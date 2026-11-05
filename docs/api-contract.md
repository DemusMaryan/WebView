# Поточний локальний API: задачі та історія

Контракт Промптів 7–11, оновлено 05.11.2026. Вхід через Nginx: **https://127.0.0.1:8443**. Усі адреси нижче відносні. Прототип локальний: облікові записи, PostgreSQL sessions, власність, TLS 1.2/1.3 і queue ETA реалізовані; персональні квоти заплановані. Історичні відповіді Промпта 3 не замінюють цей контракт. [Авторизація](authentication.md), [TLS і ручна довіра CA](https.md), [черга, ETA й докази](queue-eta.md).

HTTP:8080 GET/HEAD повертає 308 на canonical HTTPS зі збереженням path/query; інші методи — 426 без проксіювання user API. Прямий HTTP `/api/*` на приватному API:8000 повертає 400 `https_required`, також із підробленим forwarding. Довіряємо forwarded headers тільки від налаштованого loopback proxy з приватним X-MTW-Proxy-Key; Uvicorn proxy_headers=False. Host/Origin — canonical IP та порт 8443; localhost:8443 має валідний SAN і перенаправляється на canonical URL. Немає CORS. Сертифікати поза Git; curl використовує явний `--cacert`, системну довіру не встановлено.

## Авторизація

| Запит | Контракт |
| --- | --- |
| `GET /api/auth/session` | Bootstrap: user або null, csrf_token; HttpOnly-cookie. За потреби коротка анонімна сесія для CSRF login/register |
| `POST /api/auth/register` | JSON login/password, 201 з user/csrf_token та новим cookie; 409 login_unavailable |
| `POST /api/auth/login` | JSON login/password, 200 з user/csrf_token та новим cookie; 401 invalid_credentials |
| `GET /api/auth/me` | Поточні id/login, 401 без чинної авторизованої сесії |
| `POST /api/auth/logout` | 204, відкликання серверної сесії й видалення cookie |

Bootstrap доступний без входу, jobs/config та всі їхні підмаршрути потребують входу. Без нього 401 `authentication_required`; чужий або unowned job — 404. Для всіх API mutations, включно з register/login/logout, потрібні Origin `https://127.0.0.1:8443` та X-CSRF-Token з bootstrap/входу поточної сесії; порушення — 403 `csrf_rejected`. Не передавати секрети в URL. Cookies **Secure/HttpOnly/SameSite=Strict**, cookie_mode=https; bootstrap оновлює старий dev-cookie без продовження строку БД. Auth validation errors не повертають password/input. Файлові відповіді захищені тим самим owner check, private/no-store. Preview/video/artifact URL відносні до HTTPS origin, без mixed content.

## Ліміти та створення

`GET /api/config` повертає `limits`, `poll_interval_ms`, `camera_model`, `scale` та `authentication`. У `config/local.toml`, секція `[limits]`:

```toml
stored_jobs = 100
queued_jobs = 10
```

`stored_jobs` — додатне ціле, глобальна кількість **усіх** записів у PostgreSQL, незалежно від стану. `queued_jobs` — окрема межа незавершених записів: queued, dispatching, running. Успішні, failed, cancelled і timed_out також витрачають stored quota. Конфігурацію API перечитує при старті; зміну ліміту застосовують перезапуском API. Ліміт не є політикою видалення й не гарантує обсяг медіа в байтах.

`POST /api/jobs` приймає multipart `video` та текстове JSON-поле `parameters` із явним `calibration`, із чинною user session/CSRF. Успіх — `202`, метадані задачі, `id`, `status_url`; owner_id встановлюється із сесії, dispatcher запускає обчислення окремо. Клієнтський owner_id у parameters або зайвому form-полі відхиляється 422. Відео й калібрування описані у [video-worker.md](video-worker.md). Попередні обмеження файлу/декодування/execution та дискові резерви збережені.

Якщо збережено `stored_jobs` або більше записів, відповідь `429`:

```json
{"detail":{"code":"stored_job_limit_reached","message":"Stored job limit reached; history and results remain available","limit":100,"count":100}}
```

Окремий pending cap дає `429` з `detail.code=pending_job_limit_reached`, `message`, `limit`, `count`. Якщо досягнуто обидві межі, першою повідомляється stored quota. Інші відмови: `413` розмір, `422` параметри/порожній файл, `507` дисковий резерв. Помилки валідації можуть мати масив `detail`, дискові/файлові помилки — рядок.

Upload передує admission: PostgreSQL transaction бере спільний advisory lock `7132101`, рахує всі записи, окремо pending і вставляє задачу під тим самим lock. За стандартного READ COMMITTED наступний count після очікування lock бачить commit попереднього POST. Одночасні API-запити не можуть зайняти останнє місце двічі. При відмові видаляється тільки власний новий UUID-каталог цього upload, включно з уже записаними частинами. Робочі чи старі задачі не прибираються. Ліміт застосовується до створення через API; прямий адміністративний INSERT у БД його обходить.

Перевищення ліміту вже наявними даними блокує лише **створення**. List, state, snapshot, result, artifacts, previews і cancel залишаються доступними. Автоматичного видалення, retention manager чи користувацьких квот немає.

## GET /api/jobs — пагінація

| Параметр | Типово | Дозволено |
| --- | --- | --- |
| `page` | 1 | ціле 1…2147483647 |
| `page_size` | 20 | ціле 1…100 |

Порядок: `created_at DESC, id DESC`. UUID — детермінований другий ключ, включно з однаковими timestamps. **total/items містять тільки власні задачі поточного користувача**, без unowned. Ліміт admission глобальний, `stored_job_limit` не є персональною квотою. `total` та `items` одного запиту читаються в одній read-only REPEATABLE READ транзакції. Offset-пагінація гарантує відсутність пропусків/дублів на незмінному наборі; між різними запитами нові записи можуть зсунути сторінки. Довготривалої замороженої сесії історії немає.

Відповідь `200` для нового облікового запису без задач (51 старий запис не призначається автоматично):

```json
{
  "items": [],
  "total": 0,
  "page": 1,
  "page_size": 20,
  "total_pages": 0,
  "has_next": false,
  "has_previous": false,
  "stored_job_limit": 100
}
```

`items` містить об'єкти метаданих: id, owner_id, status/stage, кількість кадрів, timestamps, server_id, cancel_requested, error, worker_pid, execution_seconds, parameters, has_result, has_snapshot, eta_seconds. Повної геометрії, snapshot чи JSONL у списку немає; артефакти для list не читаються.

Порожня БД: `items=[]`, `total=0`, `total_pages=0`, обидва прапорці false. Сторінка за межами набору дає `200`, порожній `items`, незмінний total/total_pages; `has_previous=true`, якщо page>1 і total>0. Некоректне число/межа — `422` validation detail. Невідомі або повторені параметри — `422`, код `invalid_pagination_parameter`. Старий `?limit=50` більше не підтримується. Відповідь тепер об'єкт із `items`, а не старий масив; frontend оновлено разом із API.

```powershell
wsl -d Ubuntu --exec curl --cacert /home/demus/.local/share/monotrajectoryweb/web/tls/ca.crt.pem "https://127.0.0.1:8443/api/jobs?page=1&page_size=20"
# Без cookie тепер 401. Авторизовану історію перевіряй через UI або захищений cookie jar.
```

## Контракти перегляду й скасування

| Запит | Поведінка |
| --- | --- |
| `GET /api/jobs/{id}` | Метадані власної задачі та актуальні queue_position/estimated_wait_seconds; чужий/відсутній/unowned id — 404 |
| `GET /api/jobs/{id}/snapshot` | Невеликий latest snapshot; null-пози/tracking/map/segment збережені; JSONL не сканується |
| `GET /api/jobs/{id}/frames/{frame_id}` | Опублікований PNG, `X-Frame-Id`; 404, якщо preview недоступний |
| `GET /api/jobs/{id}/video` | Вихідне input.video як attachment лише власнику |
| `GET /api/jobs/{id}/result` | Manifest і URL артефактів; 409 до succeeded, 503 якщо файл manifest відсутній |
| `GET /api/jobs/{id}/artifacts/{name}` | trajectory.json, map_points.json, snapshots.jsonl або native-summary.json для succeeded; великий вміст завантажується окремо |
| `POST /api/jobs/{id}/cancel` | 202 з метаданими; terminal task не запускається повторно; для running запит обробляє compute |

У Промпті 8 всі ці маршрути перевіряють власника; video додано як захищений attachment. На compute /execute і /health потрібен приватний X-MTW-Service-Key, також при прямому зверненні. Браузерна сесія його не замінює. Ключ не видається користувачам, dispatcher/control читають його з native файла 0600. [R5 і cancel/timeout isolation](parallel-processing.md) залишаються окремими доказами; CV не змінювався.

## Позиція черги та ETA — Промпт 10

Промпт 11 змінює capacity на кількість fresh-ready registry (base2/max3); starting/stopped/failed/stale сервер не створює додаткового слота. Реальна перевірка R6 двох слотів збережена; R7 автоматично додав третій контейнерний слот через Nginx, усі чотири результати сценарію валідні. `worker_pid_scope` у metadata пояснює PID: `host` для native baseline, `container:compute-3` для третього. PID compute-3 не можна трактувати як host PID; mapping міститься тільки в операторських доказах. Нових публічних службових/admin endpoints немає. [Команди та докази R7](autoscaling.md).

`GET /api/jobs/{own_id}` для готової до запуску queued-задачі додає:

```json
{"status":"queued","queue_position":1,"estimated_wait_seconds":20.3}
```

Це фрагмент реальної відповіді для `2d27a4da-dd0f-42aa-b998-f976f4075da2`. queue_position — додатна глобальна позиція серед queued без cancel_requested, FIFO `created_at ASC, id ASC`. Dispatching/running/cancelled/terminal виключені. estimated_wait_seconds — приблизне число ≥0, округлене до 0,1 с; null, якщо невідомі характеристики/статистика та початкова оцінка вимкнена, або неможливо оцінити доступність слотів. Для неqueued обидва поля null. Чужі UUID/імена/власники не повертаються.

GET status рахує оцінку зі snapshot PostgreSQL: до 30 успішних execution durations без queue time, кадри×пікселі, залишок активної роботи та два паралельні слоти з FIFO попередниками. `[queue].initial_seconds=45` — cold start на 360×640×480, значення 0 вимикає fallback. JSONL/великі артефакти для ETA не читаються. [Формула, випадки null, налаштування й точні обмеження](queue-eta.md).

У POST/list/cancel ці нові поля теж є, але null: це metadata responses, актуальні значення потрібно брати з GET status. Старе `eta_seconds` залишається null для сумісності; його не використовувати як новий ETA. Pending cap 10 включає queued+dispatching+running, stored cap 100 — всі стани; коди відмови незмінні. Перевірки owner/session/CSRF/HTTPS поширюються на нові поля. Браузер опитує тільки відкриту активну задачу й показує «Приблизне очікування», не гарантію часу.

Приймання: всі три реальні задачі succeeded з валідними JSONL/геометрією; третя чекала дві native роботи й почалася після звільнення слота, переживши restart dispatcher. Перевірені FIFO/ties/concurrency/cancel/cold-start/null/owner у isolated PostgreSQL. 51 попередня задача збережена; тепер 54. [UUID, точні часи, ресурси й результати](queue-eta.md).

## Приймання Промпта 7

Наведені нижче браузерні результати — історичне приймання до появи ownership. Старий history-browser не є анонімним smoke для поточного API. Актуальне приймання з двома користувачами, isolated schema й командами prepare/browser/cleanup — у [authentication.md](authentication.md). PostgreSQL admission/history регресії адаптовано до сесій і повторно пройдено в Промпті 8.

Реальний PostgreSQL, одноразові схеми `mtw_history_<uuid>` / `mtw_test_<uuid>` з окремими тимчасовими файлами. Доступ до public jobs у цих HTTP-тестах замінено на isolated Session; decoder, dispatcher і SLAM тестами не запускаються. Після тестів схеми й власні тимчасові файли прибрані.

```powershell
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest -v test_history_pg test_scheduler_pg
wsl -d Ubuntu --exec bash scripts/build-ui
node frontend/tests/history-browser.mjs
```

- **12/12 backend tests**: 7 нових HTTP/PG перевірок admission/history та 5 наявних reservation/slot регресій. Біля cap=2 з одним записом обидва POST одночасно дійшли до SQL admission (barrier), відповіді 202/429, фінальний count=2, залишився тільки каталог прийнятої задачі.
- Усі 7 станів враховані в stored cap; pending перевірений окремо. При count=3/cap=1 історія, стан, snapshot, result і cancel доступні; новий POST відхилено, старі файли збережено.
- 27 записів, з них 25 з однаковим часом, page_size=7: 4 сторінки, точний очікуваний порядок, 27 унікальних id без пропусків. Порожній набір, остання/позамежна сторінка, 14 некоректних наборів параметрів перевірені. List не читає артефакти.
- Реальний частковий запис 1 048 576 байтів перед помилкою 413: UUID-каталог прибрано; порожній файл і невалідні параметри також не залишають записів/файлів. Відмова stored quota після upload перевірена окремо.
- **Frontend build + TypeScript успішні, 6/6 unit tests**. Включено machine code і зрозумілий український текст stored/pending помилок.
- **Chrome 156.0.8078.80 / Playwright, тільки GET до localhost**: усі 51 задача доступні на сторінках 20/20/11, ліміт 100 видимий. Перехід вперед/назад та refresh списку не скидають відкритий результат і не перезапускають його запити. Наявний `ad174e33-fa00-467e-9909-e580bbac0e6f` (360 кадрів, 2730 точок) і старий `e8688279-ad66-42af-b7ec-3ce672547406` (798 кадрів, 2780 точок) відкриті з preview/картою; старий результат пережив reload. Нових задач у public не створено.

Локальні докази поза Git: `/home/demus/.local/share/monotrajectoryweb/web/reports/prompt7-backend-tests.log`, `prompt7-browser/report.json`, `prompt7-browser/history-old-result.png`, `prompt7-preservation-before.json`, `prompt7-preservation-after.json`. Звірено всі колонки 51 запису та підписи каталогів (імена, розміри й mtime файлів): без змін. SHA-256 п'яти R5 reports і п'яти CV binaries також незмінні; 0 активних задач, тестових схем не залишилося. Повного повторного хешування всіх медіа не проводили.

Перед цільовими тестами C: 20 407 566 336 байтів (19,01 GiB), native 1 010 849 157 120 байтів; після основних перевірок C: 20 397 445 120 байтів (19,00 GiB), native 1 010 847 670 272 байти. Це поточні заміри, не оцінка причини зміни диска. Резерв 5 GiB збережено; CV запусків/перезбирання й змін Windows/WSL не було.

Перевірений код і тести: локальний коміт `5232b93` — `feat: cap stored jobs and paginate API and UI history`. Автор і комітер: `versal217 <versaldata@gmail.com>`, ефективні ідентичності перевірені перед комітом. Стороння заміна файла промптів і файл `Release` не включені; push/Publish/Sync не виконувалися.
