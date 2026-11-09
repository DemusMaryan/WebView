# Промпт 12: панель адміністратора — R8

> Приймання R8 від 05.11.2026: наведені counts/PID/CPU/RSS — вимірювання того запуску, не live status. Поточний стан показують `scripts/status` та панель. Фінальне HTTPS UI/restart приймання завершено у [Промпті13](final-acceptance.md). `admin_evidence.py` має конкретний baseline71 jobs і не запускається автоматично на новій БД.

**Перевірено 05.11.2026.** Захищена read-only панель: **https://127.0.0.1:8443/admin**. Роль зберігається у PostgreSQL, призначається лише явно локальним оператором. У робочій БД адміністраторів **0**: користувач не вказував облікового запису для призначення. Браузерне приймання проведено в disposable schema на https://127.0.0.1:8444/admin з тими самими API/UI та справжнім production monitoring cache. Жодних admin-прав робочому test account не надавали.

## Доступ і запуск

Міграція **0006_admin_role** додає `users.is_admin NOT NULL DEFAULT false`; попередні облікові записи й нові реєстрації залишаються звичайними. Credentials забороняє зайві поля, включно з role/is_admin; JobParameters також не приймає is_admin. `GET /api/auth/session`, `/me` та відповіді входу містять `user.is_admin`. BrowserSecurity завантажує користувача з БД при кожному запиті, тому відкликання ролі діє на вже видану сесію. Backend перевіряє роль для кожного admin-endpoint. HTTPS, Secure/HttpOnly/Strict, CSRF та власність звичайних job/artifact маршрутів збережені; admin не отримує обхід власності.

| Запит до `/admin`, `/admin/`, `/api/admin/monitoring` | Результат |
| --- | --- |
| Без авторизованої сесії через HTTPS | 401 authentication_required |
| Звичайний користувач | 403 admin_required |
| Адміністратор | 200 |
| Прямий незахищений HTTP `/admin` на API:8000 | 400 https_required |

Nginx проксіює `/admin` і `/admin/` до backend з чинними приватними forwarding headers, а не віддає незахищений SPA fallback. HTTP:8080 лишається GET/HEAD redirect до canonical HTTPS. Файли JS/CSS загальнодоступні, але не містять метрик/секретів. Єдиний admin API — **GET /api/admin/monitoring**; process actions, shell, видалення та зміни серверів через UI відсутні.

З поточного PowerShell workspace, для вже підготовленого native WSL/PostgreSQL:

```powershell
wsl -d Ubuntu --exec bash scripts/migrate
wsl -d Ubuntu --exec bash scripts/stop api
wsl -d Ubuntu --exec bash scripts/start api monitor
wsl -d Ubuntu --exec bash scripts/status
```

Звичайний повний `scripts/start` тепер включає project monitor. `scripts/stop` коректно зупиняє його поряд з іншими службами. Це окремий root collector через явний WSL helper `scripts/monitor`, без системного systemd unit, sudoers, глобальних Windows/WSL settings або нових пакетів. Окремий monitor дозволяє спостерігати стан autoscaler незалежно від його роботи.

Після створення потрібного локального облікового запису оператор може виконати, **замінивши EXISTING_LOGIN на точний існуючий login**:

```powershell
wsl -d Ubuntu --exec bash scripts/admin-role --login EXISTING_LOGIN --grant
wsl -d Ubuntu --exec bash scripts/admin-role --login EXISTING_LOGIN --revoke
```

Команда в одній транзакції змінює тільки роль явно заданого користувача. Без --login, без grant/revoke або для відсутнього login — помилка без змін. Немає автоматичного першого адміністратора, зв'язку з Git-ідентичністю чи HTTP endpoint призначення ролі. **Ці команди в public schema не виконувалися.** Після зміни ролі перезавантажте UI; серверна перевірка наступного запиту вже використовує нову роль.

## Джерела та шкала метрик

Collector працює одним довгоживучим процесом, приблизно кожні **2 с** публікує атомарний native `web/monitoring/latest.json` (0600). API читає тільки bounded cache, а не /proc, JSONL або дерево артефактів. Сесія/роль перевіряються окремо в PostgreSQL. JSON-відповідь містить sampled_at, age_seconds, stale, environment, services, servers, capacity, queue, processes та явні ознаки обрізання.

| Дані | Джерело / значення |
| --- | --- |
| API, dispatcher, Nginx, autoscaler, monitor, baseline compute | Власні PID records + PID/start_ticks/marker; HTTP health для API/Nginx/compute. `process_present` для dispatcher/controller означає наявність процесу, а не гарантію працездатності його циклу. |
| PostgreSQL | Project postmaster.pid, перевірка argv усередині collector та SQL-доступність. Дочірні процеси БД також показані. |
| Compute/слоти | R7 compute_servers registry, свіжість heartbeat, окремий health; active Job із server_id. Base2/max3; один слот на server. |
| Черга | Глобальні queued/cancel_requested=false записи PostgreSQL у FIFO created_at/id; positions та приблизний ETA через чинний R6 estimator з actual ready capacity. |
| Процеси | Власні service roots і їхні descendants, плюс перевірені project nspawn supervisors. Один запис на host PID, без дублювання за server/namespace. |
| PID | host_pid завжди з host /proc; pid — останній NSpid; pid_scope=host або container:compute-3; PID namespace inode показаний окремо. |
| CPU процесу | Δ(utime+stime)/SC_CLK_TCK/Δmonotonic ×100. **100%=один логічний CPU**, багатопотоковий процес може перевищувати 100%. Час waited-for children не додається. |
| Час процесу | uptime − start_ticks/SC_CLK_TCK; CPU-секунди — накопичені utime+stime. |
| RSS | RSS pages з /proc/PID/stat × SC_PAGE_SIZE, у API байти; UI — MiB, 1 MiB=1048576 bytes. Це kernel RSS, включно зі спільними сторінками, не PSS/USS. |
| Загальні CPU/RAM | /proc/stat та /proc/meminfo одного середовища WSL. CPU 0–100% усіх logical CPU. MemTotal/MemAvailable і SwapTotal/SwapFree переводяться з kB у байти множенням на1024. Це не фізична RAM Windows. |

Джерела полів: [Linux proc_pid_stat](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html), [proc_meminfo](https://man7.org/linux/man-pages/man5/proc_meminfo.5.html). Namespace mapping використовує `/proc/PID/status` NSpid. Контейнер ділить kernel/RAM із WSL: **не підсумовуємо RSS як незалежну пам'ять трьох машин** і не показуємо три копії WSL memory total.

Перший CPU sample або PID reuse дає `cpu_percent=null`, metrics_status=warming_up. Недоступні джерела — null/unavailable, не вигаданий нуль. PID, який уже завершився між читаннями, пропускається. Snapshot застарілий після **6 с**, при reboot або від'ємному monotonic age; UI показує явне попередження й час останнього вимірювання. При помилці fetch старі дані також позначаються непідтвердженими. Polling послідовний, 2 с після відповіді; abort/cleanup припиняють його при navigation/logout, 401/403 зупиняють цикл.

Межі: максимум **256 process rows**, **8192 /proc candidates**, cache **256 KiB**, queue до10, registry до3. Жодних recursive artifact scans або JSONL reads. Health timeout0,4с, окрема SQL pool1/connection timeout1с/statement timeout750мс. Випадкові error messages, SQL URLs, environment, argv, паролі, токени й ключі в JSON не потрапляють. Source argv читається лише локально для перевірки належності/класифікації та відкидається до публікації.

Приклад після перевірок: collection_seconds=**0,0565**, відповідь **10066 bytes**, monitor PID7938, RSS82022400 bytes, CPU1,5% одного CPU в конкретному sample. Це замір, не заявлений peak або гарантія. Під час відмови collector попередній кеш залишається й старіє; API залишається доступним.

## Перевірки без SLAM

**56 різних backend tests** пройшли: первинні23, окремі32 regression cases та ще1 новий classification case (інші3 metrics cases повторені після виправлення). Пройшли ізольовані PG role/default/migration/CLI/auth/ownership/queue/slots tests, чисті CPU/RSS/cache tests і регресії R5/R6/R7/TLS. Розширену перевірку is_admin у video parameters також виконано цільово, без decoder/SLAM. **11 frontend tests та TypeScript/Vite build — OK.**

```powershell
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest -v test_admin_pg test_metrics test_auth_pg test_nginx_owner
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest -v test_autoscale_pg test_scheduler_pg test_queue_pg test_tls
wsl -d Ubuntu --exec bash scripts/build-ui
```

В isolated schemas перевірено: 401/403/200, grant/revoke на чинній сесії, default false для existing/register, відмова role/is_admin spoof, невідомий CLI login, чужі state/snapshot/result/video/preview/artifact/cancel=404 навіть адміністратору, POST monitoring=405, empty queue, busy slots, unavailable compute, missing PID/service, TTL/reboot/oversize cache, CPU first sample/PID reuse/one-core scale. Collector-side side effects у цих tests не запускають worker або контейнер. Схеми й тимчасові каталоги прибрані.

## Один реальний worker/native запуск

Evidence ROOT: `/home/demus/.local/share/monotrajectoryweb/web/reports/admin-14da9ecf049443b5b1dbe6f97504205d`.

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/admin_evidence.py prepare
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/admin_evidence.py real --root ROOT
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/admin_evidence.py verify --root ROOT
```

Prepare був виконаний **до міграції**, коли було71 terminal задач. Повторювати його або real тепер не потрібно: рівно один новий job облікований. HTTPS через Nginx, перевірка CA без -k, чинний звичайний R6 test account, без admin-role. Готовий 300-frame ролик freiburg1_xyz, 640×480/30fps з перевіреним калібруванням; відео не перекодовували.

| Показник | Факт |
| --- | --- |
| job_id / server_id | `40a914a2-0c06-4bbb-9a8e-bd3b2064ac44` / compute-1 |
| created / started / finished UTC 05.11 | 19:19:35.242 / 19:19:35.541 / 19:20:08.367 |
| worker / native host PID | 7327 / 7367 |
| Результат | succeeded, 300 frame records, 251 valid live, 192 final poses, 2746 points |
| Export cap | 100000, truncation=false |
| Worker wall | 33,582 с у manifest: decode/vocabulary/native processing/export/validation без playback pacing; manager cleanup виключено. Окремий total compute `Job.execution_seconds` — 34,591 с, із запуском child/wait/reap/cleanup; queue/upload виключено |
| Перевірка геометрії | JSONL/PTS/Twc/null/segments/quaternions/finite points і final_snapshot_consistency passed |
| Реальні метрики | 10 різних native snapshots; CPU максимум серед samples245,22% одного CPU, RSS максимум585764864 bytes |
| Звірка CPU | 9 інтервалів повторно обчислені з ticks/monotonic, усі відповідають показаному CPU з точністю округлення |
| Звірка PID | 36 зіставлень host/local PID/namespace з прямим /proc — passed |
| Після завершення | active0, worker/native відсутні, робочі RGB прибрані; результат і preview залишилися |

Кеш і прямий kernel sample беруться в різний час, тому RSS може змінитися між ними. Одиниці й парсинг RSS окремо звірені безпосередньо з kernel stat у тесті. Peak вище — лише максимум знятих samples. Захист C:5 GiB весь час збережений; RAM/commit не є gates.

## Браузер і контейнер

Playwright/Chrome **лише localhost**, два незалежні контексти, HTTPS8444. **ignoreHTTPSErrors=true**: це функціональний тест, не підтвердження системної довіри до CA. Окремі реальні curl --cacert запити на production8443 дали401 для anonymous /admin та admin API, успішно перевіривши TLS/hostname без -k; прямий HTTP API /admin дав400.

Для disposable середовища використано `auth_browser_fixture.py prepare`, потім `admin_browser_fixture.py --root FIXTURE_ROOT`: останній явно надає browser_a роль **тільки** в `mtw_browser_<uuid>` і відкриває read-only view справжнього monitoring cache. Dispatcher/SLAM у fixture не працюють. Після перевірки `auth_browser_fixture.py cleanup --root FIXTURE_ROOT` видалив лише свої processes/schema/files. Production grants не виконувалися.

```powershell
node frontend/tests/admin-browser.mjs FIXTURE_ROOT ROOT
wsl -d Ubuntu -u root --exec env MTW_RUNTIME_HOME=/home/demus PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/admin_container_probe.py --root ROOT
```

Браузер підтвердив ACL/ownership, показ фактичного native PID/CPU/RSS із response, active→released, контейнерний PID mapping, stale banner після контрольного stop monitor та його зникнення після restart. Polling припинився після переходу до задач і після logout. JS errors/mixed content/non-local requests —0.

Візуальний перегляд знайшов неправильну роль nspawn supervisor: argv містив команду дочірнього Python. Класифікація тепер враховує executable, а не тільки наявність `mtw.service` серед аргументів. Додано test; повторено **тільки короткий container display**, без нового SLAM/job:

```powershell
node frontend/tests/admin-container-browser.mjs FIXTURE_ROOT ROOT
wsl -d Ubuntu -u root --exec env MTW_RUNTIME_HOME=/home/demus PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/admin_container_probe.py --root ROOT --review
```

Фінальний probe: supervisor host8385/role container_runtime; init host8387/local1; application host8390/local4/role compute, PID namespace4026532235 проти host4026532221. Host PID у всьому списку унікальні. Registry compute-3 навмисно залишався stopped, health був healthy: це ручний короткий probe, **не новий доказ автомасштабування й не додатковий ready-slot**. Після stop remaining=[]; обидва container probes створили0 задач, початковий baseline2/compute-3 stopped відновлено.

Скріншоти поза Git, у ROOT (їх відкрито й візуально перевірено):

- `admin-idle.png` — служби, ready2/max3, порожня черга.
- `admin-active.png` — реальна native initialization, PID7367, CPU94,8%, RSS222,4 MiB; подальший tracking/progress є в real.json.
- `admin-finished.png` — звільнений слот і завершені процеси.
- `admin-container-reviewed.png` — виправлені runtime/init/application ролі, host/container PID.
- `admin-stale.png` — явне попередження при age8с.

Windows шлях до ROOT: `\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\web\reports\admin-14da9ecf049443b5b1dbe6f97504205d`.

## Збереженість та обмеження

`preservation.json`: усі71 попередні повні рядки/owner та файлові signatures names/size/mtime незмінні; старі user fields/password hashes незмінні, новий is_admin=false. Production admins0, jobs**72**, рівно1 нова задача. CV5/R5/R6/R7 SHA-256 збігаються; великі медіа повторно повністю не хешувалися. 100 stored/10 pending, base2/max3, HTTPS/session/service key, frame cleanup, CV/ABI/shutdown/dependencies збережені. Початкові Windows C:17891176448 bytes≈16,66 GiB; після перевірок17843933184≈16,62 GiB, вище резерву5. Системні налаштування, pagefile, VHDX та системну довіру не змінювали.

Остаточний свіжий замір **21:36:42 Kyiv, 05.11.2026**: Windows і WSL /mnt/c однаково **17570844672 bytes≈16,36 GiB**, native FS1009830703104 bytes. PostgreSQL/HTTPS справні, ready2/max3, active0, compute-3 stopped, monitor7938 працює. `production-access.json`: чинний звичайний account має /admin403 та admin API403, logout204; users admin0, legacy unowned51.

Докази: `real.json`, `browser.json`, `container-review-browser.json`, `container-review-probe.json`, `metric-validation.json`, `preservation.json`, `production-access.json`, `final-status.json`, `regression-tests.log` і PNG. Перед комітами перевіряються Git status/diff/секрети/автор і комітер. Сторонні файли промптів та Release не включаються.

Моніторинг локальний і sampled: дуже короткі процеси можуть виникнути та завершитися між двома вимірами. RSS не є точним приватним memory accounting; PSS/USS не збираються. Collector потребує довіреного WSL root для інших UID і container namespaces; це read-only OS observer з фіксованою програмою, не довільний shell API. Верхні межі списків мають явні truncation flags. Адмінпанель не змінює ліміти/сервіси/ролі; production-доступ потребує явного login від користувача. Фінальний етап не починався.

Перевірена реалізація — локальний коміт **5864b1b**, `feat: add role-protected admin monitoring and WSL metrics`. Автор і комітер **versal217 <versaldata@gmail.com>**, ефективні значення та джерело конфігурації перевірені безпосередньо перед комітом. Секретів/БД/медіа/screenshots у Git немає; UI screenshots і результати збережені в native ROOT. Push не виконувався.
