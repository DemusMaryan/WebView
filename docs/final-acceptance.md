# Фінальне приймання та підготовка до захисту

Прийнято **06.11.2026, Europe/Kyiv** (новий browser run: 05.11 22:00:42–22:03:01 UTC). Межі Промпта 13: перевірки й документація; production-код, CV-ядро, ABI, залежності та системні налаштування не змінювалися. R1–R8 підтверджені в описаному локальному середовищі; це не перевірка довільного відео чи багатовузлового production deployment. Остаточну навчальну оцінку визначає викладач.

Початковий [WebProject.txt](../WebProject.txt) не змінений. Нижче формулювання його пунктів наведені дослівно; ідентифікатори R1–R8 — позначення проєкту. HTTPS винесено окремо через стан довіри браузера.

## Матриця вимог

### R1 — перевірено

> Повинна бути обмежена максимальна трудомісткість одної задачі (кількість невідомих, час обчислення).У разі перевищення - помилка або відмова у виконанні. (Перевірку вхідних даних задачі можна виконати ще на клієнті (frontend)).

- Реалізація: `backend/mtw/contracts.py:JobParameters`, `api.py:UploadLimit/create_job`, `video.py:decode`, `compute.py:run_job`, `capacity.py:ensure_space`, `frame_work.py:cleanup_decoded_frames`; значення — `config/local.toml`.
- Доказ: нові isolated PostgreSQL input tests, справжні decode-failure child processes без SLAM; допустимий HTTPS UI job `4c718f50-153f-4c61-a33e-f4ce8ce42a94` успішний. Реальний timeout R5 `4fd38b92-0559-49e9-90c5-b364bd0bee4f` повторно не запускався: відповідні гілки timeout/cancel і cleanup звірені з `dafba89`, див. нижче.
- Показ: ліміти у формі й `GET /api/config`, помилка для execution=181; звіт тестів відмов/cleanup і збережений timeout. Детальні межі — наступний розділ.

### R2 — перевірено

> Оскільки запити трудомісткі, клієнт повинен інформуватись про хід виконання задачі (відсоток/етап виконання).

- Реалізація: `worker.py:run` атомарно публікує `latest.json`; `api.py:snapshot`; `frontend/src/useJob.ts`, `App.tsx`, `MapView.tsx`.
- Доказ: фінальний browser run отримував різні live frame_id й різні point digests до завершення. PNG canvas для кадрів **8, 47, 86** різні; відповідні RGB label/alt/frame job_id збігалися. Worker не створює координат таймером. Є 300 JSONL frame-записів 0–299, реальні tracking states та 48 null-поз.
- Показ: відкрити поточну задачу, етап, лічильник кадрів, одночасний preview/карту; розріджені точки, траєкторія й камера мають відносний масштаб. Збережені screenshots дозволяють показати попередній live стан без повторних обчислень.

### R3 — перевірено

> Клієнт повинен мати доступ до історії результатів виконання задач, переглядати стан поточної задачі (див. пункт 1.), скасувати виконання задачі, запустити виконання нової задачі. (максимальна кількість задач повинна бути обмежена, необхідні дані зберігатись в базі даних)

- Реалізація: `db.py:Job`, `api.py:create_job/list_jobs/status/result/cancel`, PostgreSQL migrations, `frontend/src/useHistory.ts` та `App.tsx`.
- Доказ: глобально **100 stored усіх станів**, окремо **10 pending**. Atomic admission, одночасні POST біля cap, cleanup після відмови, owner-filtered pagination та однакові timestamps перевірені в isolated PG — [контракт/докази](api-contract.md), [auth](authentication.md). У фінальному прийманні всі 72 старі повні рядки й файли незмінні; додано рівно 1 задачу. Усі **73** збережені після повного restart БД/служб; history/result/session пережили reload у тому самому browser context.
- Показ: історія власника з пагінацією, відкриття результату після reload; докази cancel R5 й ліміту R3 без заповнення робочої БД тестовими рядками. Legacy 51 owner=NULL збережені, звичайним користувачам не видаються.

### R4 — перевірено; HTTPS — див. окремий статус

> Авторизація клієнта (з використанням https (+бали))

- Реалізація: `auth.py`, `proxy.py`, `service_auth.py`, `db.py:User/AuthSession`, `frontend/src/Auth.tsx`; owner перевіряється backend на стані, snapshot, preview, video, result, artifacts, cancel. Argon2id, PostgreSQL session token hash, expiry/logout, CSRF/Origin; browser cookie Secure/HttpOnly/SameSite=Strict.
- Доказ: два isolated users і ACL у [authentication.md](authentication.md), повторення через HTTPS та admin role tests у [admin-monitoring.md](admin-monitoring.md). Анонімному 401, чужому job_id 404; роль admin не обходить ownership. Фінальний UI login/logout і збереження сесії після повного restart успішні; свіжий анонімний HTTPS `/api/auth/me` →401.
- Показ: вхід, власна історія; анонімний запит 401. Облікові записи не містяться в Git; новий користувач створюється через UI. Персональних квот/OAuth/reset password немає.

### R5 — перевірено, головна вимога

> Веб застосування повинне забезпечувати т.з. балансування навантаження як мінімум для 2-ох серверів.
> (для цього повинен бути застосований так званий load balancer, в його ролі може виступати і сам веб сервер, а задачі виконуватись на кількох серверах застосувань (application server)).

- Реалізація: `scheduler.py` atomic reservation, `dispatcher.py`, `compute.py` один слот/server та idempotent handoff; `control.py:write_nginx` і native project Nginx config.
- Доказ: [parallel-processing.md](parallel-processing.md): `d3a0a85a-00ec-409d-9db1-85e50b27ff2c` / `4f7a65d8-f149-4514-820f-ac24b7352127`, compute-1/2, native PID1618/1619, перекриття **≥10,140 с**, CPU ticks/tracking, Nginx `/execute`, два валідні результати. Окремі cancel/timeout завершили тільки цільову process group, сусідня задача успішна, залишених PID/zombie немає. R7 зберіг один слот/server, збільшив максимум із 2 до 3.
- Показ: таблиця job/server/PID і часовий ряд перекриття, access log Nginx, два validation results; два health200 самі по собі доказом не є. Повторної базової пари у фінальному прийманні не запускали.

### R6 — перевірено

> (+бали) Якщо всі ресурси системи вичерпані, клієнт повинен бути повідомлений про те, що його запит поставлено у чергу та інформуватись про приблизний час очікування.

- Реалізація: `scheduler.py`, `queue_eta.py`, `api.py:status`, `frontend/src/queue.ts`, `App.tsx`. FIFO `(created_at,id)`, pending cap10; ETA моделює фактичні ready slots, успішний worker wall без queue time, розмір відео й залишок активних робіт; cold start45с/reference360frames640×480, configurable, fallback0 вимикає його.
- Доказ: [queue-eta.md](queue-eta.md): `2d27a4da-dd0f-42aa-b998-f976f4075da2` чекала при двох зайнятих compute, position1, ETA20,3с, фактично19,329с після прогнозу; пережила restart dispatcher, почалася після звільнення слота, всі три результати валідні. Browser queued→running→succeeded; isolated FIFO/ties/null/cancel/owner tests. З R7 queue/ETA враховує ready2 або ready3.
- Показ: збережений queued screenshot, HTTP status з position/ETA й час фактичного старту. Чужі job_id/owner через owner status не розкриваються. ETA приблизний, може бути null, не гарантія.

### R7 — перевірено

> (+бали) Горизонтальне розширення за рахунок автоматичного запуску нових серверів (віртуальні машини, контейнери), максимальна кількість повинна бути обмежена.

- Реалізація: `autoscaler.py`, `servers.py`, `container_runtime.py`, migration0005, project Nginx/start/status/stop. Base2 native / max3 including starting; atomic launch reservation, bounded readiness/retries; health→Nginx config test/reload→ready.
- Доказ: [autoscaling.md](autoscaling.md): systemd-nspawn255.4, справжня mount/PID/filesystem isolation, `mtw-compute-3`, container job `d1a1356c-1307-48ad-8341-5bb87a9efaed` через Nginx8103; worker host3052/local6, native host3104/local31; 300frames/249live/215final/2726points, valid. Усі4 результати прийнятого run валідні; четверта задача чекала4,371с при3 busy; C/D overlap≥16,058с. Max3/concurrency/start failure/stop tests; namespace й PID після stop відсутні.
- Показ: trigger busy2+queue, журнал launch/isolation/health/reload/assignment, validator й registry. Це Linux-контейнер зі спільними WSL kernel/RAM/loopback, не окрема ВМ чи незалежний вузол. Автоматичного scale-down немає; Docker не використовується.

### R8 — перевірено

> (+бали) Панель адміністратора для моніторингу всіх запущених процесів та навантаження на систему серверів.

- Реалізація: `admin.py`, `admin_role.py`, `monitoring.py`, `metrics.py`, `frontend/src/Admin.tsx/adminPolling.ts`, migration0006. Backend ACL кожного endpoint; root collector/cache2с/TTL6с, bounded256 rows/256KiB; моніторинг read-only.
- Доказ: [admin-monitoring.md](admin-monitoring.md): anonymous401/user403/admin200, isolated role grant/revoke/registration spoof rejection; real worker7327/native7367 CPU/RSS під час job `40a914a2-0c06-4bbb-9a8e-bd3b2064ac44`, результат валідний; container-only namespace/PID probe, busy/missing/unavailable/stale й polling cleanup. Production admins **0**, автоматичних призначень не було.
- Показ: `/admin` лише після явного ручного grant вибраному існуючому login; service status, slots, queue/ETA, host/container PID і WSL CPU/RAM. Процес CPU100%=один logical CPU, system CPU0–100% усіх CPU, RSS bytes/API→MiB/UI; спільну RAM не рахують як три машини. Без grant можна показати screenshots і ACL tests; не слід видавати403 за несправну панель.

## R1: фактичні межі й результати відмов

| Обмеження | Значення та застосування |
| --- | --- |
| Відеофайл | ≤67108864 bytes =64 MiB; клієнт, streaming upload і повторно decoder |
| HTTP multipart body | ≤file cap+65536 bytes, включно з chunked body; перевищення413 |
| JSON parameters | ≤8192 символів; UI calibration file≤8192 bytes |
| Реальне відео | ≤40с, ≤1200 decoded frames, ≤1280×720; параметри запиту можуть лише зменшити максимуми |
| Калібрування | обов'язкове, finite числа, fx/fy>0, principal point всередині; decoded dimensions рівні calibration; без resizing або автоматичного TUM для стороннього відео |
| Потоки й час | рівно один video stream, RGB; actual PTS обов'язкові/strictly increasing, відлік від першого PTS; duration враховує останній frame duration |
| Execution | 1–180с; compute budget охоплює decode, запуск/vocabulary, paced native pipeline, export; queue time виключено; при timeout група TERM/KILL→wait/reap→cleanup |
| Місце | native reserve2GiB та C:5GiB; для цього контрольного запуску перед стартом C:≥10GiB; RAM/commit інформаційні |

Схеми `mtw_final_*` і каталоги тестів створено окремо від public; після тестів видалено. П'ять нових test methods у `backend/tests/test_final_input_pg.py` перевірили: допустимі 3 RGB frames на точних межах; усі6 параметрів вище configured cap та некоректне калібрування→422 із назвою поля; частково записаний файл понад request cap→413, empty→422; total Content-Length/chunked body→413. Після upload-відмов нових jobs/директорій немає.

Шість реальних worker decode subcases: frames, duration, width, height, невідповідне калібрування, corrupted input. API допустимо повертає202 до декодування, далі задача `failed` з конкретним `error` (`Decoded frame count exceeds…`, `Decoded duration exceeds…`, `Decoded resolution…`, `InvalidDataError`). Для кожної group_members_after=[], worker PID зник, слот вільний, `sequence/rgb` прибрано, input збережений; native SLAM не запускався. Разом із `test_contracts`(8), `test_capacity`(2), `test_frame_work`(6) перевірено **21 різний test method**.

Чесний перебіг: перша команда21 tests мала6 failures у subcases одного нового test method через відсутній mock-lifespan SQL lease у fixture, не через production defect. Fixture виправлено на реальне isolated SQL connection; **лише цей метод** повторено, усі6 subcases OK за6,305с. Інші20 methods уже пройшли. Не називаємо первинний лог21/OK. `r1-tests.log`, `r1-worker-final.log`, `r1-checks.json`, `r1-checks-final.json` збережені в native ROOT.

```powershell
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -B -m unittest -v test_final_input_pg test_contracts test_capacity test_frame_work
# Фактично виконаний цільовий повтор після виправлення fixture:
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -B -m unittest -v test_final_input_pg.FinalInputTests.test_real_worker_decode_errors_reap_process_and_clean_frames_without_SLAM
```

Реальний timeout з R5 використаний повторно за доказом `timeout-code.json`: AST гілок cancel/elapsed budget і terminate/wait/reap/cleanup у `compute.run_job` збігаються з `dafba89`; `processes.py` і `frame_work.py` повністю збігаються. Весь `run_job` не тотожний: R6 додав successful video_info metadata. Ця зміна не зачіпає перевірений timeout шлях. Не робили нового timeout SLAM run.

## Чому R7 має 58 backend tests, а R8 — 56

Це різні **набори**, не зменшення загального покриття й не падіння тестів. `test-counts.json` отриманий AST-підрахунком test methods із фактичних комітів R7 `3d64ff8` та R8 `5864b1b` і звірений із командами/логами звітів.

| Модулі | R7 | R8 |
| --- | ---: | ---: |
| test_auth_pg / test_nginx_owner | 13+2 | 13+2 |
| test_autoscale_pg / test_scheduler_pg / test_queue_pg / test_tls | 9+5+9+9 | 9+5+9+9 |
| test_history_pg / test_container_stop | 7+4 | не входили |
| test_admin_pg / test_metrics | не входили | 5+4 |
| Різних methods | **58** | **56** |

Спільних47, R7-only11, R8-only9: 58−11+9=56. R7 команда: `python -B -m unittest -v test_autoscale_pg test_scheduler_pg test_queue_pg test_auth_pg test_history_pg test_container_stop test_nginx_owner test_tls` з `MTW_POSTGRES_TESTS=1`, `PYTHONPATH=backend:backend/tests`; лог `web/reports/prompt11-backend-tests-final.log`,58/OK/18,644с.

R8 первинна команда `python -m unittest -v test_admin_pg test_metrics test_auth_pg test_nginx_owner`:23/OK/17,853с (metrics тоді3). Regression команда `python -m unittest -v test_autoscale_pg test_scheduler_pg test_queue_pg test_tls`:32/OK/5,622с. Після role-classification fix `test_metrics`:4/OK/0,006с, три повтори та один новий. Розширений auth is_admin case повторений окремо, додатковим методом не рахується. Разом23+32+1=56 distinct. R8 frontend:11 tests і TypeScript/Vite build OK. У Промпті13 production Python/config/frontend не змінювалися від R8; попередні suites/build повторно не запускали. Новий browser harness перевірений `node --check` та реальним виконанням; Python test/helper/audit syntax перевірена.

## Єдиний новий HTTPS UI run та restart

Native ROOT: `/home/demus/.local/share/monotrajectoryweb/web/reports/final-833d89db3f1c48f5a1ec3dba93d81e18` (поза Git). Browser harness: `frontend/tests/final-browser.mjs`, preservation/validator: `backend/tests/final_evidence.py`.

| Факт | Результат |
| --- | --- |
| Чому потрібен | R12 перевірив API/admin/live process, але не весь звичайний UI upload→map→reload шлях через HTTPS |
| Вхід | готовий `freiburg1_xyz-rgb-30fps-short-10s.mp4`,300frames640×480, explicit paired calibration; відео не готували повторно |
| Job/server | `4c718f50-153f-4c61-a33e-f4ce8ce42a94`, compute-1; POST202 |
| Результат |300 frame records0–299,252 valid live/48 null,226 final poses,2695 exported points, cap100000, truncation=false |
| Валідація | finite xyz, Twc rotations/quaternions, timestamp=frame_id/30, final IDs subset live, однакові map/segment, workers_finished=true, active_gba=0, stable geometry digests |
| Час | worker wall **32,205482с**: decode/vocabulary/native pacing/export; це не чиста швидкість SLAM і не FPS benchmark |
| Live | snapshots/points змінювалися до succeeded; canvas screenshots live-8/47/86, matching frame label/alt/job; без штучних затримок |
| Після завершення | результат й історія після reload; робочих RGB немає, preview200, input/results/JSONL збережені, process group порожня |
| Повний restart | API/compute/Nginx/monitor/autoscaler/dispatcher та PostgreSQL штатно stop→start; до stop нема активних jobs; БД реально stopped, усі service PID absent; після start ready2/max3, compute-3 stopped |
| Browser після restart | той самий cookie context без повторного login: користувач, готовий результат, історія й preview відкриваються; logout успішний; JS/mixed-content errors немає |

Файли доказів: `before.json`, `timeout-code.json`, `test-counts.json`, `browser.json`, `job.json`, `validated.json`, `restart-before.json`, `after-restart.json`, `services-before.json`, `services-after.json`, `result-before-restart.png`, `result-after-restart.png`, `live-*.png`. Screenshots переглянуті; вони залишені native, не містяться в Git.

`before/validated/after-restart`: усі72 повні старі rows/owners незмінні; names/size/mtime signatures усіх їхніх файлів однакові; users/roles/password-hash digest незмінний, legacy51 owner=NULL, admins0. Для великих медіа це файлові signatures, не повторний повний content hash. CV5 та збережені R5/R6/R7/R8 proof SHA-256 збігаються. Після restart усі73 rows/files тотожні checkpoint. Додана тільки одна явно перевірочна задача існуючого test account; нових production accounts/ролей не створювали. При першій викликаній перевірці preservation helper мав помилку повторного відкриття вже відкритого httpx client; виправлено тільки helper, повторено лише read-only validate, без нового обчислення.

Місце: перед підготовкою Windows C:17 536 999 424 bytes≈16,33GiB; безпосередньо перед real run WSL `/mnt/c`16 491 896 832≈15,36GiB; після restart Windows16 406 843 392≈15,28GiB. Значення зняті в різний час; різниця не приписується цьому проєкту. Аварійний резерв5GiB не спрацював. RAM/commit залишалися інформаційними; жодних системних змін/видалення старих jobs.

## HTTPS і фактична довіра

Canonical origin **https://127.0.0.1:8443**, TLS1.2/1.3; тільки loopback. Свіжий `curl --cacert` після restart: `/api/auth/me`→401, `ssl_verify_result=0`, без `-k`; HTTP GET/HEAD8080→308 зі збереженим path/query, наприклад `/api/jobs?page=1`→`https://127.0.0.1:8443/api/jobs?page=1`. HTTPS/CSRF/proxy/service-key tests попередніх етапів чинні, HTTP user API обходу немає.

Свіжий isolated Chrome context **без overrides** отримав `net::ERR_CERT_AUTHORITY_INVALID`. Функціональний сценарій мав **ignoreHTTPSErrors=true**; він підтверджує UI/HTTPS flow, **не системну довіру Chrome**. CA автоматично не встановлювався. CA/leaf/key лежать native `web/tls`; CA до31.10.2036, leaf до03.11.2027 (точні UTC у `scripts/status`). Приватні ключі не публікуються.

Ручна довіра — лише користувач, після звірки fingerprint:

```powershell
$mtwCaFile = '\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\web\tls\ca.crt.cer'
Get-FileHash -Algorithm SHA256 -LiteralPath $mtwCaFile
# Очікувано 93D44BCDA14500C88B8873BAD3636DF667F04F3D9024EFEEC1CF551B248DB469
# Лише за власним рішенням, до CurrentUser\Root, публічний CA, не приватний ключ:
Import-Certificate -FilePath $mtwCaFile -CertStoreLocation Cert:\CurrentUser\Root
```

Потім користувач сам перевіряє сайт у Chrome без overrides. Цей позитивний trust сценарій поки не виконаний. [Деталі й окрема WSL trust інструкція](https.md).

## Команди демонстрації й ручні дії

З PowerShell у поточній робочій папці (шлях із пробілами/кирилицею не потрібно інтерполювати в bash):

```powershell
wsl -d Ubuntu --exec bash scripts/start
wsl -d Ubuntu --exec bash scripts/status
# Сайт: https://127.0.0.1:8443 ; admin: https://127.0.0.1:8443/admin
# Зупинити власні служби, зберегти БД:
wsl -d Ubuntu --exec bash scripts/stop
# За потреби зупинити також власний PostgreSQL, коли служби вже зупинені:
wsl -d Ubuntu --exec bash scripts/stop postgres
```

Усі команди start/stop реально виконані, без setup/build/dependency rebuild. `start` повторно запускає PostgreSQL, TLS validation та всі потрібні служби. Початкові2 native compute й controller/monitor після перевірки працюють; container3 не запущений без попиту. Для першої підготовки іншого середовища потрібні setup/setup-https/setup-ui і існуюче CV — це не команда звичайної демонстрації.

Файли у Windows file picker:

```text
\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\datasets\freiburg1_xyz-rgb-30fps-short-10s.mp4
\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\datasets\freiburg1_xyz-rgb-30fps-short-10s.parameters.json
```

Вхід через власний обліковий запис; якщо його ще немає — UI «Створити обліковий запис». Пароль не задавати в shell argv/документації. Калібрування paired fixture640×480, для іншої камери потрібне власне. Новий демонстраційний run створить ще одну stored job; не запускайте повторно автоматично. Готовий результат фінального приймання вже доступний його існуючому test owner, чужим login він правильно поверне404.

Адміністратор призначається **тільки вибраному користувачем існуючому login**, не Git-ідентичності й не першій реєстрації. Команди нижче — інструкція, у production не виконувалися:

```powershell
wsl -d Ubuntu --exec bash scripts/admin-role --login EXISTING_LOGIN --grant
# Для зняття:
wsl -d Ubuntu --exec bash scripts/admin-role --login EXISTING_LOGIN --revoke
# Необов'язкова явна передача всіх51 legacy tasks, спочатку dry-run:
wsl -d Ubuntu --exec bash scripts/assign-unowned --login EXISTING_LOGIN --all-unowned --dry-run
# Тільки якщо користувач вирішив призначити саме цьому login:
wsl -d Ubuntu --exec bash scripts/assign-unowned --login EXISTING_LOGIN --all-unowned
```

Швидкий захист на5–7хв і карта файлів — [how-it-works.md](how-it-works.md). Не потрібно повторювати всі CV/parallel/autoscale runs для показу вже зафіксованих доказів.

## Git і ручна публікація

Read-only `git ls-remote target refs/heads/main` підтвердив remote main **faa98ba6984b1af24ccaf7c65ae81b7c8564f3ce**; локальний `target/main` збігається. Для аудиту використано цей актуальний base, не застарілий remote ref. До Промпта13 було22 неопублікованих коміти; завершені перевірки — **ce3449c**. Підсумкова документація додає ще один локальний коміт. Точний кінцевий40-символьний HEAD наводиться у фінальному повідомленні й native `publication.json`, щоб не створювати самопосилальний SHA у власному коміті.

`scripts/audit-publication.py` перевіряє **кожну версію blob у кожному tree** `target/main..HEAD`, filenames/вміст на private-key/certificate payload, credential URLs/provider tokens, БД/відео/build signatures, файли>1MiB, авторів і комітерів. Значення збігів не друкує. Початковий audit22 commits/299unique blobs: findings0, >1MiB0; найбільший blob475034 bytes — підготовлена `docs/figures/cv-trajectory.png`. Остаточний audit після docs commit збережений у `publication.json`; findings мають залишатися порожніми. Це цільова перевірка, не математична гарантія від довільних секретів. Всі перевірені author/committer — versal217 <versaldata@gmail.com>.

```powershell
git status --short
git log --oneline target/main..HEAD
git rev-parse HEAD
python scripts/audit-publication.py --base target/main --report "$env:TEMP\mtw-publication-audit.json"
```

Користувацькі зміни залишено поза комітами: видалений старий файл промптів, непроіндексований файл із `(1)`, `Release`. Вхідні WebProject/оновлені промпти не редагувалися. CA/приватні keys, credentials/sessions/service keys, PostgreSQL, великі videos/datasets/build/results залишаються native поза Git. Публікацію виконує користувач; історію не переписували.

## Залишок перед захистом

1. За власним рішенням довірити публічному локальному CA й перевірити Chrome без overrides.
2. Зареєструвати/вибрати свій login; вручну grant admin, лише якщо потрібна жива панель на захисті. Legacy assignment необов'язковий та окремо явний.
3. Перевірити `scripts/status`, запас диска й готовність тестового ролика. Переглянути5–7хв сценарій і збережені R5/R6/R7 докази.
4. Перевірити фінальний SHA/status і вручну опублікувати потрібний HEAD. Нових функціональних прогалин R1–R8 у межах локального приймання не виявлено; production hardening, незалежні вузли, scale-down, квоти й reset-password виходять за перевірений обсяг.

Push не виконувався.
