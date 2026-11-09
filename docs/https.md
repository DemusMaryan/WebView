# Промпт 9: локальний HTTPS

Перевірено **03.11.2026**. Застосунок і користувацький API: **https://127.0.0.1:8443**. Наявний проєктний Nginx у WSL слухає лише 127.0.0.1, підтримує **TLS 1.2/1.3**. HTTP:8080 перенаправляє GET/HEAD на цей canonical origin зі шляхом і query; інші методи дають **426**, без проксіювання входу/upload/cancel через HTTP.

**Довіру Windows/WSL/браузера не змінювали.** Curl із явним CA перевірив ланцюжок і hostname без `-k`. Звичайний ізольований Chrome без override отримав `ERR_CERT_AUTHORITY_INVALID`. Функціональні тести двох браузерних контекстів пройшли з `ignoreHTTPSErrors: true`; це підтверджує роботу інтерфейсу, але не довіру браузера. Інструкція ручної довіри наведена нижче й агентом не виконувалася.

## Підготовка й запуск

PowerShell у поточному репозиторії:

```powershell
wsl -d Ubuntu --exec bash scripts/setup-https
wsl -d Ubuntu --exec bash scripts/start
wsl -d Ubuntu --exec bash scripts/status
```

У поточному середовищі сертифікати вже створені й HTTPS працює. `setup-https` не встановлює пакети й не збирає CV. Загальний `scripts/setup` також готує TLS у своєму native runtime. Повторні setup/start зберігають існуючі CA й leaf; `start` вимагає валідний комплект, самостійно не генерує заміну CA та не переходить на HTTP.

При оновленні вже запущеного старого API потрібно один раз перезапустити лише API, щоб він прочитав HTTPS/Origin/proxy налаштування:

```powershell
wsl -d Ubuntu --exec bash scripts/stop api
wsl -d Ubuntu --exec bash scripts/start api nginx
```

`start` перевіряє сертифікат, ключ, SAN, строк дії й canonical settings до запуску сервісів. Кандидат Nginx-конфігурації проходить `nginx -t`, потім атомарно замінює active config і reload виконується через PID власного master. Відсутній, прострочений, невідповідний ключ або неправильний SAN — зрозуміла помилка без HTTP fallback. Останню добу чинності leaf вважаємо сигналом для явного renewal. `status` перевіряє файли та HTTPS /health через власний CA, без глобальної довіри.

Для перевірки файлів або явного поновлення лише сервера з тим самим CA:

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m mtw.tls check
wsl -d Ubuntu --exec bash scripts/setup-https --renew-server
wsl -d Ubuntu --exec bash scripts/start nginx
```

Неповну CA-пару не замінюємо автоматично: потрібно відновити оригінал. Приватний CA key потрібен для issuance/renewal; Nginx використовує тільки server key. Нової міграції БД або нових залежностей немає.

## Сертифікати та приватні файли

Native каталог: `/home/demus/.local/share/monotrajectoryweb/web/tls` (0700), поза Git/OneDrive. Фізичний backing volume WSL VHDX залишається C:.

| Файл | Призначення |
| --- | --- |
| `ca.key.pem` | Приватний ключ окремого локального CA, 0600 |
| `ca.crt.pem` | Публічний CA для curl/OpenSSL |
| `ca.crt.cer` | Та сама публічна CA у DER для ручного Windows import |
| `server.key.pem` | Приватний server key, 0600 |
| `server.crt.pem` | Leaf із SAN DNS:localhost та IP:127.0.0.1, EKU serverAuth |

RSA 3072 / SHA-256. CA: **03.11.2026 15:10:09 UTC → 31.10.2036 15:10:09 UTC** (3650 днів). Leaf: **03.11.2026 15:10:12 UTC → 03.11.2027 15:10:12 UTC** (365 днів).

SHA-256 fingerprint CA:

```text
93:D4:4B:CD:A1:45:00:C8:8B:88:73:BA:D3:63:6D:F6:67:F0:4F:3D:90:24:EF:EE:C1:CF:55:1B:24:8D:B4:69
```

Приватні ключі, PEM/DER сертифікати й runtime Nginx-конфігурація не додаються до Git. Проєктний config містить приватний proxy key, тому має 0600 і каталог 0700; файли /etc/nginx не змінені.

## Origin, cookies та приватний backend

`config/local.toml`: `ports.https=8443`, `tls.canonical_origin=https://127.0.0.1:8443`, `auth.cookie_mode=https`, єдиний allowed_origin — canonical URL. HTTPS GET до localhost:8443 має валідний SAN, але перенаправляється на canonical IP URL. Для портів 8000/API, 8081/balancer, 8101/8102/compute збережено приватні loopback HTTP listeners.

Сесійний cookie **Secure + HttpOnly + SameSite=Strict**. Bootstrap оновлює також старий dev-cookie до Secure без продовження expires_at у БД. CSRF/Origin перевірки, expiry/logout і всі ownership перевірки збережені; 401 без сесії, 404 для чужого job_id. Користувацькі `/api/*` на прямому HTTP API відхиляються **400 https_required**, навіть із підробленим X-Forwarded-Proto.

Uvicorn API запускається з **proxy_headers=False**. Nginx перезаписує Host/X-Forwarded-Host/Proto та додає окремий X-MTW-Proxy-Key. ASGI приймає forwarding тільки від налаштованої адреси 127.0.0.1 з правильним приватним ключем і canonical authority; IP сам по собі не є доказом проксі. Ключ — native `web/proxy-key` (0600), не передається браузеру й не пишеться в access log. Інакше scheme залишається HTTP і user API відмовляє.

Службовий X-MTW-Service-Key залишається окремим від proxy credential і browser session. Dispatcher→Nginx:8081→compute та прямий compute перевіряють service key; валідний ключ не обходить перевірку reservation. Health API лишається мінімальним службовим HTTP probe, без доступу до задач.

Frontend використовує відносні API/artifact/video/preview URL. RGB image — blob:https після HTTPS fetch. У браузері перевірено відсутність HTTP resources/links і mixed-content повідомлень, карту/траєкторію та reload результату; CV алгоритми не змінені.

## Curl і перевірки

Без зміни довіри, з явним CA; `-k` не використовується:

```powershell
wsl -d Ubuntu --exec curl --cacert /home/demus/.local/share/monotrajectoryweb/web/tls/ca.crt.pem https://127.0.0.1:8443/health
wsl -d Ubuntu --exec curl --cacert /home/demus/.local/share/monotrajectoryweb/web/tls/ca.crt.pem --location https://localhost:8443/health
wsl -d Ubuntu --exec curl --silent --dump-header - --output /dev/null "http://127.0.0.1:8080/api/jobs?page=2&page_size=20"
```

| Реальна перевірка | Результат |
| --- | --- |
| HTTPS IP та hostname localhost з `--cacert` | curl exit 0, ssl_verify_result=0; hostname localhost дає canonical 308 |
| Примусові TLS 1.2 / 1.3 | обидва health 200, peer protocol додатково перевірено SSL socket |
| TLS 1.1 client | peer відхилив: protocol version alert |
| Curl без проєктного CA | exit 60: CA не довірений стандартному curl |
| HTTP GET /api/jobs?page=2&page_size=20 | 308 Location https://127.0.0.1:8443/api/jobs?page=2&page_size=20 |
| HTTP POST login / прямий API HTTP / spoofed forwarding | 426 / 400 / 400 |
| HTTPS без user session / інший власник | 401 / 404 |
| Register/login/me/logout, incorrect password | 201/200/200/204; неправильний пароль 401 |
| Без CSRF / з HTTP Origin під HTTPS | 403 / 403 |
| Own preview/result/artifact/video Range | 200/200/200/206 через HTTPS із CA verification |
| Service key health через balancer та обидва compute | 200; execute без ключа 401, із ключем та неіснуючим reservation 409, без worker |
| Повторні setup/start | CA, leaf та приватні файли незмінні, API PID 15996 / Nginx PID 478 не дублюються |

Автоматизовані перевірки, без dispatcher/SLAM у тестовій схемі:

```powershell
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest -v test_tls test_auth_pg test_history_pg test_scheduler_pg
wsl -d Ubuntu --exec bash scripts/build-ui
```

**34 унікальні backend tests пройшли**: початкові 33, потім фінальний TLS-набір із доданим CLI missing-certificate test (9 TLS cases). Є перевірки wrong SAN, expiry, mismatch, permissions, missing CA/leaf, CA reuse/leaf renewal, реального startup CLI, довіри proxy, upgrade cookie й регресії R3/R4/R5. **7 frontend tests, TypeScript і Vite production build успішні.**

Для повтору HTTPS/curl/browser функцій — isolated runtime/schema, тимчасові API/Nginx на **8444/8200**, HTTP redirect 8180, без compute/dispatcher. Підставити ROOT з prepare, cleanup виконати також при невдалому тесті:

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/auth_browser_fixture.py prepare
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/https_checks.py --fixture ROOT
node frontend/tests/auth-browser.mjs ROOT
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/auth_browser_fixture.py cleanup --root ROOT
```

Curl провів **32 запити**, паролі й CSRF подані через stdin, cookie jar/headers/body — лише у приватному тимчасовому каталозі, прибраному після перевірки; секрети не потрапили до argv/log/report. Browser — Chrome 156.0.8078.80, 03.11.2026 15:23:13–15:23:18 UTC: два незалежні contexts, Secure/HttpOnly/Strict, login/logout/register, owner history 21/1, чужі шляхи/cancel, CSRF, preview/карта/reload. Використано архівні результати як access fixtures, не нове обчислення.

Докази поза Git: native `web/reports/prompt9-backend-tests.log`, `prompt9-curl-checks.json`, `prompt9-browser/report.json`, `prompt9-browser/owner-{a,b}-result.png`, `prompt9-preservation-before.json`, `prompt9-live-state.json`.

Усі **51 записи, власники й файлові каталоги** незмінні; перевірено повні рядки й file signatures (імена/size/mtime), не повторне hashing усіх медіа. User accounts також незмінні (0 на момент перевірки). П'ять CV binaries і п'ять R5 reports мають ті самі SHA-256; 100 stored / 10 pending збережено. Тестові schemas/процеси/файли прибрані. C: **11 750 014 976 → 11 735 019 520 байтів** (10,94→10,93 GiB), native free після 1 010 477 682 688; резерв 5 GiB збережений. RAM/commit не були gates; VHDX/pagefile та системні налаштування не змінювалися.

## Ручна довіра CA — виконує лише користувач за власним рішенням

Ці дії **не виконувалися** й не входять до setup/start. Довіряти треба саме цьому окремому CA після звірки fingerprint, а не server certificate. Приватні `.key.pem` не імпортувати й не передавати.

Для Windows/Chrome/Edge можна перевірити SHA-256 публічного DER файла з PowerShell, потім вручну імпортувати його до **CurrentUser\Root**:

```powershell
$mtwCaFile = '\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\web\tls\ca.crt.cer'
Get-FileHash -Algorithm SHA256 -LiteralPath $mtwCaFile
# Лише після звірки з fingerprint вище, якщо вирішено довіряти цьому CA:
Import-Certificate -FilePath $mtwCaFile -CertStoreLocation Cert:\CurrentUser\Root
```

Альтернатива — certmgr.msc → Trusted Root Certification Authorities → Certificates → Import публічного ca.crt.cer. Після ручного імпорту користувач повторно перевіряє https://127.0.0.1:8443 у браузері без overrides; за потреби сам перезапускає браузер. Цей позитивний сценарій системної довіри ще **не перевірений**. [Microsoft Import-Certificate](https://learn.microsoft.com/en-us/powershell/module/pki/import-certificate).

Для WSL достатньо `curl --cacert` — він не змінює trust store. Якщо користувачу окремо потрібна системна довіра Ubuntu, він може вручну виконати нижче; це незалежно від Windows/браузера:

```bash
sudo install -m 0644 "$HOME/.local/share/monotrajectoryweb/web/tls/ca.crt.pem" /usr/local/share/ca-certificates/monotrajectoryweb-local-ca.crt
sudo update-ca-certificates
```

Агент ці команди не запускав. [Ubuntu root CA trust](https://ubuntu.com/server/docs/how-to/security/install-a-root-ca-certificate-in-the-trust-store/). Немає глобального вимкнення certificate verification або змін browser settings. На момент Промпта9 черга/ETA, автомасштабування й адмінпанель були наступними етапами; тепер вони прийняті у Промптах10–12, див. [фінальне приймання](final-acceptance.md).

Перед комітом повторний `scripts/status` знову підтвердив listener_verified=true, ті самі API/Nginx PID та два вільні compute. Останній окремий Windows `Get-PSDrive C` показав **19 791 806 464 байти (18,43 GiB)**; це пізніший замір, джерело фонового збільшення вільного місця в цьому етапі не досліджувалося. Bash syntax checks setup/setup-https/start/status успішні; для нового setup явно перевіряються наявні /usr/sbin/nginx та OpenSSL, залежності не перевстановлювалися.

Налаштування звірені з первинними джерелами: [Nginx TLS module](https://nginx.org/en/docs/http/ngx_http_ssl_module.html), [Uvicorn proxy settings](https://uvicorn.dev/settings/), [OpenSSL hostname/IP verification](https://docs.openssl.org/3.0/man1/openssl-verification-options/).

Перевірений код, конфігурація, скрипти й тести збережені локальним комітом **7c91c05** — `feat: serve local app over TLS with verified proxy forwarding`. Автор і комітер перевірені: `versal217 <versaldata@gmail.com>`. Сторонню заміну файла промптів та Release не включено; історію не переписано. Push не виконувався.
