# Поточний локальний API: задачі та історія

Контракт Промпта 7, перевірено 03.11.2026. Вхід через Nginx: `http://127.0.0.1:8080`. Усі адреси нижче відносні. Прототип локальний; автентифікація, власність і персональні квоти ще заплановані. Історичні відповіді Промпта 3 не замінюють цей контракт.

## Ліміти та створення

`GET /api/config` повертає `limits`, `poll_interval_ms`, `camera_model`, `scale` та `authentication`. У `config/local.toml`, секція `[limits]`:

```toml
stored_jobs = 100
queued_jobs = 10
```

`stored_jobs` — додатне ціле, глобальна кількість **усіх** записів у PostgreSQL, незалежно від стану. `queued_jobs` — окрема межа незавершених записів: queued, dispatching, running. Успішні, failed, cancelled і timed_out також витрачають stored quota. Конфігурацію API перечитує при старті; зміну ліміту застосовують перезапуском API. Ліміт не є політикою видалення й не гарантує обсяг медіа в байтах.

`POST /api/jobs` приймає multipart `video` та текстове JSON-поле `parameters` із явним `calibration`. Успіх — `202`, метадані задачі, `id`, `status_url`; обчислення запускає dispatcher окремо. Відео й калібрування описані у [video-worker.md](video-worker.md). Попередні обмеження файлу/декодування/execution та дискові резерви збережені.

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

Порядок: `created_at DESC, id DESC`. UUID — детермінований другий ключ, включно з однаковими timestamps. `total` та `items` одного запиту читаються в одній read-only REPEATABLE READ транзакції. Offset-пагінація гарантує відсутність пропусків/дублів на незмінному наборі; між різними запитами нові записи можуть зсунути сторінки. Довготривалої замороженої сесії історії немає.

Відповідь `200` для поточних 51 задач:

```json
{
  "items": ["метадані задач; тут скорочено"],
  "total": 51,
  "page": 1,
  "page_size": 20,
  "total_pages": 3,
  "has_next": true,
  "has_previous": false,
  "stored_job_limit": 100
}
```

`items` містить об'єкти метаданих: id, owner_id, status/stage, кількість кадрів, timestamps, server_id, cancel_requested, error, worker_pid, execution_seconds, parameters, has_result, has_snapshot, eta_seconds. Повної геометрії, snapshot чи JSONL у списку немає; артефакти для list не читаються.

Порожня БД: `items=[]`, `total=0`, `total_pages=0`, обидва прапорці false. Сторінка за межами набору дає `200`, порожній `items`, незмінний total/total_pages; `has_previous=true`, якщо page>1 і total>0. Некоректне число/межа — `422` validation detail. Невідомі або повторені параметри — `422`, код `invalid_pagination_parameter`. Старий `?limit=50` більше не підтримується. Відповідь тепер об'єкт із `items`, а не старий масив; frontend оновлено разом із API.

```powershell
curl.exe "http://127.0.0.1:8080/api/jobs?page=1&page_size=20"
curl.exe "http://127.0.0.1:8080/api/jobs?page=3&page_size=20"
```

## Контракти перегляду й скасування

| Запит | Поведінка |
| --- | --- |
| `GET /api/jobs/{id}` | Метадані задачі; 404 для відсутнього id |
| `GET /api/jobs/{id}/snapshot` | Невеликий latest snapshot; null-пози/tracking/map/segment збережені; JSONL не сканується |
| `GET /api/jobs/{id}/frames/{frame_id}` | Опублікований PNG, `X-Frame-Id`; 404, якщо preview недоступний |
| `GET /api/jobs/{id}/result` | Manifest і URL артефактів; 409 до succeeded, 503 якщо файл manifest відсутній |
| `GET /api/jobs/{id}/artifacts/{name}` | trajectory.json, map_points.json, snapshots.jsonl або native-summary.json для succeeded; великий вміст завантажується окремо |
| `POST /api/jobs/{id}/cancel` | 202 з метаданими; terminal task не запускається повторно; для running запит обробляє compute |

Ці маршрути та алгоритми CV у Промпті 7 не змінювалися. [R5 і cancel/timeout isolation](parallel-processing.md) залишаються окремими доказами.

## Приймання Промпта 7

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
