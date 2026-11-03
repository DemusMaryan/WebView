# Промпт 6: паралельні обчислення через Nginx — R5

**03.11.2026, коротка контрольна пара успішна.** Дві нові реальні задачі виконалися на compute-1/2 через Nginx; є два валідовані результати та консервативне перекриття native tracking **≥10,140 с**. Аварійні ресурсні межі не перетиналися. Це докази саме поточної успішної пари; попередні невдалі suite за неї не зараховуються. Повне приймання ізоляції cancel/timeout у двослотовій моделі ще не завершено, загальний R5 — частково. Додаткових задач поза погодженою однією парою не створено.

## Успішна пара 300/360 кадрів, 03.11.2026

Чинна конфігурація контрольного тесту — `config/r5-test-resources.toml`. Початок: Windows Available RAM **≥1610612736 bytes (1,5 GiB)**, C: **≥10737418240 (10 GiB)**, commit headroom **≥2147483648 (2 GiB)**. Старі RAM 3/2 GiB і commit 4 GiB не застосовуються. Під час пари сценарій перевіряє Windows RAM **≥536870912**, C: **≥5368709120**, commit headroom **≥1073741824**; падіння нижче будь-якої межі припиняє сценарій і надсилає cancel тільки двом створеним UUID. Збережені додаткові native FS/WSL guards. Product limits у local.toml, підкачка, VHDX і CV не змінювалися.

Нові 2 unit-тести перевірили фактичні початкові/аварійні значення, рівність межі як успіх і відмову на 1 байт нижче для кожного показника; **2 passed за 0,001 с**. Незмінені scheduler/CV-тести не повторювалися. Реальна пара не перетнула emergency thresholds, тому фактичне аварійне скасування цим прогоном не перевірене.

Перед POST, **11:59:35,899 Europe/Kyiv**:

| Показник | Фактичні байти | GiB | Поріг |
| --- | ---: | ---: | ---: |
| C: free | 21 301 321 728 | 19,83840 | 10 |
| Windows Available RAM | 3 009 105 920 | 2,80245 | 1,5 |
| CommitLimit−Committed | 3 390 205 952 | 3,15738 | 2 |

Використано вже готові `[0,10)` / `[0,12)` clips, 640×480, 30 fps, RGB-only, PTS i/30; повторного encoding не було. Явний TUM1 calibration незмінний, інший відеовхід не отримує цей профіль автоматично. Source SHA/commands/calibration наведені у попередньому розділі підготовки.

Команди з PowerShell у поточному repo (обидва output уже існують; для нового дозволеного прогону потрібні новий collector/context та нове ім'я evidence):

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest discover -s backend/tests -p test_r5_resources.py -v
wsl -d Ubuntu --exec bash scripts/start
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/parallel_short.py --context /home/demus/.local/share/monotrajectoryweb/web/reports/monitored-r5-current.json --output /home/demus/.local/share/monotrajectoryweb/web/reports/parallel-short-20261103-commit2.json
```

Сценарій створив рівно два multipart POST через `http://127.0.0.1:8080/api/jobs`, `video=@...short-10s.mp4` / `...short-12s.mp4`, поле parameters з відповідного готового `.parameters.json`. Обидва повернули 202 queued за **0,225 / 0,053 с**, не очікуючи SLAM. Фактичні скорочені відповіді й проміжні запити:

```text
POST /api/jobs → 202 {"id":"d3a0a85a-00ec-409d-9db1-85e50b27ff2c","status":"queued",...}
POST /api/jobs → 202 {"id":"4f7a65d8-f149-4514-820f-ac24b7352127","status":"queued",...}
GET /api/jobs/d3a0a85a-00ec-409d-9db1-85e50b27ff2c/snapshot → 200 {"server_id":"compute-1","frame_id":35,"pose_valid":true,"tracking_state":2,...}
GET /api/jobs/4f7a65d8-f149-4514-820f-ac24b7352127/snapshot → 200 {"server_id":"compute-2","frame_id":10,"pose_valid":true,"tracking_state":2,...}
GET /api/jobs/{A,B}/result → 200, обидва валідовані
```

Dispatcher доставив обчислювальні запити через Nginx:8081; access log, UTC:

```text
2026-11-03T09:59:37+00:00 POST /execute HTTP/1.1 202 job=4f7a65d8-f149-4514-820f-ac24b7352127 upstream=127.0.0.1:8102 server=compute-2
2026-11-03T09:59:37+00:00 POST /execute HTTP/1.1 202 job=d3a0a85a-00ec-409d-9db1-85e50b27ff2c upstream=127.0.0.1:8101 server=compute-1
```

| Факт | A | B |
| --- | --- | --- |
| job_id | `d3a0a85a-00ec-409d-9db1-85e50b27ff2c` | `4f7a65d8-f149-4514-820f-ac24b7352127` |
| server_id | compute-1 | compute-2 |
| Worker / native PID | 1553 / **1618** | 1552 / **1619** |
| Старт compute, Kyiv | 11:59:37,597 | 11:59:37,591 |
| Завершення compute, Kyiv | 12:00:09,804 | 12:00:13,427 |
| Стан | succeeded | succeeded |
| Frame records / valid live / null | **300 / 253 / 47** | **360 / 304 / 56** |
| Final pose subset / points | **220 / 2722** | **223 / 2757** |
| Point limit / truncated | 100000 / false | 100000 / false |
| Worker wall / job execution, с | 30,723 / 31,489 | 34,402 / 35,121 |
| Final geometry digest (двічі однаковий) | `798b03dd0cfe10fc` | `13e33036cdbd3a88` |

Перекриття доведено живими native PID/start ticks, а не лише status=running. В 11 sample обидва native мали валідні tracking snapshots з точками. Від першого до останнього qualified sample CPU ticks зросли **A +2576 / B +2587** (CLK_TCK=100), frame_id **35→283 / 10→230**, JSONL **3 981 343→31 911 683 / 675 217→26 893 877 байтів**. Це справжні проміжні обчислення.

Різниця початкових timestamps цих samples — 10,710260 с. Для консервативної межі виключено невідомий час HTTP/PID reads першого sample: до початку наступного sample перші обидва PID вже були перевірені, а останні reads ще не почалися на timestamp останнього. Отримано **≥10,140443 с monotonic**, межі **11:59:58,664–12:00:08,805 Kyiv**. Первинний evidence містить raw sampling span у старому полі overlap; уточнена консервативна оцінка перевірена read-only у `parallel-short-20261103-commit2-postcheck.json`, raw evidence не переписано. Driver тепер одразу використовує цю консервативну формулу.

API залишався доступним: 310 контрольних HTTP requests, максимальна зафіксована затримка **0,643 с**; 23/26 різних frame_id спостерігалися в running snapshots. Для preview перевірено власний job/server_id, X-Frame-Id та PNG 640×480. Масиви snapshot ≤256 точок/240 trajectory records. Фінальний validator прочитав повний JSONL лише по одному разу: IDs 0–299/0–359, timestamps i/30, tracking state, Twc/SO(3)/unit quaternion, null, sorted final IDs як підмножина валідних live та узгоджені map_id/segment_id. Точки finite, cap/truncation коректні. Фінальні workers finished, active_gba=0, geometry_stable=true, geometry digests рівні в кожному job. Масштаб відносний. Worker/job wall охоплює decode, initialization, SLAM, JSONL/snapshots, shutdown/quiescence/export і керування; job також cleanup. Це не чиста швидкість SLAM.

### Ресурси й збереження даних

Один Windows collector PID **1560**, 0,5 с, **186 samples за 93,000 с**, 11:59:33,899–12:01:06,399 Kyiv; CPU 3,578 с, stderr порожній, завершений stop marker. WSL sampler записав resources/PID/CPU/RSS/progress та 10 after-samples. Заміри синхронізовані через UTC й посилання на Windows sample; тривалості визначено через monotonic.

| Метрика | Мінімум / максимум зафіксованих samples | Emergency threshold |
| --- | ---: | ---: |
| Windows C: free, minimum | **21 298 954 240 bytes = 19,83620 GiB** | <5 GiB |
| Windows Available RAM, minimum | **1 449 013 248 = 1,34950 GiB** | <512 MiB |
| Windows commit headroom, minimum | **1 583 214 592 = 1,47448 GiB** | <1 GiB |
| WSL MemAvailable, minimum | 5 820 162 048 = 5,42045 GiB | додатковий native guard |
| WSL SwapFree, minimum | 2 GiB | виміряно |
| Сума RSS двох native, sampled maximum | 1 163 083 776 = 1,08321 GiB | виміряно |

Жодного emergency threshold у записі не перетнуто; API cancel не викликався. Після job/validation C: 21 299 503 104 bytes (19,83671 GiB); останній Windows sample — 21 298 982 912. Це sampled extrema, не невідомі піки між вимірами. Коротший ролик не подається як гарантія меншого RAM; стороннім програмам або pagefile причина зміни ресурсів не приписується.

Усі **41 попередні задачі**: statuses та signatures файлів незмінні; CV/exporter/dependencies — ті самі 5 hashes. Після пари стало 43 задачі; групи нових workers порожні, slots вільні. Cleanup видалив тільки 300/360 робочих PNG нової пари; збережено 27/30 published previews, source video, JSONL, результати й логи. Після cleanup окремі read-only GET фінальних snapshots/previews дали 200, X-Frame-Id 299/359, PNG 640×480 при відсутньому sequence/rgb; БД має 43 jobs, активних 0. Наявні задачі й початкові 29 не очищалися. Автоматичних повторів, нового encoding, rebuild або системних змін не було.

Evidence поза Git: `$DATA/reports/parallel-short-20261103-commit2.json` і `.jsonl` — **passed=true**; `parallel-short-20261103-commit2-postcheck.json` — read-only уточнення й перевірки; `parallel-short-20261103-commit2-preview-postcheck.json` — previews після cleanup; Windows `%LOCALAPPDATA%\MonoTrajectoryWeb\diagnostics\20261103T095933Z\windows.jsonl`, `exit.json`, порожній `error.txt`. Повні артефакти доступні через `GET /api/jobs/{id}/result` та посилання в manifest.

Перевірений сценарій/пороги/unit-тести — локальний коміт **955d435**, author і committer `versal217 <versaldata@gmail.com>`. Нині виконано лише дозволену одну пару; не запускалися додаткові cancel/survivor/timeout jobs або новий full suite. Для повного початкового приймання Промпта 6 залишаються ізоляційні сценарії нової двослотової моделі. Два compute — незалежні HTTP-процеси на одному WSL-вузлі; це не дві окремі ВМ. Авторизація/HTTPS, ETA, autoscale та адмінпанель не реалізовувалися.

## Історична відмова через колишній commit-поріг 4 GiB, 03.11.2026

У цій попередній перевірці commit headroom мав бути ≥4 GiB; наступне уточнення користувача **скасувало цей поріг**, чинне значення — 2 GiB. Тодішні значення та preflight нижче збережені як історія, не як активна конфігурація. RAM залишився ≥1,5 GiB. Поточний успішний тест, emergency limits і коміт driver наведені на початку документа.

Передстартовий Windows sample — **11:48:05,359 Europe/Kyiv**, WSL record — 11:48:05,460:

| Показник | Фактичні байти | GiB | Поріг / результат |
| --- | ---: | ---: | --- |
| C: free | 21 296 521 216 | 19,83393 | ≥10, пройдено |
| Available RAM Windows | 3 151 470 592 | 2,93504 | **≥1,5**, пройдено |
| Committed | 29 294 317 568 | 27,28246 | виміряно |
| Commit limit | 32 625 442 816 | 30,38481 | виміряно |
| Commit headroom | **3 331 125 248** | **3,10235** | ≥4, відмова |

```text
AssertionError: Initial commit headroom <4 GiB
created_jobs=0, jobs={}, passed=false
```

Сценарій зупинився до будь-якого job POST; немає нових job_id/server_id/native PID, snapshots або результатів. Це **передстартова відмова**, а не провал запущених обчислень. БД read-only підтвердила 41 задачу й 0 queued/dispatching/running. Наявні clips не готувалися знову. CV-ядро, ABI, залежності, shutdown та системні налаштування не змінювалися; власні services доступні, slots вільні. Автоматичного повтору не було.

Один Windows collector PID **23052**, 0,5 с, записав **261 sample за 130,500 с**, 11:47:58,860–11:50:08,859 Kyiv. CPU 3,422 с, stderr порожній, завершений stop marker. WSL JSONL містить preflight та 10 after-samples. Мінімально зафіксований commit headroom — 3 156 938 752 байти; на останньому sample — 3 370 455 040. Вимога 4 GiB не виконана у preflight. Останній C: free — 21 294 292 992 байти (19,83186 GiB), 5-GiB reserve не порушений. Причина недостатнього запасу commit не встановлювалася повторним аудитом і не приписується проєкту.

Артефакти поза Git: `%LOCALAPPDATA%\MonoTrajectoryWeb\diagnostics\20261103T094758Z\windows.jsonl`, `exit.json`, порожній `error.txt`; `$DATA/reports/parallel-short-20261103-ram15.json`, `.jsonl`, `parallel-short-20261103-ram15-resource-summary.json`. У самому evidence збережені фактичні thresholds і preflight до assert. Перевірені кліпи/валідатор з попереднього етапу повторно не тестувалися. Відтворення цієї передстартової перевірки було командою (output вже існує, не перезаписувати):

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python backend/tests/parallel_short.py --context /home/demus/.local/share/monotrajectoryweb/web/reports/monitored-r5-current.json --output /home/demus/.local/share/monotrajectoryweb/web/reports/parallel-short-20261103-ram15.json
```

На цьому історичному кроці було закомічено тільки конфігурацію й звіт; driver тоді лишався незакоміченим через неперевірений success path. Згодом commit-поріг змінено на 2 GiB, пара пройшла, driver збережено у 955d435. Чинні значення, команди й докази наведені на початку документа; старі 1,5/10/4 GiB більше не застосовуються.

## Історія підготовки фрагментів і попередній RAM-поріг, 03.11.2026

У цій попередній перевірці використовувався RAM-поріг 3 GiB; він **скасований** наступним уточненням користувача й більше не застосовується. Чинні 1,5/10/4 GiB наведені вище. Невеликий фоновий рух C: не є відмовою; робочий backing reserve лишається 5 GiB. Конфігурацію продукту, системи, CV, ABI та залежності не змінено. Повний storage audit, 60-секундний baseline й незмінені тести scheduler не повторювалися.

| Момент, Europe/Kyiv | C: free, GiB | Available RAM, GiB | Commit headroom, GiB |
| --- | ---: | ---: | ---: |
| Початковий замір 10:57:25,593 | 19,9651 | 4,4976 | 8,1954 |
| Перший sample collector 10:58:02,261 | 19,9653 | 4,3344 | 8,0364 |
| Найближчий попередній sample перед gate, 11:02:35,261 | 19,9118 | **2,4689** | 5,9738 |
| Останній sample 11:04:28,761 | 19,9106 | 2,5251 | 6,0268 |

Запущені тільки наявні project services через `wsl -d Ubuntu --exec bash scripts/start`: власний PostgreSQL, API PID 464, compute-1/2 PID 467/471, Nginx 478, dispatcher 480; обидва slots вільні. Відеопідготовка:

```powershell
wsl -d Ubuntu --exec /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python scripts/video-short-fixtures.py
```

Script перевірив SHA256 контрольного source, FFmpeg перекодував перші 300/360 кадрів у H.264 CRF18 yuv420p, без resize, без аудіо/depth/ground truth. `-threads 2` обмежує лише цей FFmpeg encoder; CV flags не змінювалися. PyAV декодував кожний готовий фрагмент і підтвердив кількість, 640×480 та PTS i/30 від нуля. Явний parameters.json копіюється з перевіреного TUM fixture; автоматичного профілю для інших відео немає. Encoding lossy, тому кількість SLAM-поз/точок не очікується тотожною початковому TUM.

| Native datasets artifact | Bytes | SHA256 |
| --- | ---: | --- |
| `freiburg1_xyz-rgb-30fps-short-10s.mp4` | 3 936 718 | `5e70382fc9c28f0cb82ea1e886da8654411675679926cec78fc32796571292e4` |
| `freiburg1_xyz-rgb-30fps-short-12s.mp4` | 4 679 059 | `8c49efcbd24cca98ad7a278ffc2990cc9bfcbe0fd2bf94748f020aa33a949ec3` |

Для кожного збережено `.parameters.json` і `.provenance.json` з реальною FFmpeg command, межами [0,10)/[0,12), кількістю та timestamps. Відео поза Git у наявному `$CV_ROOT/datasets`; повторно перекодовувати їх не потрібно. Script не перезаписує наявні clips.

Collector Windows PID **18412** працював один, кожні 0,5 с, **774 samples за 387,000 с**; CPU 12,438 с, stderr порожній, зупинений власним marker. Збережено C:/RAM/commit, розміри файлів і процеси до/під час/після підготовки. За весь запис C: −58 793 984 байти; pagefile, VHDX logical/allocated та знайдені swap на початку/кінці незмінні. Committed у відповідних endpoint samples — 22,3484 → 24,3580 GiB при limit 30,3848 GiB. Джерело зміни RAM/commit не встановлено; SLAM ще не запускався, коротший ролик не видається за доказ меншого RAM.

Сценарій однієї пари зупинився **до POST** о 11:02:35:

```text
AssertionError: Initial Windows Available RAM <3 GiB
created_jobs=0, jobs={}, passed=false
```

Початковий замір ресурсу проходив, але перед запуском задач уже не проходив. Точний preflight object у першій версії driver не зберігся до assert; наведено найближчий попередній Windows sample (за 0,006 с до started_at), а не вигаданий синхронний замір. Порядок збереження виправлено для наступної перевірки. У native JSONL залишилися 10 реальних post-event samples без native-процесів; обчислювальних job/PID/snapshots/results цього прогону немає. Автоматичного повтору не було.

`backend/tests/check_parallel_results.py` тепер приймає expected_frames, CLI читає його з evidence (для старих звітів default 798); додатково перевіряє frame type/tracking state, межі final IDs і final timestamps. Змінений validator **read-only** пройшов на двох збережених результатах 798 кадрів; окрема read-only перевірка відхилила помилково задані 300 кадрів для такого результату. Це регресійна перевірка зміненого коду, не новий CV-прогін і не нове приймання R5. БД: **41 задача, 0 queued/dispatching/running**, нових немає.

Evidence поза Git:

- Windows `%LOCALAPPDATA%\MonoTrajectoryWeb\diagnostics\20261103T085801Z\windows.jsonl`, `exit.json`, порожній `error.txt`.
- `$DATA/reports/parallel-short-20261103.json` / `.jsonl` — failed preflight, жодного POST.
- `parallel-short-20261103-resource-summary.json` — синхронні діапазони й nearest-before sample.
- `short-validator-existing-results-20261103.json`, `short-validator-count-contract-20261103.json` — read-only validation, no jobs.

Локальний driver `backend/tests/parallel_short.py` підготовлено для рівно однієї пари, але success path із двома новими результатами ще не перевірено; його не включено в коміт перевірених fixtures/validator/docs. Наступний дозвіл продовжити потребує лише свіжого resource check і нового evidence filename. Clips уже готові, залежності/ядро не будувати, старі full-suite/isolation-only commands нижче зараз не запускати. Після проходження gate потрібні два різні server_id, Nginx handoffs, реальні native CPU/frame progress і два валідовані результати; поточна підготовка цього не доводить.

## Історичний пасивний baseline, 03.11.2026, до уточнення порогів

Після перерваного сеансу попередній неповний baseline не використано: він містив лише 18 завершених записів. На початку поточної перевірки WSL уже мав новий сеанс роботи; старий Windows collector завершився. Агент не перезапускав WSL/Windows, не змінював системні параметри й не запускав SLAM. Нова перевірка — **10:38:55,760–10:39:56,455 Europe/Kyiv**, 61 WSL sample, 60,011 с загального monotonic wall. Різниця monotonic між першим і останнім вимірами — 59,999 с; UTC у WSL коригувався. Живих `mono_export` не було. Zombie PID явно відрізняються від живих процесів у скрипті спостереження.

`scripts/resource-monitor-windows.py` — один довгоживучий Python-процес Windows, PID **12364**, інтервал 0,5 с. Він напряму викликає Win32 API через ctypes, без WMI, subprocess або PowerShell на кожен показник. Запуск виконано через `Start-Process -WindowStyle Hidden`; єдиний його дочірній процес у baseline — console host, PID 33552. Collector записав 264 samples від 10:38:43,984 до 10:40:55,482 Kyiv: до/під час/після baseline. Завершений через власний stop marker; stderr порожній. Зміни процесів інших засобів діагностики записуються окремими PID/PPID, не приписуються захисту.

| Показник у 60-секундному baseline | Початок → кінець | Фактично спостережений діапазон / зміна |
| --- | --- | --- |
| C: free, WSL | 21 453 660 160 → 21 445 984 256 байтів | **19,98028 → 19,97313 GiB; −7 675 904 байти (−7,3203 MiB)** |
| Pagefile, логічний розмір | 16 312 721 408 → 16 312 721 408 | без зміни; allocated недоступний, Win32 error 32, JSON null |
| Ubuntu ext4.vhdx, логічний / allocated | 17 942 183 936 / 17 942 183 936 | обидва без зміни, 16,70996 GiB |
| Три знайдені host swap.vhdx | кожний 37 748 736 байтів | кожний logical/allocated без зміни, по 36 MiB; належність активному `/dev/sdc` не встановлена |
| Windows swapfile.sys, логічний | 16 777 216 → 16 777 216 | без зміни; allocated недоступний, error 32 |
| Windows Available RAM | 665 456 640 → 1 021 329 408 | **523 509 760–1 033 715 712 байтів** (0,488–0,963 GiB) |
| Windows committed memory | 29 079 003 136 → 28 683 522 048 | 28 666 802 176–29 264 687 104 (26,698–27,255 GiB) |
| Windows commit limit | 32 625 442 816 → 32 625 442 816 | 30,385 GiB, без зміни |
| Collector RSS / private | близько 17,3 / 10,8 MiB | RSS 18 124 800–18 186 240; private 11 313 152–11 423 744 байти |
| PowerShell / pwsh, усі видимі процеси | 1 / 0 → 1 / 0 | максимум 2 / 0 через короткі команди перевірки; накопичення не зафіксовано |

У межах усього 132-секундного Windows запису C: зменшився на 17 682 432 байти (16,863 MiB), мінімум 21 427 216 384 байти. Available RAM мінімум той самий; максимум committed — 29 343 277 056 байтів. Це **виміряні samples**, не приховані апаратні піки між ними. Collector витратив 6,109 CPU-секунди за 132,001 с; максимально один sample займав 0,0572 с. Розміри діагностичних журналів невеликі, але сам запис не є абсолютно нульовим I/O. Повне джерело фонового зменшення C: не встановлено. Незмінні виміряні великі файли не пояснюють його; дані не доводять провину pagefile, VHDX, проєкту або захисту. Нових job/decoded-кадрів не створено; 41 наявний каталог задач збережено.

### Перевірка C: guard

- Поточна конфігурація: native reserve 2 GiB, backing mount `/mnt/c`, backing reserve **5 × 1024³ = 5 368 709 120 байтів**. Mount — Windows `C:\\`, не Linux root чи інший диск.
- `backend/mtw/capacity.py` викликає Linux `shutil.disk_usage`, тобто `os.statvfs`; free = `f_bavail × f_frsize` у байтах. Текстової Windows-відповіді або розбору локалізованих чисел немає. Перевірено також граничний випадок «рівно reserve» / «reserve − 1 байт».
- Guard викликається перед decode й на кожен decoded frame, приблизно кожні 0,1 с у кожному активному compute manager та на upload chunks API (1 MiB). За одночасного decode двох 30-fps відео це приблизно 60 frame-перевірок/с плюс близько 20 manager-перевірок/с. Це звернення до файлової системи, **не 80 запусків PowerShell/с**; фактичний темп decode може відрізнятися від fps.
- 100 реальних `ensure_space()` викликів у WSL: 0 зафіксованих `subprocess.Popen/os.system/os.posix_spawn`, середня тривалість 0,334 мс, максимальна 0,887 мс. Самі scripts спостереження також не імпортують subprocess. Це разом із переглядом коду не виявило Windows helper на один вимір.
- `OSError`/`TimeoutError` не перетворюються на нуль або успіх, а поширюються. Compute exception handler завершує власну групу процесів. API перетворює low-space ValueError на 507; інша помилка вимірювання може дати 500, upload cleanup збережено. **Власного тайм-ауту навколо statvfs немає**: завислий системний виклик міг би затримати цей guard/manager; фактичного такого зависання не зафіксовано. Інжектований TimeoutError у тесті підтверджує лише поширення вже отриманої помилки, не існування таймера.
- Windows `GetDiskFreeSpaceExW` звірено з незалежним `GetDiskFreeSpaceW` (кластери × сектори × байти): у 121 sample baseline різниця **0 байтів**. WSL і останній асинхронний Windows sample відрізнялися максимум на 831 488 байтів; Windows sample мав вік до 0,976 с. Це не синхронні системні виклики, відмінність не видається за помилку одиниць.
- Collector використовує `GetPerformanceInfo`, Toolhelp process enumeration та `GetProcessMemoryInfo`; незчитані значення — null з error code. Захищений wslservice мав error 5, його пам'ять не підставлена нулем. Монітор RSS не зростав від початку до кінця baseline. Зростання кількості PowerShell/дочірніх процесів як причину попереднього провалу **не доведено**.

Цільові тести guard: **2 passed, 0,002 с**, без CV або job. Після уточнення обробки помилки enumeration collector додатково пройшов self-check: один процес, 3 samples за 1,5 с, усі disk/memory/process API успішні, два disk API рівні, процес завершився. Це не другий baseline і не обчислювальна спроба. Observer помічає недоступний повторний statvfs як null/error, а zombie окремо; початковий успішний шлях підтверджений повним baseline.

### Відтворення й артефакти

Запущений у Windows collector (шляхи наведені через змінні, без зміни системи):

```powershell
# $diag — новий локальний каталог diagnostics; $vhdxPath — фактичний наявний Ubuntu ext4.vhdx.
$monitorScript = Join-Path (Get-Location) 'scripts\resource-monitor-windows.py'
$arguments = '"' + $monitorScript + '" --output "' + $diag + '\windows.jsonl" --vhdx "' + $vhdxPath + '" --stop-file "' + $diag + '\stop" --seconds 1800 --interval 0.5'
Start-Process (Get-Command python.exe).Source -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput ($diag + '\exit.json') -RedirectStandardError ($diag + '\error.txt')
```

WSL baseline читає context JSON з `windows_log` (Windows path), що створений після запуску collector. Імена output мають бути новими; скрипти не перезаписують наявні JSONL.

```powershell
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python scripts/resource-baseline-wsl.py --context /home/demus/.local/share/monotrajectoryweb/web/reports/monitored-r5-current.json --output /home/demus/.local/share/monotrajectoryweb/web/reports/monitored-r5-baseline-20261103-morning.jsonl
wsl -d Ubuntu --exec env PYTHONPATH=backend /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -m unittest discover -s backend/tests -p test_capacity.py -v
```

Raw Windows: `%LOCALAPPDATA%\MonoTrajectoryWeb\diagnostics\20261103T083843Z\windows.jsonl`; там само `exit.json`, порожній `error.txt`, `selfcheck.jsonl`. Native reports: `monitored-r5-baseline-20261103-morning.jsonl`, `monitored-r5-guard-review-20261103.json`, `monitored-r5-20261103-summary.json`, `monitored-r5-postcheck-20261103.json`. Останній підтвердив 36/36 старих signatures із попереднього evidence, 41 наявний job-каталог, 0 нових jobs, 0 native-процесів, незмінні п'ять хешів CV/exporter/dependencies; C: 21 400 764 416 байтів. Журнали залишаються поза Git. Обидва collectors завершені, нічого не видалено.

### Рішення перед обчисленням

Залишок понад 10 GiB і 5-GiB reserve не порушений. Однак у простої є невелика від'ємна зміна, тому виконано умову користувача **не запускати обчислення при падінні C: вже у baseline**. Це консервативний стоп за поточним правилом, а не доказ аварійної тенденції; відхилення 7,32 MiB не ототожнюється з попередньою втратою близько 12 GiB. Нових HTTP POST job, фрагментів відео, SLAM-прогонів, Nginx handoff, PID tracking або R5 результатів у цій перевірці немає. CV/dependencies/ABI/shutdown і системні налаштування не змінено, залежності не перебудовано.

Після цього користувач уточнив правило: −7,32 MiB/хв не є блокером і не потребує нового загального baseline. Чинні практичні пороги, вже готові 300/360-frame clips і остання RAM-відмова наведені на початку документа. Далі дозволена лише одна коротка пара з моніторингом, без автоматичних повторів. Скорочення відео не гарантує меншого RAM.

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
