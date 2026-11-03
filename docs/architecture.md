# Нативна архітектура

Промпти 3–4 перевірено 02.11.2026; [аудит Промпта 5](day1-report.md) зберігає історичний зріз послідовного dispatcher. [Промпт 6](parallel-processing.md) підтвердив два слоти, [7–9](api-contract.md) — глобальні ліміти, ownership/auth та HTTPS, [10](queue-eta.md) — FIFO й приблизне очікування. Усі компоненти запускаються нативно у WSL Ubuntu, без Docker. Frontend React/TypeScript/Vite/Three.js віддається власним Nginx на https://127.0.0.1:8443; HTTP:8080 тільки GET/HEAD redirect. Поточні адреси, ліміти й шляхи централізовані у `config/local.toml`.

```text
Браузер (React / TypeScript / Vite / Three.js)
  → TLS Nginx 127.0.0.1:8443 (HTTP:8080 redirect)
  → FastAPI 127.0.0.1:8000 → PostgreSQL 127.0.0.1:55432
                                   ↑
Dispatcher ← персистентна jobs-черга ┘
  → Nginx 127.0.0.1:8081
  → незалежні HTTP compute-1:8101 / compute-2:8102 (127.0.0.1)
      → окрема process group Python worker → перевірений mono_export

API / compute / worker → native shared storage поза Git
```

POST API приймає відео й явне калібрування, перевіряє файл/параметри, створює UUID-задачу та швидко повертає 202. Дані PostgreSQL: SQLAlchemy jobs/users/auth_sessions, Alembic до 0004_video_info, параметри, невеликі video hints, статус/етап/лічильники, часові мітки, server_id, cancellation/reservation, PID/start ticks, виміряний execution time, помилка та посилання. Великі відео, PNG, JSONL і фінальні масиви залишаються у `~/.local/share/monotrajectoryweb/web/jobs/<UUID>`. БД має власний cluster/роль, конфігурація Nginx окрема; глобальні конфігурації не перезаписуються.

Dispatcher резервує найстарішу готову queued-задачу через row lock, FIFO created_at/id. Промпт 10 прибрав SKIP LOCKED: тимчасово заблокована голова не пропускається. Транзакційний advisory lock робить count + claim атомарним для всіх циклів/процесів: максимум **2 dispatching/running** загалом. Доставка виконується конкурентно через Nginx round-robin upstream із двома серверами. Зайнятий compute відповідає 409, dispatcher повторює той самий job/token через Nginx, поки PostgreSQL ще показує dispatching. Автоматичне Nginx replay POST вимкнено (`proxy_next_upstream off`).

Compute під row lock перевіряє token/status, займає свій asyncio slot, записує фактичний server_id і швидко повертає 202; окрема фонова task володіє дочірнім worker до його завершення. Partial unique index `uq_jobs_active_server` (міграція `0002_active_server`) додатково забороняє два активні записи одного сервера. Session advisory lease не дозволяє двом compute-процесам з однаковим server_id одночасно виконувати startup/recovery. Повторна доставка до власника running-задачі повертає idempotent 202; до іншого або завершеного job — 409 без нового worker. Незмінний `execution-start.json`, створений через exclusive open, додатково захищає від другого запуску того самого каталогу.

Після перезапуску dispatcher running-задачі продовжують працювати; dispatching reservation повторно надсилається з тим самим token. Скасування queued/dispatching під row lock може завершити задачу до запуску. Якщо process group не припинилася, compute залишає running/termination_failed і утримує слот замість дозволу наступного обчислення. Це захист від перевищення ліміту, а не автоматична відмова чи replay.

Кожен compute має незалежний HTTP-процес, server_id і один слот. Менеджер запускає worker у новій process group; той декодує відео у RGB/PTS-послідовність і запускає незмінений перевірений runner. Native `-march=native`, ABI, версії й shutdown з Промпта 2 збережено. Один відеофайл завжди обробляється послідовно. Скасування та execution timeout припиняють worker і native дочірній процес, перевіряють відсутність живих членів групи й звільняють слот. Черга не входить у execution budget.

Worker читає JSONL інкрементально та публікує атомарний latest snapshot з обмеженими масивами. API опитує лише цей невеликий файл; повний JSONL зберігається окремим артефактом. Точний frame_id визначає RGB-кадр для перегляду. Twc/null, tracking state, map_id/segment_id й прогалини збережені; координати у відносних одиницях. Фінальна геометрія перевіряється telemetry ядра й порівнянням двох узгоджених знімків. Live-історія є послідовністю історичних оцінок, фінальна траєкторія — окремою оптимізованою підмножиною.

Frontend читає public limits через `/api/config`, опитує лише відкриту активну задачу приблизно 2 рази/с і атомарно перемикає snapshot разом із уже декодованим RGB preview. GPU-буфери перевикористовуються; при закритті перегляду звільняються, polling припиняється. Для нових задач опубліковані previews зберігаються окремими hardlinks до очищення робочого `sequence/rgb` після завершення process group. Відео/результати й початкові 29 задач залишаються. Це локальний lifecycle одного запуску, не глобальна система керування сховищем. [Браузерні докази й координатна конвенція](frontend-prototype.md).

Ліміти застосовуються також до фактично декодованих кадрів; intrinsics не вгадуються, resize не виконується. Native сховище та backing Windows volume мають окремі резерви місця. Проєктні scripts/setup, start, status, stop керують лише власними PID/cluster і зберігають БД та артефакти. Після перезапуску результат та черга доступні; аварійно перерваний compute не відновлює SLAM автоматично. [Команди й фактичні докази](backend-prototype.md).

Усі project listeners — localhost. Нові задачі отримують owner із PostgreSQL user session, усі API/artifact шляхи перевіряють власність, CSRF й HTTPS; 51 legacy owner=NULL не призначається автоматично. Службовий dispatcher/compute key окремий від приватного TLS proxy key. React/TypeScript/Vite реалізовано у Промпті 4. Черга має global cap 10 pending окремо від 100 stored; позиція й ETA двох слотів обчислюються з bounded PostgreSQL metadata й успішних durations без JSONL, details — [queue-eta.md](queue-eta.md). Міграція 0004 додає nullable video_info, не змінюючи старі рядки. Користувацьких квот та автоматичного retention немає; нові тимчасові decoded-кадри очищаються після роботи процесів.

R5 перевірено: збережена справжня пара через Nginx із native overlap ≥10,140 с та двома валідованими результатами; окремо завершені cancel/timeout isolation на різних compute. Python manager дочікується й збирає всі записи власного PGid, включно із zombie, перш ніж очищати PNG і звільняти слот. Сусідня група продовжує роботу, API доступний. Це два незалежні HTTP-процеси одного WSL-вузла; часові ряди/PID/результати — у `docs/parallel-processing.md`. У тестовому сценарії лишилися C: ≥10 GiB перед стартом і 5 GiB аварійного резерву; RAM/commit — інформаційні. Глобальні ресурси ОС, pagefile, CV/ABI/Shutdown та стандартний job limit 180 с незмінні; per-job 22 с застосовано тільки timeout-target через вже наявний механізм.

R7 збережено: майбутній контролер реагує на навантаження, запускає третій сервер, перевіряє готовність і додає його в upstream; проєктна верхня межа — 3, базово — 2. Це налаштування проєкту, не числа з навчальної умови. Відповідність масштабування лише процесами на одному вузлі наведеним у WebProject.txt ВМ/контейнерам залишається відкритим питанням. Автоматичне розширення й адміністративний моніторинг R8 не реалізовані.
