# Промпт 8: локальні облікові записи й власність задач

**Продовження Промпта12:** `users.is_admin` default false, роль тільки через локальну CLI `scripts/admin-role --login EXISTING_LOGIN --grant/--revoke`. Вона перевіряється backend на кожному `/admin` і admin API-запиті; admin не обходить власність звичайних job/artifact шляхів. Register/login/me/session повертають is_admin, клієнт не може його задати. У робочій БД admin0, явного login для призначення користувач не надавав. [Команди, isolated тести та моніторинг](admin-monitoring.md). Історичні межі Промпта8 нижче не є поточним статусом R8.

**Поточний запуск після Промпта 9 — https://127.0.0.1:8443**, Secure-cookie та HTTPS-only user API. Нижче збережено історичні результати Промпта 8 з localhost HTTP dev; його HTTP URL й cookie_mode не є поточною конфігурацією. [HTTPS setup/start, curl-перевірки та ручна довіра CA](https.md). Сесії, CSRF, ownership, CLI й схема БД збережені; повторні HTTPS-перевірки виконано без SLAM.

Перевірено 03.11.2026. Промпт 8 завершено в межах локальної авторизації; HTTPS — окремий наступний етап. Нативний WSL, застосунок **http://127.0.0.1:8080**. CV, ABI, shutdown і залежності ядра збережено; нових SLAM-запусків не було.

## Запуск і перший обліковий запис

У вже підготовленому середовищі з PowerShell у репозиторії:

```powershell
wsl -d Ubuntu --exec bash scripts/start
wsl -d Ubuntu --exec bash scripts/status
```

Міграцію `0003_authentication` вже застосовано до робочої БД. Для оновлення іншої підготовленої копії: зупинити API/compute/dispatcher, встановити backend lock у наявний **web-venv**, виконати міграції та UI build, запустити сервіси:

```powershell
wsl -d Ubuntu --exec bash scripts/stop api compute-1 compute-2 dispatcher
wsl -d Ubuntu --exec /home/demus/.local/share/monotrajectoryweb/web-venv/bin/pip install --only-binary=:all: -r backend/requirements.lock
wsl -d Ubuntu --exec bash scripts/migrate
wsl -d Ubuntu --exec bash scripts/build-ui
wsl -d Ubuntu --exec bash scripts/start
```

Цей етап додав лише готові backend wheels: argon2-cffi 25.1.0, argon2-cffi-bindings 26.1.0, cffi 2.1.1, pycparser 3.12. Інші backend версії та CV залежності не змінювалися. Ключ службового доступу створює setup/start, якщо його ще немає; status повідомляє про відсутній ключ без його створення.

На сторінці натиснути **«Створити обліковий запис»**, ввести власні логін і пароль. Логін нормалізується trim/lowercase: 3–32 ASCII-символи, перший a–z/0–9/_, далі також . і -. Пароль 12–128 символів, не обрізається й не нормалізується. Після реєстрації користувач уже увійшов; ім'я та «Вийти» показуються у верхній панелі. У робочій БД під час приймання користувачів не створювали — немає типового облікового запису чи пароля.

## Сесії та CSRF

- Паролі у `users.password_hash` — **Argon2id** через `argon2-cffi.PasswordHasher`, версію зафіксовано. Перевірка бібліотечна, за потреби hash оновлюється при вході. Немає plaintext паролів у таблиці, логах або звітах. Одночасні Argon2 операції обмежені двома потоками; це не персональна квота задач.
- Cookie `mtw_session`: криптографічно випадкові 32 байти, HttpOnly, Path=/, **SameSite=Strict**. БД `auth_sessions` містить лише SHA-256 із доменним префіксом, user_id, created_at/expires_at/revoked_at. Строк перевіряється сервером при кожному API-запиті; cookie Max-Age не є єдиним захистом. Стандартна сесія — 28 800 с, pre-auth — 600 с.
- `GET /api/auth/session` повертає user або null і `csrf_token`; за потреби створює коротку анонімну серверну сесію для захисту login/register. CSRF — одностороння SHA-256 похідна випадкового cookie з іншим доменним префіксом; вона не розкриває session cookie. Після входу/реєстрації сесія ротується, попередня відкликається. Logout відкликає запис і прибирає cookie; повтор старого cookie дає 401.
- POST/PUT/PATCH/DELETE в API потребують **X-CSRF-Token** з поточної сесії та **Origin** з явного списку. Це стосується також login/register/logout і multipart upload. Інший Origin, Sec-Fetch-Site=cross-site, відсутній або неправильний токен — 403. BrowserSecurity перевіряє доступ до розбору multipart. CORS не увімкнено. Host допускає тільки localhost/127.0.0.1.
- Усі авторизовані API-відповіді, включно з файлами, мають private/no-store. Помилки валідації облікових даних не віддзеркалюють password/input. Access logs не містять тіл, Cookie, CSRF чи службового ключа. Frontend тримає CSRF у пам'яті, без localStorage; реальний session cookie недоступний JavaScript.

`config/local.toml`, `[auth]`: явний **cookie_mode="localhost-http"**, allowed_origins для localhost/127.0.0.1:8080. Це dev-режим без Secure, дозволений тільки для loopback HTTP. Режим **https** вимагає HTTPS origins, завжди встановлює Secure і відхиляє видачу cookie через HTTP. Політику Secure перевірено в ASGI-тесті; TLS listener/сертифікат у цьому етапі не налаштовували.

Реалізацію бібліотечного hashing та підхід CSRF звірено з [argon2-cffi API](https://argon2-cffi.readthedocs.io/en/stable/api.html) і [OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html). SameSite доповнює серверний CSRF-захист.

## Власність та службовий шлях

Новий Job.owner_id задає API з перевіреної сесії. Owner у parameters або зайве multipart-поле відхиляються 422. List/total обмежені власником, з попереднім детермінованим порядком і page/page_size. UI показує **«Ваших задач»** та окремий глобальний ліміт. Count для admission залишається глобальним: **100 stored / 10 pending**, включно з чужими й безвласними записами. Персональних квот немає.

State, snapshot, preview, source video, result, усі дозволені artifacts та cancel перевіряють owner у SQL. Без входу — 401, для чужого або безвласного UUID — 404. Немає статичного alias до jobs/runs; зовнішній Nginx відхиляє /jobs/, /artifacts/, /internal/, /execute.

Dispatcher надсилає **X-MTW-Service-Key** через Nginx:8081 до compute:8101/8102. Кожен compute перевіряє ключ також при прямому зверненні; browser cookie його не замінює. Ключ випадковий, зберігається тільки в native `web/service-key` із правами 0600, не в Git, аргументах процесу чи логах. Немає endpoint для його видачі. Після ручної ротації файла потрібно перезапустити dispatcher/compute. Наявний reservation token, повторюваний handoff, PostgreSQL slots і два глобальні обчислення збережено.

Наявність ключа не дозволяє довільний запуск: ще потрібен валідний reservation. Ізольований тест перевіряє прийняття **owned** reservation без browser cookie та idempotent повтор; native worker у цьому тесті замінено тестовим callback, SLAM не запускається. Справжні R5 результати залишаються доказами Промпта 6.

## Старі 51 задача: тільки явне призначення

Усі 51 записи й файли збережено; owner_id залишився **NULL**. Міграція й реєстрація нікого не призначають власником. Новий користувач спочатку побачить порожню історію. Це очікувана зміна доступу, а не видалення даних.

Після створення потрібного облікового запису локальний оператор може виконати наступні команди, замінивши EXISTING_LOGIN на його точний логін:

```powershell
wsl -d Ubuntu --exec bash scripts/assign-unowned --login EXISTING_LOGIN --all-unowned --dry-run
wsl -d Ubuntu --exec bash scripts/assign-unowned --login EXISTING_LOGIN --all-unowned
```

CLI в одній транзакції змінює тільки owner_id IS NULL, не перепризначає вже owned задач і не змінює медіа. Без --login/--all-unowned або для відсутнього користувача — помилка без призначень. **У робочій БД цю команду не виконували**, оскільки користувач не вказував власника. Реальний CLI subprocess перевірено в isolated schema: dry-run=0 змін, призначено 2 unowned, чужий owned запис збережений.

## Перевірки й докази

```powershell
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest -v test_auth_pg test_history_pg test_scheduler_pg
wsl -d Ubuntu --exec bash scripts/build-ui
```

**23 backend checks пройшли:** основні 20 + 3 додаткові цільові тести service acceptance/key permissions/status. Ізольовані PostgreSQL schemas, тестові каталоги, без dispatcher/SLAM. Перевірено реєстрацію/дублі, правильний/хибний пароль, rotation/logout/replay/expiry, CSRF до й після входу, Secure policy, sanitized validation, власність усіх маршрутів, підміну owner, глобальні stored/pending caps і concurrent POST, owner pagination із timestamps ties, CLI та upgrade 0002→0003 без зміни legacy рядка. Схеми після тестів прибрано.

Браузерний сценарій використовує власний **8180→8200** Nginx/API, isolated PostgreSQL schema; dispatcher/compute не стартують. Підготовка повертає ROOT без секретів; підставити цей шлях у два наступні виклики:

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/auth_browser_fixture.py prepare
node frontend/tests/auth-browser.mjs ROOT
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/auth_browser_fixture.py cleanup --root ROOT
```

Прибирання потрібне й при відмові браузера. Helper відмовляється, якщо його порти зайняті, перевіряє PID/start ticks/marker і видаляє лише власний native каталог та schema. Виявлену гонку початкового читання PID усунено bounded очікуванням; перерваний fixture прибрано, фінальний prepare/browser/cleanup пройшов. Test passwords існують у пам'яті як похідні тимчасового приватного fixture seed; до БД потрапляє тільки Argon2 hash. Seed і всі тестові дані прибрані.

**Chrome 156.0.8078.80 / Playwright, два незалежні контексти, тільки localhost:** owner A бачить 21 запис (20/1), B — 1; legacy unowned прихований від обох. Реальні архівні результати Промптів 3/6 використані як файлові fixtures з новими test UUID; це перевірка доступу, не новий CV прогін. Обидві карти/preview відкрилися, own result пережив refresh/reload. UI registration, wrong password, logout пройшли; вихід A не зачепив B. HttpOnly/Strict/dev-cookie та недоступність session cookie JavaScript підтверджені.

| Реальний HTTP запит у перевірці | Відповідь |
| --- | --- |
| Anonymous GET /api/jobs | 401 |
| Інший користувач: state/snapshot/preview/video/result/artifact/cancel | 404 на кожному маршруті |
| Own cancel без CSRF / з чужим Origin | 403 / 403 |
| Own terminal cancel з CSRF | 202, без повторного запуску |
| Прямі /execute:8101/:8102 і через Nginx:8081 без ключа | 401 / 401 / 401 |
| Службовий health із ключем на обох compute/балансувальнику | 200 |
| Через робочий Nginx з ключем, неіснуючий reservation | 409; access log підтвердив upstream 127.0.0.1:8101, жоден процес не стартував |
| Невідомий Host через робочий Nginx до API | 400; Nginx передає Host для TrustedHost перевірки |

Останній службовий probe: `2026-11-03T14:18:53+00:00`, job marker `032491de-ee40-4a5f-953e-afc8a6160f5b`. Секретні заголовки й body не записувалися. **7 frontend unit tests, TypeScript та Vite build успішні.**

Докази поза Git: native `web/reports/prompt8-backend-tests.log`, `prompt8-browser/report.json`, `prompt8-browser/owner-{a,b}-result.png`, `prompt8-preservation-before.json`, `prompt8-preservation-and-service-check.json`. Звірено всі колонки 51 job та підписи файлових каталогів (імена/size/mtime), SHA-256 п'яти R5 reports і п'яти CV binaries: незмінні. Повного повторного hashing усіх медіа не проводили. Робочих users=0, jobs=51, unowned=51, active=0. Служби працюють; міграція 0003 застосована.

Перед початком C: 20 425 945 088 байтів (19,02 GiB), під час фінальної службової перевірки 11 879 206 912 (11,06 GiB), native free 1 010 479 284 224. Резерв C: 5 GiB збережено. Ці заміри не встановлюють причину фонової зміни C:; загального аудиту й змін Windows/WSL/VHDX/pagefile не робили. RAM/commit не використовувалися як gates.

## Межі

Лише loopback HTTP dev. HTTPS/TLS, OAuth, пошта, відновлення/зміна пароля, адмінпанель, персональні квоти й автоматичне призначення legacy не реалізовані. Немає захисту рівня production від перебору/масової реєстрації або автоматичного retention старих auth_sessions. Локальний OS/DB оператор є довіреним: файл службового ключа й прямий SQL не є користувацьким API. Наступні етапи автоматично не запускаються.

Перевірений код, міграція, CLI та тести: локальний коміт `e512c16` — `feat: add local sessions and enforce job ownership`. Автор і комітер: `versal217 <versaldata@gmail.com>`, перевірені в середовищі Git перед комітом. Вхідні файли промптів, `Release`, WebProject.txt та R5 докази до змін не включені. Секретів, медіа чи БД у коміті немає.
