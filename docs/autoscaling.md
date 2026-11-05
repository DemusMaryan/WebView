# Промпт 11: systemd-nspawn і автоматичний третій compute — R7

**05.11.2026: Промпт 11 завершено, R7 перевірено.** Два зайняті native compute та черга автоматично спричинили запуск справжнього systemd-nspawn контейнера. Після isolation/health і Nginx readiness він виконав реальну задачу: 300 кадрів, 249 live-поз, 215 фінальних поз, 2726 точок. Усі чотири результати прийнятого сценарію валідні; додатковий попит не перевищив max3. Після явного stop не залишилося PID/namespace; controller знову працює з двома baseline. Початкові 54 задачі/owner/files збережено. **Тепер 71 задача: 54 + 13 з чотирьох невдалих спроб + 4 прийняті.** Усі спроби обліковані нижче, автоматичного циклу повторення тесту немає.

## Початкова можливість: успішно до 10 хвилин

Початок перевірки — **20:40:42 Kyiv (18:40:42 UTC)**. До 20:42:54 вже отримано успіх PID/mount/filesystem probe: верхня межа витраченого часу **2 хв 12 с**, менше погоджених 10 хвилин.

- WSL Ubuntu 24.04, kernel `6.6.87.2-microsoft-standard-WSL2`, systemd `255.4-1ubuntu8.18`.
- `sudo -n true` вимагає пароль, але явний `wsl -d Ubuntu -u root --exec ...` доступний. Системні sudoers/Windows/WSL settings не змінювалися.
- `unshare --mount --pid --fork --mount-proc` успішний.
- Спочатку systemd-nspawn відсутній. Додано **тільки systemd-container 255.4-1ubuntu8.18**, 417 kB download / 1431 kB installed; **0 upgraded**. Рекомендований libnss-mymachines не встановлювався. Пакет створив власний стандартний symlink machines.target; інші host services/configs агент не переналаштовував.

Фактично виконані PowerShell-команди:

```powershell
wsl -d Ubuntu -u root --exec apt-get -s install --no-install-recommends --no-upgrade systemd-container
wsl -d Ubuntu -u root --exec apt-get install -y --no-install-recommends --no-upgrade systemd-container=255.4-1ubuntu8.18
wsl -d Ubuntu -u root --exec bash scripts/container/probe-nspawn.sh
```

Результат початкового probe:

```text
HOST_PID_NAMESPACE=pid:[4026532221]
HOST_MOUNT_NAMESPACE=mnt:[4026532219]
systemd 255 (255.4-1ubuntu8.18)
CONTAINER_PID=2
CONTAINER_PID_NAMESPACE=pid:[4026532234]
CONTAINER_MOUNT_NAMESPACE=mnt:[4026532229]
PID 1 (sd-stubinit), 2 bash, 5 ps
FILESYSTEM_AND_PID_ISOLATION_OK
```

Контейнер не бачив контрольного host-only файла, запис у /usr відхилено. Це справжні окремі namespaces/rootfs, а не тільки новий PID/server_id. Новий Docker Engine, Dockerfile, Compose, kernel/WSL upgrade не використовувалися.

## Середовище та mounts

Native каталог — `/home/demus/.local/share/monotrajectoryweb/containers/compute-3/rootfs`. Rootfs — мінімальний каталог з /etc і mount points, без дублювання репозиторію, venv чи OS /usr. Host /usr та потрібні native ресурси bind-mounted read-only: `web-venv`, `prefix`, `build/exporter`, `src/ORB_SLAM3`. Репозиторій є одним read-only bind на `/opt/monotrajectoryweb`; пробіли/кирилиця у host path передаються окремими argv, без shell-конкатенації команд.

Rootfs read-only; /tmp — приватний tmpfs. Запис для обчислень дозволено в shared `web/jobs`; усі вже terminal каталоги додатково накриваються read-only binds при запуску, включно з 54 збереженими. Database URL і службовий ключ — тільки read-only файли, не literals у argv/logs/Git. Немає mount цілого C: або всієї home. Backing-volume guard всередині перевіряє C: через read-only Git mount /opt/monotrajectoryweb, який фізично залишається на тому самому C:; поріг 5 GiB не змінюється. Native storage guard 2 GiB теж збережений.

Контейнерний застосунок — UID/GID 1000, dropped capabilities, NoNewPrivs=1. PID/mount/UTS/IPC namespaces ізольовані; **kernel, host /usr і loopback network спільні**. User namespace вимкнений для сумісного доступу до native файлів UID 1000; це не VM й не повна ізоляція від довіреного локального оператора. HTTP compute-3 слухає лише **127.0.0.1:8103** зі старим окремим service key. Жодного зовнішнього HTTP listener не додано. PostgreSQL/HTTPS/trust stores і глобальний Nginx не переналаштовувалися.

Використано можливості встановленої [systemd-nspawn 255](https://www.freedesktop.org/software/systemd/man/255/systemd-nspawn.html), також [Ubuntu Noble manual](https://manpages.ubuntu.com/manpages/noble/man1/systemd-nspawn.1.html), не можливості новішого systemd.

## Перевірений application smoke, без задач

```powershell
wsl -d Ubuntu -u root --exec env MTW_RUNTIME_HOME=/home/demus PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/nspawn_smoke.py
```

`MTW_RUNTIME_HOME` — лише project-process variable для правильного розгортання `~/` під WSL root; HOME й глобальне середовище не змінюються. Root helper — `scripts/autoscaler`.

Одна конкретна помилка сумісності виправлена: голий `--no-new-privileges` дав `Failed to parse --no-new-privileges= argument: --kill-signal=SIGTERM`; для цієї версії потрібне явне `--no-new-privileges=yes`. Payload з --user фактично має PID **4**, тому controller перевіряє повернутий PID/команду і належність supervisor, не припускає PID 2.

Останній успішний smoke 05.11.2026, вже з Nginx та linker check без SLAM:

| Об'єкт | Значення |
| --- | --- |
| container / server | mtw-compute-3 / compute-3 |
| host init / payload PID | 2776 / 2779 |
| container init / payload PID | 1 / 4 |
| PID / mount namespace | pid:[4026532233] / mnt:[4026532230] |
| Захист | root/repository/core read-only=true; jobs writable=true; UID=1000; NoNewPrivs=1; host marker visible=false |
| Задачі / native SLAM | **0 / 0** |
| Після stop | remaining=[], всі простежені kernel PID зникли |

Зупинка надсилає SIGTERM перевіреному payload, щоб Uvicorn/manager завершили свої процеси. Початковий варіант сигналу лише supervisor потребував bounded fallback kill init; це замінено direct payload shutdown. У фінальному smoke є `Application shutdown complete` / `Finished server process [4]`; nspawn supervisor повернув **255** після навмисного stop, усіх простежених PID немає. Не прирівнюємо exit supervisor до exit native: SLAM у smoke не запускався. Rootfs і core не перебудовувалися.

Runtime перевіряє PID/start_ticks/argv і зв'язок namespace із власним nspawn. Namespace inode/PID можуть повторно використовуватися, тому невідома належність дає відмову без сигналів стороннім процесам. Bounded force-stop може вбити тільки перевірений початковий init цієї namespace; ресурс не позначається звільненим, поки kernel entries не зникли, включно із zombie.

Доказ: native `web/reports/prompt11-nspawn-smoke.json`, `prompt11-container-linker.json`, журнал `web/logs/nspawn-compute-3.log`. Сам smoke є доказом application/ізоляції/stop; реальний автоматичний запуск доведений окремим сценарієм нижче. Read-only mounts також містять `/etc/alternatives` та `/etc/ld.so.cache`: вони потрібні для незмінених LAPACK/BLAS symlinks. Linker перевірено всередині контейнера з тим самим LD_LIBRARY_PATH, що у worker, без відсутніх бібліотек.

## Підготовлена автоматика

Міграція **0005_compute_servers** вже застосована. Вона створює тільки registry (три дозволені ID), не змінює jobs/users. Два native endpoints стають ready після health; додатковий compute-3 спочатку stopped. Під навантаженням controller перевіряє обидва baseline running і queued/cancel_requested=false. Advisory transaction lock **7132104** та row lock атомарно резервують один starting/token. Стан starting вже займає єдине місце можливого третього екземпляра; створення compute-4 відхиляється CHECK. Окремий controller flock, nspawn machine ownership та PostgreSQL compute lease додатково захищають від дублів.

Конфігурація `[autoscale]` у `config/local.toml`: base_servers=2, max_servers=3, startup_seconds=25, retry_seconds=30, max_start_attempts=2, health_interval_seconds=1, heartbeat_seconds=15. Старт/health обмежені часом; невдалий запуск лишає queued задачі, не резервує для них неіснуючий слот. Після помилки — cooldown та не більше двох launch attempts без явного втручання; restarting dispatcher не скидає лічильник. Після штатного operator stop лічильник обнуляється. Є локальний root `reset-failure`, публічного API для цього немає.

Порядок публікації: реальний endpoint health + PID/mount/RO/user proof → проєктний Nginx candidate → nginx -t → atomic replace/reload → health compute-3 через Nginx → PostgreSQL ready. До цього third slot не видається. Root controller делегує render/reload окремому процесу з UID/GID власника DATA (demus), без supplementary groups, timeout30. TLS/proxy ownership checks не послаблено, приватні ключі/сертифікати й їхні власники не змінені. Render серіалізований flock; при помилці видалення upstream stop усе одно завершує власну namespace та журналює помилку.

Dispatcher бере capacity з fresh ready registry, максимум 3; partial unique index залишає один job на server_id. ETA застосовує той самий фактичний ready count, не додає starting/stale слот. HTTP retries використовують попередній dispatch token. Сервери не зменшуються автоматично: після успіху третій лишається до явного stop.

## Setup/start/status/stop

Усі команди — PowerShell із поточного репозиторію. Звичайні start/status працюють через нативний WSL; start/stop root controller використовують конкретний WSL root helper, не sudoers або глобальний сервіс.

```powershell
wsl -d Ubuntu --exec bash scripts/migrate
wsl -d Ubuntu -u root --exec bash scripts/autoscaler setup
wsl -d Ubuntu --exec bash scripts/start
wsl -d Ubuntu --exec bash scripts/status
wsl -d Ubuntu --exec bash scripts/stop
```

`start` за замовчуванням запускає api, два baseline compute, project nginx, root autoscaler, dispatcher. Контейнер не створюється наперед. `stop` спочатку припиняє dispatcher/автозапуск, завершує власний контейнер і групи, потім baseline; БД не видаляється, postgres додається окремим аргументом. Якщо controller вже помер, root stop шукає тільки точно власний nspawn. **Невідомі PID/namespace не знищуються автоматично.**

Окреме керування:

```powershell
wsl -d Ubuntu --exec bash scripts/stop autoscaler
wsl -d Ubuntu --exec bash scripts/start autoscaler
# Тільки після аналізу помилки й коли немає живого контейнера:
wsl -d Ubuntu -u root --exec bash scripts/autoscaler reset-failure
```

## Цільові тести

Фінальний suite: **58 tests, OK, 18,644 с** (`web/reports/prompt11-backend-tests-final.log`). PostgreSQL tests — disposable schemas; root/process/network side effects controller mocked. Без SLAM перевірено конкурентний єдиний launch, busy/queue trigger, max3 і CHECK compute-4, cooldown/max attempts/persistence, error/timeout зі збереженням задач, health→Nginx→ready, readiness-aware ETA, ownership stop, збій Nginx під час stop і privilege delegation. Регресії auth/history/FIFO/slots/TLS також пройшли. **8 frontend tests та TypeScript/Vite build** пройшли під час підготовки 03.11; UI-код після них не змінювався, повторного build/CV rebuild не робили.

```powershell
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -B -m unittest -v test_autoscale_pg test_scheduler_pg test_queue_pg test_auth_pg test_history_pg test_container_stop test_nginx_owner test_tls
wsl -d Ubuntu --exec bash scripts/build-ui
```

Фактичний pre-load start/status пройшов: api **24047**, compute-1 **24101**, compute-2 **24105**, nginx **478**, dispatcher **24121**, autoscaler **24221**; ready capacity **2/3**, compute-3 stopped/attempts=0. HTTPS listener_verified=true, CA не змінено. Це також не доказ R7 під навантаженням.

## Історичний preflight 03.11: диск до створення задач

Після початкових 15 587 049 472 bytes C: простір двічі значно коливався. Перша підготовка зупинилася на **5 135 368 192 < 5 368 709 120**. Після нового заміру **15 089 197 056** підготовка proof успішна й application smoke перевірено. Перед observer навантаження C: знову впав до **4 887 080 960**, нижче того самого резерву. **Нових POST /api/jobs не було; run не викликався.**

Команда, яка зупинилася:

```powershell
wsl -d Ubuntu -u root --exec env MTW_RUNTIME_HOME=/home/demus PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/autoscale_acceptance.py observe --root /home/demus/.local/share/monotrajectoryweb/web/reports/autoscale-19a878dd40f04dca888cbfec45244c43
```

```text
ValueError: Insufficient free space on /mnt/c: 4887080960 bytes free; reserve 5368709120
```

21:14:17 Kyiv: Windows Get-PSDrive **4 885 913 600**, дві Win32 disk API **4 885 979 136 → 4 885 913 600**, WSL demus/root df ті самі значення в межах інтервалу. Це не розбіжність одиниць або неправильний диск. Останній наступний замір 21:18:16 — **4 910 362 624 bytes (~4,57 GiB)**. Причину зовнішнього падіння/відновлення місця не встановлено; його не приписуємо проєкту/pagefile або самому monitor без доказів. Pagefile/hiberfil/VHDX та Windows/WSL settings не змінювалися, очищення збережених задач не виконувалося.

Один Windows native-API collector PID **2700** записував 0,5-секундні samples без per-metric subprocess, зупинений marker. Серед наявних samples RAM/commit лише інформаційні: о 21:14:17,426 Available RAM=658 493 440; committed=38 381 477 888, limit=39 247 777 792. Жодного RAM/commit gate немає; зупинка лише за C:. Monitor RSS у цьому sample 18 726 912 / private 11 821 056 bytes; причинного висновку це не дає. Періодичні PowerShell не створювалися.

Цей preflight збережений у `web/reports/autoscale-19a878dd40f04dca888cbfec45244c43`; його не зараховували й не перезаписували.

## Свіжі ресурси 05.11.2026

| Джерело / Kyiv time | C: bytes free | GiB |
| --- | ---: | ---: |
| Windows Get-PSDrive, 20:30:29 | 18493603840 | 17,22 |
| WSL statvfs /mnt/c, 20:30:46 | 18454892544 | 17,19 |
| Windows початок прийнятого monitor, 20:44:06 | 18064416768 | 16,82 |
| Windows/WSL останній monitor sample, 20:46:10 | 17895424000 | 16,67 |
| Windows Get-PSDrive / WSL statvfs, 20:56:05 | 17886941184 / 17886941184 | 16,66 / 16,66 |

Заміри Windows/WSL підтверджують достатній запас; початкові пороги ≥10 GiB, аварійні <5 GiB незмінні. Native FS перед стартом мав 1010405855232 bytes free. Повного аудиту диска, змін pagefile/hiberfil/VHDX/Windows/WSL settings або очищення старих задач не було. Не робимо висновків про причину зовнішніх коливань вільного місця.

## Прийнятий реальний сценарій

Використано готові `freiburg1_xyz-rgb-30fps-short-10s.mp4` (300 RGB) і `...-12s.mp4` (360 RGB), 640×480/30fps та їхні перевірені parameters.json з калібруванням. Повторного перекодування, штучних затримок або CV rebuild не було. HTTPS client перевіряє CA, service key залишається приватним.

Evidence ROOT: `/home/demus/.local/share/monotrajectoryweb/web/reports/autoscale-0debce34bd9a485bae08a848e392cccf`. Перед prepare було 67 збережених terminal задач після облікованих нижче спроб; початкові 54 додатково порівняно з першим before.json.

Фактично виконані команди з репозиторію (ROOT — зазначений вище native каталог):

```powershell
wsl -d Ubuntu --exec bash scripts/start autoscaler
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/autoscale_acceptance.py prepare --expected-jobs 67
# Окремий довгоживучий Windows collector scripts/resource-monitor-windows.py, interval=0.5.
# ROOT підставляється явно; observer і run працюють в окремих процесах.
wsl -d Ubuntu -u root --exec env MTW_RUNTIME_HOME=/home/demus PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/autoscale_acceptance.py observe --root ROOT
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/autoscale_acceptance.py run --root ROOT
wsl -d Ubuntu --exec bash scripts/stop autoscaler
wsl -d Ubuntu --exec bash scripts/start autoscaler
```

Observer зупиняється файлом `ROOT/stop`; collector — своїм приватним stop-file. Новий run відмовляється перезаписувати наявний run.json. Повторення не потрібне й створило б ще чотири задачі. `--expected-jobs` — лише перевірочна CLI, не нова можливість public API.

Події **UTC 05.11.2026** (Kyiv = UTC+2):

| Подія | Час |
| --- | --- |
| A/B отримали native compute-1/2 | 18:44:17.840 / 18:44:18.044 |
| Обидва native працюють, C queued → launch_reserved | 18:44:39.287 |
| Контейнер готовий після isolation/health/Nginx | 18:44:41.822 |
| C прийнята compute-3 через Nginx | 18:44:42.609 |
| D поставлена в чергу при трьох зайнятих | 18:44:43.088 |
| D запущена після звільнення compute-1 | 18:44:47.459 |
| C / D succeeded | 18:45:14.037 / 18:45:18.278 |
| Явний stop: original PID/namespace/supervisors=[] | 18:46:10.818 |

Container `mtw-compute-3`; nspawn supervisor **3028**, init **3030**, application host **3033** / container **4**; PID namespace `pid:[4026532233]`, mount `mnt:[4026532230]`, відмінні від host. Root/repo/core RO, jobs RW, host marker invisible, UID1000/NoNewPrivs1. Worker host **3052** / container **6**, native host **3104** / container **31**. Повторне використання inode namespace у послідовних тестах не означає незмінний контейнер: належність перевіряється supervisor PID/start_ticks і деревом процесів.

| Job | job_id | server_id | worker / native PID | frames / live / final / points | worker wall, s |
| --- | --- | --- | --- | --- | ---: |
| A | 8cbec59e-d1dc-4d6e-bd9b-ceddb9f67f13 | compute-1 | host2938 / 2991 | 300 / 251 / 219 / 2732 | 30,565 |
| B | 2aa6b18f-a41d-460d-84e1-45ed401d441d | compute-2 | host2939 / 2992 | 360 / 306 / 245 / 2715 | 35,480 |
| C | d1a1356c-1307-48ad-8341-5bb87a9efaed | compute-3 | host3052(local6) / host3104(local31) | 300 / 249 / 215 / 2726 | 32,087 |
| D | 56d09c45-16cf-4e0c-a460-00ad3ebdc586 | compute-1 | host3079 / 3123 | 300 / 251 / 205 / 2724 | 31,671 |

Усі succeeded; JSONL frame_id/PTS/Twc/null/segments, finite points/quaternions та фінальна snapshot consistency перевірені. Points limit100000, truncation=false для всіх. Wall time виміряний monotonic і включає decode, vocabulary/native зі стандартним pacing, export та cleanup; це не чиста швидкість SLAM. UTC clocks у samples коригувалися, тому elapsed/overlap беремо з monotonic, не різниці wall-clock.

Максимум active=3, supervisor=1, один job/server; D queued без native, фактичне очікування 4,371 с. A/B native перекриття ≥16,676 с. C/D native перекриття ≥16,058 с, з одночасними frame progress протягом ≥8,425 с: C105→299 / CPU+1810 ticks, D0→176 / CPU+1812. Загальні native CPU deltas A/B/C/D =3446/4077/3463/3484. Третій сервер обробляє реальні дані, а не лише health/HTTP запити. Вимога не потребує одночасного tracking усіх трьох: базові короткі ролики можуть завершитися під час завантаження vocabulary нового процесу.

Фактичні HTTPS операції: GET `/api/auth/session` 200; POST `/api/auth/login` 200; чотири multipart POST `/api/jobs` (video + parameters) →202; GET власного state/snapshot →200, після завершення result/artifacts →200; logout →204. Credentials/cookie/CSRF не наводяться. У `run.json` збережені метод/path/status/elapsed і відповіді стану. Приклади повернених полів C: `status=running`, `server_id=compute-3`, `worker_pid=6`, `worker_pid_scope=container:compute-3`; фінально `processed_frames=300`, `valid_live_poses=249`, `points_exported=2726`.

Nginx access proof:

```text
2026-11-05T18:44:42+00:00 POST /execute HTTP/1.1 409 job=d1a1356c-1307-48ad-8341-5bb87a9efaed upstream=127.0.0.1:8101 server=-
2026-11-05T18:44:42+00:00 POST /execute HTTP/1.1 409 job=d1a1356c-1307-48ad-8341-5bb87a9efaed upstream=127.0.0.1:8102 server=-
2026-11-05T18:44:42+00:00 POST /execute HTTP/1.1 202 job=d1a1356c-1307-48ad-8341-5bb87a9efaed upstream=127.0.0.1:8103 server=compute-3
```

Той самий reservation/token при busy 409 не створює дубля. До завершення API повернув 9/11/9/8 проміжних snapshots для A/B/C/D; C frame0→299, tracking1→2, справжні point samples до256. Фінальні previews повернули200 після видалення робочих RGB. Termination reports мають group_members_after=[]; явний container stop видалив init3030/payload3033/supervisor3028 і namespace, включно з kernel entries/zombies. Наступний controller start дав ready2/max3, compute-3 stopped без нового запуску: автоматичного зменшення не реалізовано.

## Моніторинг та збереженість

Однопроцесний Windows collector **41048**, interval0,5с, 250 samples; WSL observer —243 samples із PID/CPU ticks/RSS/progress. Collector CPU4,75с за125с, max observed RSS18718720 bytes; per-metric PowerShell не запускався. Мінімум C:17895424000 bytes=16,67 GiB, аварійного скасування не було. Available RAM min/max1675812864/3326832640; commit headroom min/max2730782720/4558524416 bytes — лише інформація. C native RSS максимум серед знятих samples586604544 bytes; це спостережений максимум, не системний peak. Windows log: `%LOCALAPPDATA%\MonoTrajectoryWeb\diagnostics\autoscale-0debce34bd9a485bae08a848e392cccf\windows.jsonl`.

ROOT містить `before.json`, `run.json`, `series.jsonl`, `validation.json`, `facts.json`, `tracking-overlap.json`, `events.log`, `nginx-routes.json`, `monitoring-summary.json`, `before-stop.json`, `after-stop.json`, private session files. `facts.json` додатково звіряє **всі 54 початкові рядки/owner та файлові signatures (names/size/mtime)** з першим preflight; без змін. CV5/R5-5/R6-5 SHA-256 і users hash збережені. Масові медіа повторно повністю не хешувалися. Глобальні ліміти100 stored/10 pending, захист диска, HTTPS/CSRF/ownership/service key збережені.

## Невдалі спроби та конкретні виправлення

Усі reports у `web/reports`, всі 13 задач збережені; повний перелік UUID/станів — `facts.json → all_test_jobs` та відповідні run.json. Жодна з цих спроб не зарахована як приймання R7. Повторювали тільки після конкретного виправлення, без автоматичного retry loop driver.

| Report suffix після autoscale- | Нових задач | Причина зупинки / виправлення |
| --- | ---: | --- |
| db0d9c26364947ad8f5fb7fcb64b4246 | 3 | Root render: `TLS private key must be an owned native file with mode 0600`; делеговано demus, додано bounded cleanup при Nginx error. Driver завершився `Baseline ended before three-native proof`. |
| df46a5dbc40a486a9d5f5c4538a5e929 | 3 | Nginx уже успішний, але driver вимагав зайвого three-native overlap і скасував C під час decode. Прибрано цей критерій; actual native/result і cap3 залишено. |
| 94dd58171cc44f7c954d48078e2e2828 | 4 | C exit127: `liblapack.so.3: cannot open shared object file`; read-only alternatives/cache + повний container ldd smoke, без rebuild. |
| b8ae3daca5d54af5931494441c9f9762 | 3 | Observer raw argv scan порахував pre-exec fork як другий nspawn. Smoke відтворив parent2773/child2776; ancestry тепер рахує один supervisor, незалежні peers залишаються двома. |

Ще під час підготовки виправлено Python `too many statically nested blocks` у test fixture переходом на ExitStack; це не помилка production CV і не потребувало обчислювального запуску. Всі фінальні58 tests успішні.

Контейнер спільного WSL kernel/loopback з read-only host libraries — локальний прототип, не VM і не захист від адміністратора host. CA не встановлювався у системну довіру. Автозменшення, адмінпанель, retention manager і фінальний етап не реалізовувалися. Нових обчислювальних перевірок для завершення Промпта11 не потрібно.

Перевірена реалізація — локальний коміт **3d64ff8**, `feat: autoscale a third nspawn compute through Nginx`; автор і комітер **versal217 <versaldata@gmail.com>**, перевірені перед комітом. Фінальний status: PostgreSQL/HTTPS справні; api561, compute1/2=617/621, nginx625, dispatcher735, controller3272; ready2/max3, compute-3 stopped. Native FS free1009882038272 bytes. Початкові файли вимог/промптів, сторонній Release, ключі, БД, медіа, rootfs та результати не включені в коміт. Push не виконувався.
