# Промпт 6: паралельні обчислення через Nginx — R5

**03.11.2026, повне приймання не завершено: повторний блокер — вільне місце C:. R5 залишається частковим.** У другій спробі дві паралельні задачі успішно завершилися, їхні результати незалежно перевірено через API. Ізоляція cancel/timeout у новій моделі ще не завершена: під час наступної пари backing guard знову зупинив прогін. Успіх основної пари й помилка решти suite розділені; passed=false повного suite не приховується.

## Реалізація

- `scheduler.reserve_next`: транзакційний advisory lock навколо count + атомарного FIFO claim (`FOR UPDATE SKIP LOCKED`). Максимум 2 dispatching/running загалом, решта залишається queued. PostgreSQL зберігає reservation token і є джерелом стану.
- Dispatcher доставляє reservations конкурентно тільки на `127.0.0.1:8081/execute`. Nginx має два round-robin upstream (`8101`, `8102`), POST replay вимкнено. Зайнятий compute дає 409; повторна спроба використовує той самий token. Фактичний server_id записує compute, а не клієнт.
- Compute має один asyncio slot, захищений від другого активного запису partial unique index та від другого процесу з тим самим server_id session advisory lease. `/execute` швидко повертає 202, фонова task володіє worker process group. Повтор running-доставки власнику — idempotent 202 без нового PID, іншому серверу — 409. Для завершеної задачі — 409. Є exclusive per-job `execution-start.json`.
- Restart dispatcher не зупиняє worker і не повторює running. Незавершений dispatching зберігає token для повторної доставки; це не нове обчислення. Queue time не входить в execution budget. Cancel queued/dispatching атомарно завершує reservation до запуску.
- TERM/KILL, перевірка PID/start identity, reap і cleanup збережені. Слот не звільняється при живих членах групи: залишається running/termination_failed. Старі задачі не очищаються; нові preview/source/results зберігаються за наявною політикою.

CV-ядро, залежності, `-march=native`, ABI та shutdown не змінювалися й не перебудовувалися. Авторизація/HTTPS, ETA, autoscale та адмінпанель не додаються.

## Команди

У поточній робочій папці PowerShell. Для оновлення на підготовленому середовищі, без активних задач:

```powershell
wsl -d Ubuntu --exec bash scripts/stop dispatcher api compute-1 compute-2
wsl -d Ubuntu --exec bash scripts/migrate
wsl -d Ubuntu --exec bash scripts/start
wsl -d Ubuntu --exec bash scripts/status
```

Міграція `0002_active_server` додає тільки partial unique index; задачі/артефакти не видаляє. Застосунок залишається **http://127.0.0.1:8080**.

Цільові тести (18 пройшли, 1,173 с; у цьому запуску до дискового блокера):

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend MTW_POSTGRES_TESTS=1 /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest discover -s backend/tests -v
```

Чотири нові PostgreSQL-тести використовують випадкову ізольовану schema і прибирають тільки її: 8 конкурентних claims дають рівно 2 різні reservations; третя задача queued; terminal status звільняє reservation; running не захоплюється повторно; token переживає відновлення циклу; БД відхиляє другий активний job на одному server_id. Попередні 14 тестів контрактів/preview/cleanup також пройшли. Доданий згодом п'ятий PG-тест окремо пройшов за 0,970 с: cancel ще не прийнятого dispatching reservation знімає його й блокує пізню доставку. Разом 19 різних перевірок, без нового CV-запуску для останнього тесту. Це не підміняє повне реальне приймання.

Реальний тест, **повторювати лише після відновлення запасу C:**:

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/parallel_e2e.py --output /home/demus/.local/share/monotrajectoryweb/web/reports/parallel-20261103.json
```

Цей файл доказів уже існує; для наступної спроби обов'язково нове ім'я. Тест не перезаписує старий звіт. План повного тесту — шість API-задач: дві до успіху + третя corrupted у черзі; потім пара cancel/survivor і короткий timeout на звільненому слоті. Перша спроба створила три задачі, друга (`parallel-20261103-retry.json`) — п'ять до повторного блокера. Не потрібно ще раз запускати дві вже успішно перевірені задачі.

Після стабілізації місця повторювати тільки решту ізоляції (три нові задачі, одна до успіху, одна до cancel, одна до timeout):

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/parallel_e2e.py --isolation-only --output /home/demus/.local/share/monotrajectoryweb/web/reports/parallel-isolation-next.json
```

Цей режим підготовлено, але через повторне порушення резерву ще не запускалося. Read-only перевірка вже готових A/B результатів виконана окремо й пройшла, без нового worker:

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/check_parallel_results.py --evidence /home/demus/.local/share/monotrajectoryweb/web/reports/parallel-20261103-retry.json --output /home/demus/.local/share/monotrajectoryweb/web/reports/parallel-20261103-retry-validation.json
```

Останній output також уже існує; його не перезаписувати.

## Справжній вхід і HTTP

Наявні TUM RGB MP4 і calibration JSON з Промпта 3: 798 кадрів, 640×480, 30 fps, без resize. Відео з RGB-послідовності `freiburg1_xyz`, жодних depth/ground truth. Інші калібрування автоматично не підставляються. Історія успішних одиночних відеозапусків — у [backend](backend-prototype.md) і [frontend](frontend-prototype.md); вона не зараховується за два паралельні результати.

Еквівалент створення двох задач у WSL (тест виконував POST через наявний API):

```bash
root="$HOME/.local/share/monotrajectoryweb"
curl -sS -F "video=@$root/datasets/freiburg1_xyz-rgb-30fps.mp4" \
  -F "parameters=<$root/datasets/freiburg1_xyz-rgb-30fps.parameters.json" \
  http://127.0.0.1:8080/api/jobs
# Повторити для другої задачі; POST не очікує SLAM.
```

Фактичні скорочені відповіді першої спроби:

```text
POST /api/jobs → 202 {"id":"d1a85b93-09fe-4b60-81b6-22c24d8cb2b3","status":"queued",...}
POST /api/jobs → 202 {"id":"2b749071-63af-42b8-bc66-7cf31f5dfda0","status":"queued",...}
GET /api/jobs/{A}/snapshot → 200 {"job_id":"d1a85b93-09fe-4b60-81b6-22c24d8cb2b3","server_id":"compute-1","frame_id":183,...}
GET /api/jobs/{B}/snapshot → 200 {"job_id":"2b749071-63af-42b8-bc66-7cf31f5dfda0","server_id":"compute-2","frame_id":187,...}
GET /api/jobs/{third} → 200 {"status":"queued","server_id":null,...}
POST http://127.0.0.1:8081/execute (той самий A/token) → 202 {"duplicate":true,...} або 409 на іншому compute
```

Доставки мають `X-MTW-Job: <UUID>`; токени не друкуються у звітах. Nginx access log фіксує job, upstream IP/port і `X-MTW-Server` прийнятого запиту. Отже вибір server_id та проходження через Nginx можна звірити без довіри лише до двох health endpoint.

## Перша спроба: перекриття, потім storage guard

Часи нижче — 03.11.2026 Europe/Kyiv (UTC+2); сирий звіт містить UTC. Job started/finished охоплює decode/startup/SLAM/керування, не лише чистий SLAM.

| Задача | server_id | worker PID / native PID | Старт compute → завершення | Факт після зупинки |
| --- | --- | --- | --- | --- |
| A `d1a85b93-09fe-4b60-81b6-22c24d8cb2b3` | compute-1 | 7899 / 7951 | 01:03:57,027 → 01:04:48,983 | failed, 198 raw frame-записів / 163 валідні live-пози |
| B `2b749071-63af-42b8-bc66-7cf31f5dfda0` | compute-2 | 7898 / 7950 | 01:03:57,027 → 01:04:48,983 | failed, 199 raw frame-записів / 163 валідні live-пози |
| Третя `dff523fc-d202-49d7-8a71-aace26bf46b3` | не призначено | не запускалася | started_at=null | залишалася queued за двох зайнятих слотів, скасована тестом після блокера |

Доведене консервативне перекриття **обох живих native-процесів із tracking snapshots: 01:04:37,247–01:04:47,942, не менше 10,695 с**. За цей інтервал CPU ticks збільшилися на A=2397, B=2415; це активні native-процеси з даними, не тільки два HTTP-запити/status=running. Frame 183/187 і різні JSONL/preview файли спостерігалися одночасно; для snapshots перевірено job_id/server_id, рамки масивів та X-Frame-Id відповідного PNG. Максимальна спостережена затримка GET state/snapshot/preview — 0,536 с.

Dispatcher перезапущено під час обох native-обчислень: PID 7951/7950 не змінилися. Чотири повторні handoffs A дали idempotent 202 або 409 без нового PID. Перевірка завершених результатів, ізоляція cancel/timeout, повний failure-release сценарій і фінальні count/geometry validation **не завершені**. Фінальних поз/точок немає: export не дійшов до кінця; live count не видається за фінальний результат.

Точний блокер:

```text
ValueError: Insufficient free space on /mnt/c: 1205411840 bytes free; reserve 5368709120
```

Обидві групи завершені, compute slots вільні. Автоматичний cleanup прибрав по 798 робочих PNG; збережено по 23 published previews, відео, raw JSONL та логи. У першій спробі немає успішного result manifest. Жодну стару задачу не видалено.

Перед прогоном резерв проходив: один із контрольних замірів — 13 705 330 688 байтів (12,76 GiB) на C:. Після блокера — близько 1,12 GiB. Native MemAvailable у збережених спостереженнях не опускався нижче 5,42 GiB, swap залишався вільним; CPU/RSS native-процесів і loadavg записані. Після cleanup нові три job-каталоги займають 83 627 454 байти. Це не пояснює все падіння C:; точне джерело всіх зовнішніх змін не встановлене. Не приписуємо весь простір проєкту, не змінюємо pagefile/hiberfil/VHDX і не знижуємо reserve.

Сирий звіт: `~/.local/share/monotrajectoryweb/web/reports/parallel-20261103.json`, passed=false. Містить запити/затримки, snapshots summaries, process CPU/RSS, відновлення dispatcher та первинні signatures наявних 33 job-каталогів. `parallel-20261103-postcheck.json` підтвердив незмінні 33 каталоги/стани, п'ять CV binary hashes, порожні групи та доступність previews після cleanup.

## Друга спроба: дві валідовані задачі, блокер наступної пари

Повторні прямі заміри Windows і WSL показали відновлення C: до 13,54 GiB, тому в межах початкового дозволу виконано одну необхідну повторну спробу. Системних файлів агент не змінював. Звіт охоплює 03.11 01:11:45,956–01:13:39,748 Kyiv, `parallel-20261103-retry.json`, загальний passed=false через пізніший reserve error.

| Результат | A | B |
| --- | --- | --- |
| job_id | `04e01910-3d18-4e80-b832-973087e63331` | `2a9aeb59-6b53-4929-81b1-01414778c9c0` |
| server_id | compute-1 | compute-2 |
| worker / native PID | 13365 / 13416 | 13366 / 13417 |
| Старт compute (Kyiv) | 01:11:46,699 | 01:11:46,756 |
| Завершення compute (Kyiv) | 01:13:00,094 | 01:13:00,121 |
| Стан / native exit | succeeded / 0 | succeeded / 0 |
| Frame-записи / валідні live-пози / null | 798 / 691 / 107 | 798 / 690 / 108 |
| Фінальні пози / точки | 462 / 2740 | 447 / 2780 |
| Ліміт точок / обрізання | 100000 / false | 100000 / false |
| Worker wall / job execution (с, monotonic) | 73,669 / 74,656 | 73,648 / 74,624 |
| Geometry digest обох фінальних знімків | `05193675300fb582` | `c5757c7d88af8de1` |

Консервативне перекриття саме native tracking-процесів — **не менше 33,306 с**, UTC-наблюдання 23:12:25,846–23:12:59,152 (Kyiv 01:12:25,846–01:12:59,152). Monotonic вимір цього вікна — 33,956913 с; UTC clock у WSL коригувався, тому для нижньої межі взято меншу тривалість. За вікно A/B додали 7804/7858 CPU ticks; обидва мали зростаючі frame counters, власні PNG/JSONL, різні PID та server_id. Job wall включає decode, startup, SLAM, JSONL/snapshot, shutdown/quiescence/export, керування та cleanup; це не чиста швидкість SLAM.

Дві відповіді Nginx на початкові handoffs мають 202 і відповідні upstream `127.0.0.1:8101/8102`, job UUID та server header; токени у лог не пишуться. Dispatcher повторно stop/start під час native обробки зберіг PID 13416/13417. Повторні handoffs A дали 202 duplicate=true власнику або 409 іншому, нового worker не створили. Третя `9c239186-8cd2-4f0a-8bad-db7214779a4c` залишалася queued, поки обидва слоти зайняті; потім виконалася й отримала реальний InvalidDataError за 0,765 с, група порожня. Після цього обидва слоти прийняли наступну пару, що підтверджує звільнення після помилки.

Фактичні access log рядки (UTC, скорочені лише до значущих полів):

```text
2026-11-02T23:11:46+00:00 POST /execute 202 job=04e01910-3d18-4e80-b832-973087e63331 upstream=127.0.0.1:8101 server=compute-1
2026-11-02T23:11:46+00:00 POST /execute 202 job=2a9aeb59-6b53-4929-81b1-01414778c9c0 upstream=127.0.0.1:8102 server=compute-2
```

`parallel-20261103-retry-validation.json` має **passed=true, creates_jobs=false**: через API окремо прочитані й перевірені обидва result manifests, trajectory/map_points та повний JSONL по одному разу. IDs 0–797, timestamps i/30, Twc/quaternion, null, sorted unique final IDs як підмножина валідних live, map/segment, finite points і cap contract коректні. Потоки завершені, active_gba=0, geometry_stable=true, обидва фінальні digest у кожному job однакові. Це два справжні результати; не кількість ключових кадрів baseline.

Максимальна спостережена затримка GET state/snapshot/preview — 0,653 с. Пік суми RSS двох native — 1 227 227 136 байтів (1,143 GiB), MemAvailable WSL мінімум 5,324 GiB, SwapFree — 2 GiB. CPU counters/loadavg/RSS/вільне місце збережені по ходу роботи. Це спостереження, не гарантія CPU/RAM для всіх відео.

Наступні `eea3eef8-ec4e-41b7-ab45-f2e3daf6a7f4` (cancel) та `0119c157-abab-4d06-ba03-efa578fe8bff` (survivor) обидві failed через reserve ще під час startup native, до запланованого tracking/cancel. Вони не доводять ізоляцію скасування. Timeout-задачу не створено. Точна друга помилка:

```text
ValueError: Insufficient free space on /mnt/c: 1737760768 bytes free; reserve 5368709120
```

На старті другої спроби C: 14 540 939 264 байти (13,54 GiB), після — 1 737 760 768 (1,62 GiB). Значна зовнішня нестабільність місця повторилася; точного джерела всього падіння не встановлено. Нових обчислювальних прогонів до стабілізації запасу не проводимо. Наявні задачі, результати й артефакти обох спроб збережені; VHDX/pagefile/hiberfil не змінювалися агентом, guard 5 GiB не знижувався.

`parallel-20261103-retry-postcheck.json` підтвердив: усі 36 попередніх job-каталогів/станів (включно з початковими 33 та першою спробою) незмінні, п'ять binary hashes core/exporter/dependencies однакові. Всього стало 41 job; активних немає, усі створені worker process groups порожні, робочих `sequence/rgb` немає. Успішні results, source, previews та потрібні артефакти збережені. Міграція 0002 та unique index наявні у власній БД.

Залишок приймання: ізоляція cancel при живому іншому native, timeout на звільненому слоті зі збереженням іншої задачі, валідований результат survivor та загальний успішний підсумок isolation suite. Після цього можна змінити R5 на перевірений; до цього статус частковий.

Локальний коміт перевіреної реалізації — `56f082c` (`feat(dispatch): execute two reserved jobs concurrently through nginx`). Author і committer — підтверджена користувацька ідентичність, перевірені безпосередньо перед комітом. Історію не переписано; стороння заміна файла промптів і `Release` не включені. Цей коміт не означає завершення заблокованих перевірок ізоляції.

Push не виконувався.
