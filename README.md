# MonoTrajectoryWeb

Локальний вебзастосунок для обробки записаного монокулярного RGB-відео через ORB-SLAM3. Показує реальний прогрес, оброблений кадр, траєкторію камери та розріджені 3D-точки; підтримує історію, скасування, облікові записи, чергу/ETA, балансування, автоматичне розширення й адміністративний моніторинг. Масштаб координат відносний. Вихідна умова — [WebProject.txt](WebProject.txt).

## Передумови

- Ubuntu 24.04 у WSL, PostgreSQL 16, Nginx, OpenSSL і FFmpeg; systemd-nspawn для третього compute-сервера. Docker не використовується.
- Уже підготовлені ORB-SLAM3/`mono_export`, native залежності, backend venv, Node/frontend dependencies та UI build. Звичайний запуск не потребує повторного збирання CV.
- Великі залежності, дані БД, відео й результати зберігаються поза Git: `/home/demus/.local/share/monotrajectoryweb`. Конфігурація — [config/local.toml](config/local.toml), міграції — `backend/migrations/versions`.

Для початкової підготовки існують `scripts/setup`, `scripts/setup-ui`, `scripts/setup-https` та `scripts/migrate`; вони не замінюють підготовку CV-ядра. Для перевірки зміненого frontend використовується `scripts/build-ui`. Ключі, сертифікати, паролі та дані БД до Git не додаються.

## Запуск і зупинка

Із PowerShell у корені цього репозиторію, у підготовленому середовищі:

```powershell
wsl -d Ubuntu --exec bash scripts/start
wsl -d Ubuntu --exec bash scripts/status
```

Сайт: **https://127.0.0.1:8443**. Панель адміністратора: **https://127.0.0.1:8443/admin**. Усі listeners прив'язані до loopback; HTTP:8080 лише перенаправляє GET/HEAD на HTTPS.

Локальний CA не встановлюється у довіру автоматично. Користувач може звірити SHA-256 через `scripts/status` і вручну імпортувати публічний `\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\web\tls\ca.crt.cer` до Windows **Current User → Trusted Root Certification Authorities**. Приватні ключі не імпортувати. До встановлення довіри Chrome може показувати попередження сертифіката.

Зупинити всі служби, а потім, за потреби, власний PostgreSQL:

```powershell
wsl -d Ubuntu --exec bash scripts/stop
wsl -d Ubuntu --exec bash scripts/stop postgres
```

Задачі, власники, відео й результати при штатній зупинці зберігаються.

## Основні ліміти

- Одне відео: **64 MiB, 40 секунд, 1200 кадрів, до 1280×720**; execution budget **1–180 секунд**, без очікування в черзі. Завершення процесів та очищення можуть додати час фіналізації.
- Глобально **100 збережених задач** усіх станів і окремо **10 незавершених**, включно з активними. Історія лише власна: 20 на сторінку, максимум 100.
- Базово два compute-сервери, максимум три при автоматичному запуску nspawn-контейнера; один слот на сервер. Контейнер використовує спільне WSL kernel/RAM. ETA приблизний; автоматичного scale-down немає.
- Дисковий резерв: **5 GiB на C:** та **2 GiB у native FS**. RAM/commit — інформаційні показники. Нові задачі очищають робочі decoded-кадри після завершення процесів, зберігаючи input, previews та результати; автоматичного видалення старих задач немає.

## Балансування задач

Scheduler у транзакції PostgreSQL бере найстарішу готову задачу (`created_at, id`) і вибирає сервер зі станом `ready` та свіжим heartbeat. Завантаження = **(running + зарезервовані dispatching) / місткість**. Кожен сервер має один слот: зайнятий виключається, серед однаково вільних вибирається той, кому найдовше не призначали роботу (`last_assigned_at`, потім server_id). Якщо місця немає, задача залишається queued. Advisory lock і наявний unique index одночасно захищають призначення задачі та конкретного server_id від конкурентних Dispatcher. CPU/RAM не впливають на вибір.

Доставка: **Dispatcher → приватний Nginx:8081 → `/internal/compute/<server_id>/execute` → саме призначений compute**. Маршрути створюються лише з allowlist compute-1/2/3, третій підключається після readiness. Round Robin залишається лише для агрегованої health-перевірки, не для виконання задач. Публічний HTTPS listener блокує `/internal/`; compute вимагає службовий ключ, відповідний server_id, чинний token і стан. Повтори після мережевої помилки та restart Dispatcher використовують той самий сервер/token, без другого worker. Недоступність сервера не звільняє неоднозначну доставку й не пересилає її іншому серверу.

Cancel/timeout/startup error звільняють слот після підтвердження завершення відповідної групи процесів. Legacy `dispatching` без server_id або з невідомим призначенням консервативно блокує нові призначення; автоматичного replay/backfill немає. Cancel позначає запит, але не звільняє неоднозначний запуск. Такі записи потребують локальної перевірки процесів і ручного узгодження стану оператором. ETA враховує зарезервовані слоти саме їхніх серверів; autoscaler враховує running і dispatching базових серверів. Журнал `web/logs/dispatcher.log` містить JSON `scheduler_decision`: job_id, server_id, candidates/capacity/running/reserved/load та причину; без token або ключів.

Для оновлення підготовленого середовища застосувати сумісну міграцію `0007_slot_assignment` (лише nullable поле справедливого чергування) **перед reload Python-служб**. Перед зупинкою дочекатися завершення наявних робіт:

```powershell
wsl -d Ubuntu --exec bash scripts/migrate
wsl -d Ubuntu --exec bash scripts/stop
wsl -d Ubuntu --exec bash scripts/start
```

Автоматизовані перевірки використовують одноразові PostgreSQL schemas, без SLAM:

```powershell
wsl -d Ubuntu --exec env MTW_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests /home/demus/.local/share/monotrajectoryweb/web-venv/bin/python -B -m unittest discover -s backend/tests -v
```

`backend/tests/slot_e2e.py prepare`, потім `run --root <повернений native ROOT>` — окремий явний HTTPS-прогін трьох нових задач на готових 300/360 кадрах із збереженням попередніх rows/files/core та доказів PID/CPU/progress/NGINX/рішення. Він використовує збережений локальний test account, перевіряє ліміти/диск, не робить автоматичних повторів і зберігає JSON/JSONL поза Git.

Перевірка 11.11.2026: 113 різних backend test methods пройшли в сукупності основного suite та цільових повторів після оновлення очікуваного migration head. Реальний HTTPS-прогін зупинив дисковий guard: C:6,42→менше5 GiB під час ініціалізації двох native-процесів. Доставка на конкретні сервери підтверджена, обидві задачі failed/процеси зібрані, усі75 попередніх задач і файли збережені; додано2, тепер77. Третю задачу не подано, нове наскрізне приймання R5 з валідними результатами **ще не підтверджене**. Докази — native `web/reports/slot-balancing-38f1432af1a14a8397ccd4cf806601bf`; повторів не було. RAM/commit не блокували запуск; системні налаштування не змінювалися.

## Мінімальна демонстрація

1. Запустити служби, перевірити `scripts/status`, відкрити сайт і створити власний обліковий запис або увійти. Типового пароля немає.
2. Обрати готовий контрольний ролик і paired calibration JSON через поля завантаження:

   ```text
   \\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\datasets\freiburg1_xyz-rgb-30fps-short-10s.mp4
   \\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\datasets\freiburg1_xyz-rgb-30fps-short-10s.parameters.json
   ```

3. Створити задачу, спостерігати етапи, лічильник кадрів і поступову карту; після завершення відкрити результат з історії та оновити сторінку. Для стороннього відео потрібне власне явне калібрування, TUM-профіль автоматично не застосовується.
4. За потреби продемонструвати скасування окремої активної задачі. Адмінпанель доступна після явного надання ролі вибраному існуючому login:

   ```powershell
   wsl -d Ubuntu --exec bash scripts/admin-role --login EXISTING_LOGIN --grant
   ```

Звичайний користувач бачить лише власні задачі. Старі задачі без власника не призначаються автоматично; локальна команда `scripts/assign-unowned --login EXISTING_LOGIN --all-unowned --dry-run` дозволяє спочатку переглянути обсяг явного призначення.
