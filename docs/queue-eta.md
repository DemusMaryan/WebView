# Промпт 10: черга й приблизне очікування — R6

> Докази нижче зафіксовані для Промпта10 з двома слотами. Після R7 `queue_eta.py` використовує фактичний ready registry: базово2, максимум3; автомасштабування й R8 уже прийняті. Початкова кількість54 задачі та майбутні етапи нижче — історичний зріз. Поточні [архітектура](architecture.md), [контракт](api-contract.md), [фінальне приймання](final-acceptance.md). `queue_e2e.py` — helper конкретного приймання з початковими51 jobs, не звичайний запуск на робочій БД.

Перевірено **03.11.2026**, нативний WSL, застосунок **https://127.0.0.1:8443**. Промпт 10 завершено в наведеному контрольному сценарії. Виконано рівно **три нові реальні задачі**, без повтору сценарію. Попередні 51 задача, власники й файли збережені; тепер у PostgreSQL **54 задачі**, активних 0. Автомасштабування й адмінпанель не реалізовувалися.

## Контракт та порядок

`GET /api/jobs/{own_id}` повертає `queue_position` (1-based серед усіх готових queued) і `estimated_wait_seconds` (секунди до початку, число ≥0 або null). Для running/dispatching/terminal/cancel_requested — обидва null. Чужі ідентифікатори, назви, параметри й власники не видаються; позиція та оцінка є агрегатами глобальної черги. Без сесії 401, чужий/unowned id — 404. У list/POST/cancel нові поля присутні як null; актуальну оцінку дає status, який UI вже опитує.

FIFO: `created_at ASC, id ASC`, тільки status=queued і cancel_requested=false. Advisory transaction lock `7132103` серіалізує count+reservation; row lock тепер очікує заблокований перший запис, замість SKIP LOCKED, який міг пропустити голову FIFO. Максимум **2 dispatching/running загалом / 1 на compute**, PostgreSQL partial unique index, lease й idempotent dispatch token збережені. Скасовані й terminal не займають чергу/слот. Черга залишається в БД після перезапуску dispatcher.

API читає власника, чергу, активні задачі й статистику в одному read-only REPEATABLE READ snapshot. JSONL/геометрія для ETA не читаються. Типово **100 stored / 10 pending**; відмови `stored_job_limit_reached` / `pending_job_limit_reached` і cleanup upload збережені.

## Метод оцінки

Конфігурація `config/local.toml`:

```toml
[queue]
initial_seconds = 45.0
reference_frames = 360
reference_width = 640
reference_height = 480
history_samples = 30
```

1. Робота `w = frames × width × height / (360 × 640 × 480)`. Новий upload отримує невелику серверну `video_info` у PostgreSQL: `ffprobe` читає лише header (`nb_frames`, або duration×avg_frame_rate), timeout 2 с, дозволені тільки file/pipe protocols. Це неточна підказка, не довіра до заявлених лімітів: decoder і далі перевіряє фактичні кадри, PTS, роздільність, тривалість і калібрування. Невідомий/пошкоджений header або timeout дає null, без вигаданих frame counts. POST додатково витрачає час header probe, а не запускає SLAM у запиті.
2. З останніх до 30 успішних задач беремо медіану `execution_seconds / w`. `execution_seconds` вимірює compute через monotonic clock: запуск worker, decode, native, експорт/перевірку, очікування завершення процесів і cleanup; **час у черзі виключено**. Failed/cancelled/timed_out, нульові й нечислові виміри не навчають модель. Для старих успіхів frames та калібрована роздільність вже є в БД; backfill старих файлів/рядків не виконується.
3. Прогноз тривалості — медіанна ставка × w. Без придатної статистики — initial_seconds × w, а без характеристик — initial_seconds. `initial_seconds=0` вимикає початкову оцінку: якщо даних недостатньо, ETA може бути null. Це не додатковий часовий ліміт.
4. Два вільні слоти починаються з 0. Для активної задачі оцінка залишку — прогноз мінус elapsed від started_at. На етапі processing додатково враховується реальний processed_frames/total_frames: беремо максимум часового залишку і прогнозу × необробленої частки. Для dispatching ще не минув execution time. Прострочена оцінка без прогресу або termination_failed означає невідомий час звільнення. Від’ємні значення не повертаються.
5. Проходимо FIFO і щоразу призначаємо наступну задачу раніше доступному з **двох** слотів. Для цільової задачі час доступності її слота є ETA. Невідомий слот не заважає оцінці через інший відомий; якщо обидва невідомі — null. Результат округлюється до 0,1 с.

UI показує «У черзі», позицію та «Приблизне очікування»; для null — «Оцінка поки недоступна». Наявне polling оновлює значення й припиняється після terminal/закриття. Скасування, історія, preview та результати доступні. Немає штучного очікування або руху карти за таймером.

Обмеження: лінійна залежність від кадрів/пікселів приблизна; текстури, tracking/reset, fixed startup costs, CPU/RAM/підкачка й конкуренція змінюють час. Header для VFR може бути неточним; після успіху compute записує фактичну невелику decode metadata. ETA не гарантія, не execution deadline й не статистичний довірчий інтервал. Elapsed/фактичне очікування за timestamps залежать від системного годинника; durations обчислень — monotonic. Подальший capacity scaling не додавався.

## Команди та HTTP

Міграція **0004_video_info** додає тільки nullable JSONB, без UPDATE старих задач. У вже оновленій копії команда stop використовує новий ORM, тому сумісну add-column міграцію треба застосувати **до** reload/stop compute новим control. Початкова спроба stop до міграції дала `column jobs.video_info does not exist`; після міграції stop/start успішні, жодна задача тоді не виконувалась.

```powershell
wsl -d Ubuntu --exec bash scripts/migrate
wsl -d Ubuntu --exec bash scripts/stop api compute-1 compute-2 dispatcher
wsl -d Ubuntu --exec bash scripts/start api compute-1 compute-2 dispatcher
wsl -d Ubuntu --exec bash scripts/status
wsl -d Ubuntu --exec bash scripts/build-ui
```

Після звичайного перезапуску: `wsl -d Ubuntu --exec bash scripts/start`. Сертифікати, Secure/HttpOnly/Strict cookie, Origin/CSRF, власність та service key збережені. Приватні HTTP compute працюють тільки через належний службовий доступ.

Фактичні запити до HTTPS з авторизованим перевірочним користувачем:

```text
POST /api/auth/register                         → 201 (окремий локальний test account)
POST /api/jobs (300-frame video + calibration)   → 202, A
POST /api/jobs (360-frame video + calibration)   → 202, B
POST /api/jobs (через UI, 300 кадрів)             → 202, C
GET  /api/jobs/2d27a4da-dd0f-42aa-b998-f976f4075da2 → 200
{"status":"queued","queue_position":1,"estimated_wait_seconds":20.3,...}
GET  /api/jobs/2d27a4da-dd0f-42aa-b998-f976f4075da2 → 200
{"status":"running","server_id":"compute-1","queue_position":null,"estimated_wait_seconds":null,...}
GET  /api/jobs/2d27a4da-dd0f-42aa-b998-f976f4075da2 → 200
{"status":"succeeded","queue_position":null,"estimated_wait_seconds":null,...}
GET  /api/jobs/{id}/result, /snapshot, artifacts  → 200 (валідація всіх трьох)
POST /api/auth/logout                           → 204, сесію відкликано
```

Секрети не наведені. Test login/password залишені тільки у native приватному credentials.json (0600); session.json (0600) містить уже відкликану сесію. Жодного legacy assignment не було. HTTP/CSRF і ownership не обходилися. Browser functional context: ізольований Chrome/Playwright, **ignoreHTTPSErrors=true**, лише localhost; довіру CA це не доводить. Native HTTPS validator використовував явний CA з certificate/hostname verification. Системну довіру не змінювали; [попередні окремі TLS-докази](https.md).

## Фактичні результати

Готові TUM freiburg1_xyz MP4, **640×480 / 30 fps**, явне перевірене калібрування. Використано існуючі short-10s/short-12s файли; повторного перекодування не було. У таблиці час **Europe/Kyiv, 03.11.2026 (UTC+02)**; сирі timestamps доказів — UTC.

| Задача | job_id | Сервер | worker/native PID | Початок → завершення | Кадри / live-пози / final-пози / точки |
| --- | --- | --- | --- | --- | --- |
| A | f6827a88-21c5-4bb0-8604-b73f672586fd | compute-1 | 20252 / 20383 | 19:30:44,142 → 19:31:38,359 | 300 / 251 / 226 / 2650 |
| B | 7016c825-1cfb-419a-a663-3d8ad396fd72 | compute-2 | 20256 / 20400 | 19:30:44,500 → 19:31:46,102 | 360 / 303 / 257 / 2501 |
| C | 2d27a4da-dd0f-42aa-b998-f976f4075da2 | compute-1 | 20812 / 20890 | 19:31:38,492 → 19:32:27,667 | 300 / 248 / 206 / 2698 |

Усі succeeded, cap точок 100000, обрізання false. Перевірено послідовні frame_id/PTS, Twc/null/tracking, finite points, map/segment, належність final poses до live frames і фінальну snapshot consistency (workers_finished, active_gba=0, два geometry digest рівні). JSONL прочитано один раз під час кінцевої валідації, а не при кожному status.

Worker wall: **54,705 / 62,368 / 47,252 с**; compute execution_seconds: **56,207 / 63,593 / 50,103 с**. Це end-to-end робота із decode/export/cleanup та витратами процесів, не чиста швидкість SLAM. Всі тимчасові sequence/rgb видалені після завершення груп; preview успішно відкрився після cleanup, відео/результати залишилися. Живих або zombie членів відповідних process groups немає, обидва слоти вільні.

Для C:

- Створено 19:31:18,578; перша зафіксована оцінка 19:31:19,163 — **20,3 с**, позиція **1** (UI округлив до «близько 21 с»).
- Реальний старт 19:31:38,492: **19,329 с після прогнозу**, помилка прогноза **+0,971 с**; загальне очікування від created_at — **19,914 с**.
- Старт через **0,134 с** після finished_at A; C зайняла звільнений compute-1, поки B ще працювала.
- Поки C була queued без native-процесу, доведений нижній поріг одночасної роботи native A/B — **18,601 с**, CPU ticks **+4201 / +4274**, processed frames **+216 / +219**. Це процеси й реальний прогрес, не перекриття HTTP-запитів.
- Dispatcher перезапущено **19840 → 20781** під час очікування: C до/після queued, native PID A/B незмінні 20383/20400. Нових/повторних native запусків для A/B немає.

Браузер бачив queued → running → succeeded, позицію/ETA, карту/preview й результат після reload; помилок JS/mixed content не зафіксовано. API залишався доступним; серед зафіксованих driver GET максимальна тривалість **0,413 с** (вибірка, не гарантія). Службові execute для всіх трьох UUID є в Nginx access log з upstream і server_id. Реальний сценарій не повторювали.

## Тести, ресурси й збереження

```powershell
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest -v test_queue_pg test_scheduler_pg test_auth_pg test_history_pg
wsl -d Ubuntu --exec bash scripts/build-ui
```

**34 унікальні backend tests пройшли**, з них 9 нових queue tests: tied timestamps/FIFO, заблокована голова, concurrent 2 slots, звільнення, queued cancel, reconnect/restart persistence, cold start/scaling, disabled fallback/unknown metadata, successful-only durations без queue time, невід’ємний залишок, owner/401/404 і невидача чужих UUID. Основний запуск мав одну розбіжність старого migration test щодо нового поля/revision; тест доповнено перевіркою обох 0003/0004 і окремо успішно повторено. Інші 33 тести повторювати не знадобилося. **8 frontend tests, TypeScript/Vite build успішні.**

Єдиний реальний прогін (prepare вимагає 51 наявну terminal задачу; ці докази вже створені, автоматично не повторювати):

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/queue_e2e.py prepare
# ROOT з prepare; observe запускається окремо й завершується за marker:
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/queue_e2e.py observe --root ROOT
node frontend/tests/queue-browser.mjs ROOT
# Лише повторний read-only аналіз наявних доказів, без HTTP/SLAM:
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/queue_e2e.py timing --root ROOT
```

Докази поза Git: `~/.local/share/monotrajectoryweb/web/reports/queue-356124ce0fb14080873fd255b3fbec2f/`: before.json, jobs.json, browser.json, queued.png, succeeded.png, native-series.jsonl, nginx-routes.json, validation.json, queue-timing.json, resources-summary.json. Backend log — `web/reports/prompt10-backend-tests.log`. Приватні credentials/session до Git не додаються.

Один існуючий Windows monitor, PID **10712**, Win32 API без subprocess на кожен показник; 383 samples по 0,5 с, 19:30:14–19:33:25 Kyiv. Зупинений власним marker, stderr порожній. Windows JSONL: `%LOCALAPPDATA%\MonoTrajectoryWeb\diagnostics\queue-356124ce0fb14080873fd255b3fbec2f\windows.jsonl`. WSL серія містить PID/start_ticks/CPU/RSS/frame progress/loadavg і дискові заміри.

C: до **19 305 410 560 байтів (17,98 GiB)**, після валідації **19 049 824 256 (17,74 GiB)**; мінімум Windows samples **19 049 381 888**, значно вище незмінного reserve 5 GiB. Native перед prepare — близько 941 GiB вільно. Windows Available RAM у вибірці: початково 745 660 416, мінімум 16 183 296 байтів; commit headroom початково 3 524 222 976, мінімум 1 376 923 648 байтів. **RAM/commit тільки telemetry**, gates/автоскасування за ними немає. Це спостережені samples, не незаписані піки й не висновок про причину навантаження. Системні налаштування/pagefile/VHDX не змінювалися.

Звірено всі попередні колонки 51 рядка/owner та file signatures (імена/size/mtime), а не повне повторне hashing медіа. П'ять CV SHA-256 і п'ять R5 report SHA-256 незмінні. Nullable video_info старих задач залишено NULL. Додано рівно A/B/C та один явно перевірочний local account, без призначення legacy. Ліміти 100/10, HTTPS та службовий ключ збережені. Попередні докази R3/R5 не переписувалися.

Перевірений код, міграція, UI й тести — локальний коміт **461eece** (`feat: add FIFO queue position and two-slot wait estimates`). Автор і комітер: `versal217 <versaldata@gmail.com>`, ефективні ідентичності перевірені перед комітом. Стороння заміна файла промптів і Release не включені. Push не виконувався.
