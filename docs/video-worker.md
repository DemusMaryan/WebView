# Відеовхід перевіреного CV-ядра

Промпт 3 використовує існуючий `mono_export`, без перебудови ядра або залежностей. Офіційний ORB-SLAM3: `4452a3c4ab75b1cde34e5505a36ec3f9edcdc4c4`. `-march=native`, застосовані патчі й порядок shutdown з Промпта 2 збережені.

`scripts/run-video` приймає довільні явно передані `--video`, `--parameters`, `--output`, `--job-id`, `--server-id`. `parameters` — JSON моделі `JobParameters`: обов’язкове калібрування, необов’язкові зменшені ліміти. Для стороннього відео потрібні його власні перевірені intrinsics, дисторсія та роздільна здатність. Профілю за замовчуванням немає. Поточна модель камери — pinhole з радіальною/тангенціальною дисторсією OpenCV; інші моделі потребують окремої підтримки.

PyAV декодує лише відеопотік у RGB PNG без зміни розміру й без пропуску кадрів. Час — PTS кадру мінус перший PTS; відсутні або невпорядковані PTS дають помилку. `rgb.txt` містить монотонні вхідні індекси й фактичні timestamps. Ліміти кількості, тривалості, роздільності, часу та збіг калібрування перевіряються на декодованих кадрах. PNG використовує lossless compression level 1; це не змінює пікселі. У YAML `Camera.RGB=0` відповідає наступному `cv::imread(COLOR)`; `Camera.fps` округлюється до цілого за вимогою Settings ORB-SLAM3, а справжні timestamps залишаються в `rgb.txt`. Новий вихідний каталог не може повторно містити `sequence` чи `native`.

Ядро пише повний `native/snapshots.jsonl`. Worker читає кожен байт один раз, перевіряє frame_id/timestamps/Twc та атомарно замінює `latest.json` не частіше ніж раз на 0,5 с під час tracking. Snapshot має максимум 256 точок і 240 останніх записів покадрової історії; це історичні live-оцінки, не фінальна оптимізована траєкторія. У ній збережено `null`, tracking_state, map_id/segment_id та прогалини. Кадр доступний за точним frame_id. Фінальні траєкторія й карта зберігаються окремо; перевіряються кінцеві атомарні telemetry ядра, незмінність геометрії двох знімків, матриці, кватерніони й контракт ліміту точок.

## Перевірене перетворення TUM

Джерело — тільки 798 RGB-файлів у порядку перевіреного `freiburg1_xyz/rgb.txt`, 640×480; depth/ground truth не використано. Команда у WSL після підготовки Python venv:

```bash
~/.local/share/monotrajectoryweb/web-venv/bin/python scripts/video-fixture.py
```

Скрипт створює тимчасові нумеровані symlink на PNG у native dataset-каталозі та виконує:

```text
ffmpeg -hide_banner -loglevel error -nostdin -n -framerate 30 \
  -i <native temporary links>/%06d.png -an -c:v libx264 -crf 18 \
  -pix_fmt yuv420p -movflags +faststart <native datasets>/freiburg1_xyz-rgb-30fps.mp4
```

Результат: 11 131 364 байти, SHA-256 `37df61791eaefdd063bb4f94791fbbfac1cdb048c7468fde8c57b13c60103fcf`, 798 кадрів, 26,6 с. Нові PTS — 0…26,566666667 із кроком 1/30 с. H.264 CRF18/yuv420p — втратне перекодування без resize; кількості поз/точок не зобов’язані збігатися з початковими PNG. Поряд створено provenance JSON і **явний** parameters JSON з intrinsics/distortion офіційного `TUM1.yaml`. Скрипт не перезаписує наявний fixture.

```bash
root="$HOME/.local/share/monotrajectoryweb"
bash scripts/run-video \
  --video "$root/datasets/freiburg1_xyz-rgb-30fps.mp4" \
  --parameters "$root/datasets/freiburg1_xyz-rgb-30fps.parameters.json" \
  --output "$root/runs/my-new-video-run" --job-id diagnostic --server-id local-cli
```

Перший успішний відеозапуск: `runs/video-adapter-prompt3-v2`, exit 0; 798 frame-записів, 688 валідних live-поз, 424 фінальні пози, 2 824/2 824 точки, ліміт 100 000, обрізання false. Wall time 124,779 с включає декодування PNG з початковим compression level 6 (86,951 с), startup ядра, tracking, JSONL/знімки, shutdown/quiescence та фінальну перевірку. Після цього lossless compression level зменшено до 1 для скорочення часу декодування; результати API-запуску документуються окремо. Wall time не є чистою швидкістю SLAM.

Перша невдала спроба `runs/video-adapter-prompt3`: `Camera.fps parameter must be an integer number, aborting...`, exit 255; виправлено лише YAML-генератор. Сім змістовних unittest перевіряють реальне декодування маленького синтетичного відео, PTS/роздільність, frame/duration/resolution caps, обов’язкове калібрування, інкрементальний JSONL і узгодженість матриці/кватерніона. Синтетичні кадри не використовуються як доказ SLAM.
