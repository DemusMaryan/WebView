# Промпт 3: нативний worker, PostgreSQL і API

**Перевірено 2 листопада 2026 року; Промпт 3 завершено.** Frontend і Промпт 4 не виконувалися. Прототип доступний лише на localhost, `owner_id=null`, автентифікація й HTTPS заплановані. Два compute-процеси підготовлені; dispatcher запускає **одну** задачу одночасно. Паралельне балансування R5 ще не доведене.

## Запуск і дані

Працююче CV-ядро не перебудовувалося. ORB-SLAM3 `4452a3c4ab75b1cde34e5505a36ec3f9edcdc4c4`, прапорці `-march=native`, патчі й shutdown з Промпта 2 збережені. SHA-256 DBoW2/g2o/Pangolin збігаються з попереднім `logs/export-dependency-hashes-before.json`. Відеоадаптер і перетворення TUM описані в [video-worker.md](video-worker.md).

Нове середовище: PostgreSQL `16.15-0ubuntu0.24.04.1`, Nginx `1.24.0-2ubuntu7.19`, FFmpeg `7:6.1.1-3ubuntu5`. Python 3.12, окреме native venv `~/.local/share/monotrajectoryweb/web-venv`; усі версії — [backend/requirements.lock](../backend/requirements.lock). CV venv не змінюється.

Передумови: WSL Ubuntu 24.04, уже перевірений `mono_export`, установлені `postgresql-16`, `nginx`, `ffmpeg`, `python3-venv`. Інсталятор пакета може сам запустити типовий системний сервіс; він не використовується проєктом. Project scripts не перезаписують `/etc/nginx`, системний PostgreSQL або дані інших робіт. Власний cluster і роль/БД `mtw` створюються окремо; пароль генерується в native storage з режимом 0600 і не друкується. Повторний `setup` перевірено без скидання БД.

З PowerShell у поточному репозиторії:

```powershell
wsl -d Ubuntu --exec bash scripts/setup
wsl -d Ubuntu --exec bash scripts/start
wsl -d Ubuntu --exec bash scripts/status
```

`scripts/stop` зупиняє лише власні процеси, зберігаючи БД запущеною; `scripts/stop api compute-1 compute-2 nginx dispatcher postgres` зупиняє також **власний** cluster зі збереженням даних. Можна передати окремі назви сервісів. PID перевіряються за `/proc`, часом старту та призначенням процесу; повторно використаний PID не сигналиться. На stop dispatcher припиняє видачу нових задач, активні compute-задачі скасовуються штатним менеджером групи процесів. Глобальні сервіси не зупиняються цими скриптами.

Центральний файл — [config/local.toml](../config/local.toml), можна вказати інший через `MTW_CONFIG`. Native storage: `~/.local/share/monotrajectoryweb/web`; `pgdata`, `jobs/<UUID>`, логи, private Nginx і секретний `database-url` лежать поза Git. Репозиторій залишається в початковому місці, шляхи з пробілами/кирилицею передаються аргументами, без shell-конкатенації.

| Компонент | Адреса |
| --- | --- |
| Вхідний Nginx | `127.0.0.1:8080` |
| API | `127.0.0.1:8000` |
| Внутрішній Nginx → compute | `127.0.0.1:8081` |
| compute-1 / compute-2 | `127.0.0.1:8101` / `127.0.0.1:8102` |
| Окремий PostgreSQL | `127.0.0.1:55432` |

SQLAlchemy/Alembic: міграція `0001_jobs`; UUID, незаданий owner_id, параметри, стани/етапи, фактичні лічильники, часові мітки, server_id, cancellation flag, reservation token, PID/start ticks, помилка й відносні посилання. Відео, кадри, JSONL і великі масиви в БД не записуються.

## HTTP і процес виконання

У WSL, з цього репозиторію, fixture створюється один раз:

```bash
root="$HOME/.local/share/monotrajectoryweb"
"$root/web-venv/bin/python" scripts/video-fixture.py
curl -sS -F "video=@$root/datasets/freiburg1_xyz-rgb-30fps.mp4" \
  -F "parameters=<$root/datasets/freiburg1_xyz-rgb-30fps.parameters.json" \
  http://127.0.0.1:8080/api/jobs
```

Якщо fixture вже існує, повторно створювати його не потрібно. Parameters JSON містить обов’язкове явне калібрування; стороннім відео TUM автоматично не призначається. POST завантажує файл і створює запис у черзі, не виконує SLAM у request handler. Dispatcher атомарно резервує рядок `FOR UPDATE SKIP LOCKED`, один екземпляр захищений PostgreSQL advisory lock. HTTP-запит проходить **dispatcher → Nginx:8081 → compute**. Compute перевіряє reservation і статус під row lock, займає один слот та створює окрему process group Python worker → mono_export. Повторна доставка того самого job_id не запускає нове обчислення.

Фактичні запити завершеного E2E, нижче наведено скорочені JSON-відповіді:

```text
POST /api/jobs (multipart video + parameters)
202 {"id":"e8688279-ad66-42af-b7ec-3ce672547406","status":"queued","owner_id":null,...}
GET /api/jobs/e8688279-ad66-42af-b7ec-3ce672547406
200 {"status":"running","server_id":"compute-2","stage":"processing",...}
GET /api/jobs/e8688279-ad66-42af-b7ec-3ce672547406/snapshot
200 {"frame_id":0,"processed_frames":1,"total_frames":798,"tracking_state":1,"Twc":null,...}
GET /api/jobs/e8688279-ad66-42af-b7ec-3ce672547406/result
200 {"processed_frames":798,"valid_live_poses":688,"final_poses":429,"points_exported":2780,...}
GET /api/jobs
200 [ ...збережені задачі... ]
POST /api/jobs/8ff9caed-0d38-443b-8c67-fc4aa8e2e7c3/cancel
202 {"cancel_requested":true,...}
GET /api/jobs/8ff9caed-0d38-443b-8c67-fc4aa8e2e7c3
200 {"status":"cancelled","server_id":"compute-2",...}
```

POST успішної задачі повернувся за **0,140 с**, включно з локальним upload 11 131 364 байтів. GET стану під час перевірки — максимум **0,0143 с**; це спостережений результат цього запуску, не гарантія затримки.

Snapshot — атомарний `latest.json`, максимум 256 точок і 240 записів live-історії, оновлення tracking не частіше 0,5 с. Найбільша спостережена відповідь — 77 682 байти. Raw JSONL читається worker інкрементально, кожен байт один раз; polling API його не перечитує. Історія зберігає null, tracking state, map_id/segment_id й розриви, не інтерполюється. Live-історія не підміняє фінальну оптимізовану підмножину поз.

`frame_url` адресує конкретний незмінний RGB PNG `frames/{frame_id}`; перевірено `X-Frame-Id` і фактичні 640×480. `/result` повертає невеликий manifest і URL артефактів: trajectory.json, map_points.json, snapshots.jsonl, native-summary.json. Завантаження великого артефакту — окремий запит. Імена — whitelist, UUID і файлові шляхи генерує сервер. Неготовий результат повертає 409.

## Результати приймання

E2E 02.11.2026: 19:27:46,955–19:30:38,384 Europe/Kyiv (17:27–17:30 UTC), **171,428 с**, `passed=true`.

| Перевірка | Фактичний результат |
| --- | --- |
| Відео через API | `e8688279-ad66-42af-b7ec-3ce672547406`, compute-2, succeeded |
| RGB / пози / точки | 798 frame-записів, індекси 0–797; 688 валідних live-поз, 110 null; 429 фінальних поз, 2 780/2 780 точок, cap 100 000, truncated=false |
| Tracking | NOT_INITIALIZED: 4, OK: 794; tracking OK не прирівнюється до доступної валідної live-пози |
| Проміжні дані | 52 різні frame_id до завершення: 0, 11, 21, …, 759, 772, 786; raw timestamps перевірено як i/30 |
| Координати / фінальна карта | Twc, quaternion xyzw, relative; перевірено числові матриці/кватерніони та сегменти; workers_finished=true, active_gba=0, geometry_stable=true; обидва digest `ea920d09e6e153a3` |
| Скасування під час tracking | `8ff9caed-0d38-443b-8c67-fc4aa8e2e7c3`, compute-2; worker PID 803, native PID 839 припинились, група порожня, слот вільний; 0,694 с |
| Тайм-аут у native runner | `223bd115-4230-4e8c-819b-7711836dc4d2`, compute-1; cap 45 с, фактично 45,039 с; PID 861/904 припинились, група порожня, слоти вільні |
| Перезапуск усіх сервісів і власного PostgreSQL | Результат успішної задачі тотожний; queued `989907ec-302f-46b6-9373-89d0ef194b47` збережено без старту до dispatcher; черга 8,401 с не включилася у cap 1 с, execution 1,081 с, timed_out |
| Некоректний вхід | Пошкоджене відео → failed / InvalidDataError; фактичні frame/duration/resolution caps і mismatch калібрування → failed з поясненням; відсутнє калібрування → 422 |
| File/body caps | Зменшений file_bytes → 413; завелике тіло через Nginx, прямо в API та chunked-upload → 413 |
| Черга | 10 pending задач прийнято; 11-та → 429; queued-cancel завершив усі 10 без запуску |
| Повторний execute | Коректний старий reservation token завершеної задачі → 409, без нового worker |
| Єдиний dispatcher | Другий процес відхилений advisory lock, exit 1: `Another dispatcher owns the single execution slot`; жодної нової reservation |
| Compute health | Різні server_id compute-1 / compute-2; це не доказ паралельності |

Worker wall time успішного відео — **62,461 с**, з них decode **26,810 с**, native total **35,121 с** (processing **28,695 с**). Wall включає декодування, startup/vocabulary, tracking/mapping, файловий ввід/вивід, JSONL/snapshot, shutdown/quiescence та фінальну перевірку; черга й HTTP upload до нього не входять. Runner без pacing. Ці числа не є чистою швидкістю SLAM.

Повні докази поза Git: `web/reports/prompt3-e2e-20261102.json` і `web/reports/upload-checks.json`; артефакти — `web/jobs/<job_id>/`. Відтворення перевірок із PowerShell після start:

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest discover -s backend/tests -v
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/e2e.py --output /home/demus/.local/share/monotrajectoryweb/web/reports/new-e2e.json
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/upload_checks.py
```

Вісім unittest пройшли. E2E створює справжні задачі, перевіряє скасування/тайм-аут, перезапускає **лише власні** сервіси й БД; запускай його без інших активних робіт цього прототипу.

## Ліміти й відкриті питання

Початкові максимуми з `config/local.toml`: файл 64 MiB, відео 40 с, 1 200 кадрів, 1280×720, execution 180 с, 10 pending задач. Це консервативні параметри локального прототипу навколо виміряних 798 кадрів 640×480; параметри запиту можуть лише зменшувати максимуми. Розмір перевіряється при upload, інші відеоліміти — також на фактично декодованих кадрах. Execution budget починається при прийнятті compute і охоплює decode та native обчислення. Менеджер припиняє всю створену process group через TERM/KILL і перевіряє відсутність живих членів перед звільненням слота.

Перший E2E 01.11 дійшов до перевірки restart і виявив порожній private Nginx pidfile після `nginx -t`; обробку виправлено. Потім C: заповнився (0 байтів), WSL повертав I/O error. Після звільнення місця 02.11 всі тести виконані успішно. Виявлені нульові README/architecture збережено в `recovery/20261102-documents`, тексти відновлено з Git і оновлено. Додано reserve: 2 GiB у native FS та 5 GiB на backing volume `/mnt/c`, з перевіркою під час upload/decode/native execution; backing_mount слід змінити, якщо VHDX розміщений на іншому томі. Linux df для VHDX сам по собі не доводить наявності фізичного місця у Windows.

Після відновлення видалено тільки 798 відновлюваних decoded PNG з першої невдалої CLI-спроби `runs/video-adapter-prompt3/sequence/rgb` (281 172 574 байти); її логи/метадані, вихідне відео й усі перевірені CV/API результати збережено. Сторонні `Release` і заміна файла промптів не комітяться.

Поки немає авторизації/HTTPS, користувацьких квот/прив’язки owner, frontend, ETA, паралельного dispatcher, автомасштабування чи адміністративної панелі. Немає політики автоматичного видалення історичних медіа/результатів: їх обсяг треба контролювати, reserve зупиняє приймання/роботу завдань при нестачі місця. При перерваному compute owned-процеси завершуються при його наступному старті, задача позначається failed/cancelled; автоматичного replay немає. Загальна підтримка моделей камер, multi-map/reset і точність на сторонніх відео не доведені.

Локальні коміти коду: `38d6f4a` — перевірений відеоадаптер; `5509720` — PostgreSQL/API, dispatcher/compute, нативні lifecycle-скрипти, ліміти та перевірки. Автор і комітер обох: `versal217 <versaldata@gmail.com>`, ефективні ідентичності перевірялися перед кожним комітом. Попередня історія не переписувалася.

Push не виконувався.
