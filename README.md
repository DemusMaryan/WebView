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
