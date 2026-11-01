# CV-прототип MonoTrajectoryWeb

Виконується лише Промпт 2 у нативному WSL Ubuntu. **Baseline успішний; покадровий експортер ще не перевірено.** Промпт 3 не виконується.

## Перевірений baseline

31 жовтня 2026 року `mono_tum` успішно пройшов стандартний цикл `freiburg1_xyz` без viewer, зі стандартними shutdown та playback pacing. Код завершення — 0, зависання й тайм-ауту не було.

| Показник | Факт |
| --- | --- |
| Вхід | 798 RGB-кадрів, 640×480 |
| Результат | 38 валідних поз ключових кадрів; скінченні числа й норми кватерніонів перевірені |
| Час процесу | 44,027973 с: словник, читання кадрів, tracking/mapping, стандартний pacing, shutdown і збереження KF-траєкторії |
| Покадрові валідні пози | Baseline окремо не експортує; кількість не виміряна |
| Точки / проміжні JSONL | Baseline їх не експортує; кількість фінальних точок не виміряна |
| Початок першої спроби | 31.10.2026 12:10:38 Europe/Kyiv (10:10:38 UTC) |
| Перший успішний запуск | 12:48:07,997–12:48:52,025 Europe/Kyiv |
| Час від початку до baseline-успіху | 38 хв 14 с; подальші перерви не є часом обчислення |

44,03 с зі стандартним pacing не є чистою швидкістю обчислення. 38 ключових кадрів не є кількістю валідних покадрових поз.

Докази у WSL: `/home/demus/.local/share/monotrajectoryweb/runs/baseline-upstream-20261031T1048/` — `baseline-summary.json`, `process.log`, `KeyFrameTrajectory.txt`, `TUM1-bgr.yaml`. Журнал збирання зі стандартним shutdown: `logs/build-upstream-shutdown.log`, остання ціль — зв’язування `mono_tum`.

## Версії та розміщення

| Компонент | Зафіксована версія |
| --- | --- |
| Linux | Ubuntu 24.04.3 LTS, WSL2, x86_64 |
| Компілятор / CMake | g++ 13.3.0 / CMake 3.28.3; C++17 |
| OpenCV / Eigen / Boost | 4.6 / 3.4 / 1.83 |
| Ядро | Офіційний [ORB-SLAM3 4452a3c4ab75b1cde34e5505a36ec3f9edcdc4c4](https://github.com/UZ-SLAMLab/ORB_SLAM3/tree/4452a3c4ab75b1cde34e5505a36ec3f9edcdc4c4) |
| Pangolin | [v0.8 aff6883c83f3fd7e8268a9715e84266c42e2efe3](https://github.com/stevenlovegrove/Pangolin/tree/aff6883c83f3fd7e8268a9715e84266c42e2efe3) |

Точні версії Linux-пакетів — `scripts/cv/ubuntu-packages.tsv`. Великі вихідники, build, datasets і runs лежать у `/home/demus/.local/share/monotrajectoryweb`. Основний репозиторій не переносився й не дублювався. Docker, PostgreSQL, Nginx та вебчастина для цього етапу не запускаються.

## Вхід і калібрування

Джерело — офіційна [TUM freiburg1_xyz](https://cvg.cit.tum.de/data/datasets/rgbd-dataset/download). [Параметри камер і формат](https://cvg.cit.tum.de/data/datasets/rgbd-dataset/file_formats). SHA-256 архіву: `a0236d97b8c30cd93b653656d2b6c293ff7c982a4130ef2a1a8beecdb124ef98`.

Витягнуто тільки `rgb/` і `rgb.txt`; depth та ground truth не подаються ядру. Калібрування — офіційне `Examples/Monocular/TUM1.yaml` на зафіксованому commit, без зміни розміру кадрів:

| Параметр | Значення |
| --- | --- |
| fx / fy | 517.306408 / 516.469215 |
| cx / cy | 318.643040 / 255.313989 |
| k1 / k2 / k3 | 0.262383 / -0.953104 / 1.163314 |
| p1 / p2 | -0.005358 / 0.002628 |

У `TUM1-bgr.yaml` змінено тільки `Camera.RGB: 0`, бо `cv::imread` повертає BGR. Intrinsics і дисторсія незмінні. SHA-256 YAML: `7e102220272166ff3afc8d15be49be8a5ca03f1823bf31119468ee2b984b9ba9`.

## Застосовані й відкладені патчі

- `orbslam3-cxx17.patch` — тип лічильника GBA `bool → int`, необхідний через заборону `++bool` у C++17.
- `orbslam3-headless.patch` — лише `bUseViewer=false` у `mono_tum`.
- Pangolin збирається з локальним `-include cstdint` для GCC 13; його вихідники незмінні.
- `orbslam3-shutdown.patch` і `orbslam3-offline-measurement.patch` збережено окремо, **не застосовано**. `System.cc` і `LoopClosing.cc` baseline побайтово збігаються з upstream.
- `LD_LIBRARY_PATH` налаштовується тільки для дочірнього процесу. Це усунуло перший runtime-збій `libpango_windowing.so: cannot open shared object file`, exit 127, без перекомпіляції й глобальних налаштувань.

## Відтворення

PowerShell, робоча папка поточного репозиторію:

```powershell
wsl -d Ubuntu --exec bash scripts/cv/run-baseline.sh
```

Скрипт перевіряє архів і кадри та створює новий каталог запуску без перезапису результатів. Для підготовки іншої Ubuntu перед цим:

```powershell
wsl -d Ubuntu -u root --exec bash scripts/cv/install-deps-wsl.sh
wsl -d Ubuntu --exec bash scripts/cv/build-wsl.sh baseline
```

На наявному середовищі залежності повторно збирати не потрібно. CMake збирає `mono_tum` та його залежності, не всі приклади.

## Що залишається в Промпті 2

Потрібно завершити й перевірити тонкий експортер: запис `frame` на кожний вхідний кадр; індекси 0–797; статус і `Twc=null` без валідного tracking; сегменти для різних систем координат карти; справжні поза/точки під час виконання. Конвенція — `Twc = inverse(Tcw)`, масштаб відносний, без заяв про метри.

Фінальні `trajectory.json`, `map_points.json`, графік, кількість валідних покадрових поз, контракт обрізання та потокова узгодженість поки не перевірені. R1–R8 вебзастосунку залишаються запланованими. Автор і комітер локальних комітів — підтверджена ідентичність `versal217 <versaldata@gmail.com>`.

Push не виконувався.
