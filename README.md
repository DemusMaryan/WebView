# MonoTrajectoryWeb

Локальний вебзастосунок для обробки записаного монокулярного RGB-відео через ORB-SLAM3. Користувач передає відео та явне калібрування, бачить реальний прогрес, RGB-кадр, траєкторію камери й розріджені 3D-точки, зберігає результат або скасовує задачу. Координати мають відносний масштаб; живий потік, повторна локалізація у збереженій кімнаті та щільна модель поза темою.

## Перевірений стан

R1–R8 прийняті в локальних сценаріях Промптів 1–13. [Вимоги](docs/requirements.md), [точні докази фінального приймання](docs/final-acceptance.md), [пояснення та захист на 5–7 хвилин](docs/how-it-works.md).

Останнє обчислювальне приймання — **06.11.2026**: задача `4c718f50-153f-4c61-a33e-f4ce8ce42a94`, 300 кадрів, 252 валідні live-пози, 226 фінальних поз, 2695 точок, без обрізання. Worker wall 32,205 с включає декодування, завантаження словника, native processing, експорт і валідацію, **без playback pacing**. Це не чистий час SLAM; manager cleanup відбувається пізніше й входить до окремого `Job.execution_seconds`. Після повного restart PostgreSQL і служб збережено 73 задачі, власників і файли; на той момент active=0, legacy51 owner=NULL, admins=0. Ці числа — датований результат перевірки, не поточна телеметрія.

Звірка документації 09.11.2026 не запускала нових CV-задач і не змінювала ядро, залежності, конфігурацію чи дані. Історичні звіти нижче збережені для доказів; їхні старі HTTP-команди й тимчасові блокери не є поточними інструкціями запуску.

## Запуск у підготовленому середовищі

З PowerShell у поточній робочій папці:

```powershell
wsl -d Ubuntu --exec bash scripts/start
wsl -d Ubuntu --exec bash scripts/status
```

Сайт — **https://127.0.0.1:8443**, панель адміністратора — **https://127.0.0.1:8443/admin**. Увійти або створити власний обліковий запис; стандартного пароля немає. Усі listeners прив'язані до loopback. HTTP:8080 лише перенаправляє GET/HEAD на HTTPS; службовий балансувальник працює на 8081, compute — 8101/8102 і контейнерний 8103 після готовності, API — 8000, власний PostgreSQL — 55432.

Зупинка служб; потім, за потреби, також власного PostgreSQL:

```powershell
wsl -d Ubuntu --exec bash scripts/stop
wsl -d Ubuntu --exec bash scripts/stop postgres
```

Start/stop керують лише процесами проєкту та зберігають задачі й файли. Для звичайного старту не потрібно повторювати setup, build UI або збирання CV.

Передумови: Ubuntu у WSL, підготовлений ORB-SLAM3/mono_export і залежності, PostgreSQL 16, Nginx, OpenSSL, FFmpeg, backend venv, frontend dependencies/збірка та systemd-nspawn для R7. Налаштування — `config/local.toml`; міграції — до `0006_admin_role`. Для початкової підготовки окремо існують `scripts/setup`, `setup-ui`, `setup-https`, `migrate`; їхнє призначення описане в тематичних документах. Залежності й ABI не перебудовувати без конкретної потреби. Docker/Compose не використовуються.

## Відео, ліміти й доступ

- Вхід: відеофайл та JSON калібрування саме його камери; TUM-профіль не застосовується до сторонніх відео автоматично.
- Межі однієї задачі: **64 MiB, 40 с відео, 1200 кадрів, 1280×720, execution 1–180 с**. Перевіряються й фактично декодовані кадри; resize не виконується.
- Глобально **100 збережених задач усіх станів**, окремо **10 pending**, включно з активними. Історія лише власна: 20 на сторінку, максимум 100.
- Два базові compute, максимум три при автоматичному розширенні; один слот на сервер. Черга FIFO, ETA приблизний. R7 використовує справжній nspawn-контейнер зі спільним WSL kernel/RAM; автоматичного scale-down немає.
- Нові задачі очищають робочі decoded PNG після завершення процесів; input, previews, JSONL та результати зберігаються. Початкові 29 задач не очищалися, автоматичного retention немає.
- Дані й великі залежності лежать у `/home/demus/.local/share/monotrajectoryweb`, поза Git/OneDrive. VHDX фізично на C:, тому guard зберігає **5 GiB на C:** і 2 GiB у native FS; RAM/commit — інформаційні.

Готовий короткий контрольний ролик і paired calibration у Windows file picker:

```text
\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\datasets\freiburg1_xyz-rgb-30fps-short-10s.mp4
\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb\datasets\freiburg1_xyz-rgb-30fps-short-10s.parameters.json
```

TLS 1.2/1.3 перевірено через `curl --cacert` без `-k`. На останньому прийманні Chrome не довіряв локальному CA; functional Playwright context мав `ignoreHTTPSErrors=true`. **Автоматичного імпорту CA немає**; [ручна довіра та перевірка fingerprint](docs/https.md). Для живої адмінпанелі користувач явно обирає існуючий login і виконує `scripts/admin-role --login EXISTING_LOGIN --grant`; [роль і зняття прав](docs/admin-monitoring.md). Legacy jobs не видаються новому користувачу автоматично; [явне CLI-призначення](docs/authentication.md).

## Актуальні документи

| Потреба | Документ |
| --- | --- |
| Вихідна навчальна умова | [WebProject.txt](WebProject.txt), не редагувати |
| Статус R1–R8 | [requirements.md](docs/requirements.md) |
| Докази, demo-команди, ручні кроки | [final-acceptance.md](docs/final-acceptance.md) |
| Пояснення, карта коду, сценарій захисту | [how-it-works.md](docs/how-it-works.md) |
| Компоненти, слоти й життєвий цикл | [architecture.md](docs/architecture.md) |
| HTTP-контракт, ліміти й пагінація | [api-contract.md](docs/api-contract.md) |
| Accounts/sessions/ownership та legacy CLI | [authentication.md](docs/authentication.md) |
| TLS і ручна довіра CA | [https.md](docs/https.md) |
| Декодування та підготовка контрольного відео | [video-worker.md](docs/video-worker.md) |
| CV baseline/export та застосовані патчі | [cv-prototype.md](docs/cv-prototype.md), [patches/README.md](patches/README.md) |
| Паралельність, черга, масштабування, admin | [R5](docs/parallel-processing.md), [R6](docs/queue-eta.md), [R7](docs/autoscaling.md), [R8](docs/admin-monitoring.md) |
| Правила роботи з репозиторієм | [AGENTS.md](AGENTS.md) |

<details>
<summary>Історичні плани й звіти — для доказів, не щоденного запуску</summary>

- [Початковий чотириденний план](docs/day-plan.md) і [аудит Промпта 5](docs/day1-report.md) фіксують попередній стан; нові функції вже прийняті у наступних етапах.
- [Backend Промпта 3](docs/backend-prototype.md) і [frontend Промпта 4](docs/frontend-prototype.md) містять унікальні job_id, PID, часи, cleanup/restart докази та screenshots. Старий HTTP/no-auth режим у них історичний.
- [Початкові промпти користувача](<MonoTrajectoryWeb-Day-01-Codex-Prompts (1).md>) — вхідний матеріал, не чинна конфігурація або команда виконати етапи заново.
- Звіти R5–R8 містять також невдалі спроби й старі ресурсні пороги. Прийняті результати та чинні межі зазначені окремо; старі невдалі спроби не зараховані за приймання.

</details>

## Перевірки та допоміжні файли

Після зміни frontend перевірений entry point — `wsl -d Ubuntu --exec bash scripts/build-ui`: TypeScript, data/api/queue/admin tests і Vite build. `frontend/package.json` → `npm test` наразі запускає тільки data/api subset. Збережене приймання R8 має 11 frontend tests/build; повторювати build без зміни коду не потрібно.

Backend `test_*.py` — регресійні перевірки; PostgreSQL boundary tests потребують явного `MTW_POSTGRES_TESTS=1` та disposable schema. Конкретні suites й відмінність 58/56 methods описані у [фінальному звіті](docs/final-acceptance.md). Blanket запуск усіх acceptance helpers на робочій БД не є smoke-перевіркою.

Історичні HTTP CLI `e2e.py`, `upload_checks.py`, `parallel_e2e.py`, `parallel_short.py`, `parallel_isolation.py`, CLI-гілка `check_parallel_results.py`, browser harnesses `browser.mjs`/`history-browser.mjs` не адаптовані до чинного login/HTTPS. Їхні старі команди не слід виконувати як поточне приймання. Модулі залишені: сучасні HTTPS helpers імпортують `signature()` з `parallel_e2e.py` та `validate()` з `check_parallel_results.py`.

Новіші acceptance helpers теж прив'язані до конкретних запусків: queue очікував 51 стару задачу, admin — 71, final — 72; autoscale має `--expected-jobs`. Вони не призначені для автоматичного повтору на поточній БД. Скрипти моніторингу, генерації fixtures, CV reproduction й неактивні offline/shutdown патчі збережені для відтворення та попередніх явно замовлених експериментів, не є зайвим runtime-сміттям.

## Git і дані

Автор локальних комітів — підтверджений `versal217 <versaldata@gmail.com>`; перевіряти effective author/committer перед комітом. Публікацію виконує користувач. Паролі, токени, service/proxy keys, приватні TLS-ключі, PostgreSQL, datasets, videos, build і результати не додаються до Git; lock-файли та перевірені малі fixtures відстежуються.

Користувацькі незакомічені зміни файла промптів і `Release` збережені окремо. Документи з доказами не видаляються лише через те, що вони не потрібні процесу API для запуску.
