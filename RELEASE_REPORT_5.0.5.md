# FargoVPN 5.0.5 — выполненные проверки и границы результата

Дата: 08.10.2026, Asia/Yekaterinburg. Источник: два предоставленных архива, полный diff и фактические проверки. Утверждения исторических AUDIT/TEST_REPORT из архивов не использовались как доказательство.

## Подтверждённая причина на рабочем сервере

На рабочей панели вход выполнен. Без обхода CSP страница содержит 39 пользователей; после запроса с заведомо отсутствующим именем остаются видимыми все 39, счётчик остаётся 0. Push показывает «Не проверено», нажатия не вызывают обработчики. Консоль содержит `Executing inline script violates ... script-src 'self'`.

HTTP-ответ содержит две политики: приложение разрешает `script-src 'self' 'unsafe-inline' https://telegram.org`, внешний слой добавляет `script-src 'self'` и `style-src 'self'`. В тестовом браузере с `bypass_csp=True` поиск даёт 0 строк, очистка возвращает список, кнопки push выполняют запросы. Сервер сообщает корректную VAPID-пару, три сохранённые подписки. Это причинный эксперимент, не изменение production-конфигурации.

| Проблема | Файл/строки 5.0.4 | Изменение относительно 4.9.3 | Почему ломается |
|---|---|---|---|
| Поиск и фильтр | `webapp.py:2487–2529`; раньше скрипт начинался в `webapp.py:2444` | Скрипт остался inline; добавлены пустое состояние и изменения панели. CSP приложения не стал строже | Внешняя CSP блокирует весь inline-скрипт до регистрации input/change/click |
| Push-кнопки и подготовка | `webapp.py:4850–4905`; в 4.9.3 обработчики находятся около `webapp.py:4730–4801` | Добавлены доступность кнопок, журнал, feedbackBusy/loading/toast; обработчики по-прежнему inline | Блокируется регистрация обработчиков, чтение статуса и подготовка подписки |
| Service Worker bootstrap | `webapp.py:1553`, CSP `webapp.py:372–378` | Обе версии используют inline bootstrap и разрешают его в своей CSP | Второй заголовок запрещает inline, даже если первый разрешает |
| Лишние 401 при входе | `static/panel.js:415–416` | Добавлены общие опросы unread/payments | Запускаются до авторизации; в 5.0.5 ограничены panel-page |

**Изменение внешней CSP отсутствует в обоих архивах. Нельзя честно назвать коммит/строку архива, которые добавили этот заголовок.** Конкретный файл nginx/CDN и момент изменения требуют серверного `nginx -T`/диагностического лога. Внешний nginx не менялся в этой сессии. Нет доказательств, что причина — SQL, миграция или VAPID.

Механизм нескольких CSP подтверждён самим экспериментом браузера. Наследование nginx: [официальная документация](https://nginx.org/en/docs/http/ngx_http_headers_module.html). В nginx >=1.29.3 нужно учитывать `add_header_inherit merge`.

## Diff архивов

Изменены 56 файлов. Полный `git diff --no-index` — `full-4.9.3-to-5.0.4.diff`; точная побайтовая инвентаризация непосредственно из tar.gz — JSON/CSV с SHA-256 каждого файла.

| Область | Значимые изменения | Вывод проверки |
|---|---|---|
| `webapp.py` | +240/-94: HTML пользователей, настройки push, новый раздел ссылок, безопасные логи, перенос разделов, ограничение мониторинга | Общая зависимость от inline сохранена |
| `static/panel.js` | +97/-6: CSRF-fetch, общий обработчик POST, toast, безопасные инициализации, опрос платежей | Файл исполняется при строгом script-src; inline страницы блокируется отдельно |
| `push_service.py` | +58/-21: fcntl/atomic VAPID, config lock, валидация ключей, кеш миграций, endpoint_hashes | PostgreSQL+ключи+201/404/410 прошли в обеих версиях |
| `service-worker.js` | +11/-5: allSettled, очистка только собственных кешей, безопасный click URL, offline 503 | Реальная регистрация проверена в Chromium |
| `static/panel.css` | Добавлены hover/active/focus/aria-busy/toast | Видимый отклик сохранён и дополнен spinner |
| `db.py`/`identity_migration.py` | Диагностика производительности/конфигурации/идентификаторов | Не обнаружена миграция схемы, объясняющая блокировку UI |
| Установщик/конфиги | PostgreSQL, сокет и внешний nginx, атомарное сохранение | Нужна проверка действующей конфигурации внешнего слоя |
| Зависимости | `requirements.txt`, `requirements-dev.txt` идентичны; удалён `requirements-lite.txt` | Runtime pin-версии между архивами не менялись |
| БД/миграции | `init_db.py` и четыре SQL-миграции идентичны | Регрессия схемы из этих архивов не подтверждается |
| Manifest | Manifest формируется маршрутами в `webapp.py`, отдельного изменённого manifest-файла нет | Получен HTTP 200 на рабочем сервере |

Полный список различий:

| Файл | Изменение | Категория | + / - строк |
|---|---|---|---|
| `.gitignore` | modified | backend | 2 / 0 |
| `AUDIT_INVENTORY_5.0.2.json` | added | documentation | 1233 / 0 |
| `AUDIT_INVENTORY_5.0.3.json` | added | documentation | 1233 / 0 |
| `AUDIT_REPORT_5.0.2.md` | added | documentation | 248 / 0 |
| `AUDIT_REPORT_5.0.3.md` | added | documentation | 202 / 0 |
| `AUDIT_REPORT_5.0.4.md` | added | documentation | 124 / 0 |
| `CHANGELOG.md` | modified | documentation | 87 / 0 |
| `PANEL_CHECKLIST_5.0.3.csv` | added | documentation | 778 / 0 |
| `README.md` | modified | documentation | 66 / 19 |
| `SECURITY.md` | modified | documentation | 1 / 1 |
| `TEST_REPORT_5.0.0.md` | added | documentation | 76 / 0 |
| `TEST_REPORT_5.0.1.md` | added | documentation | 84 / 0 |
| `VERSION` | modified | version | 1 / 1 |
| `app/VERSION` | modified | version | 1 / 1 |
| `backup.py` | modified | backend | 9 / 1 |
| `config.example.py` | modified | installer/config | 4 / 1 |
| `config_store.py` | added | backend | 18 / 0 |
| `db.py` | modified | database | 17 / 3 |
| `diagnostics.py` | modified | backend | 74 / 220 |
| `identity_migration.py` | modified | database | 3 / 0 |
| `install.sh` | modified | installer/config | 108 / 143 |
| `log_reader.py` | added | backend | 27 / 0 |
| `log_security.py` | added | backend | 38 / 0 |
| `main.py` | modified | backend | 5 / 1 |
| `nginx_panel_guard.py` | modified | installer/config | 6 / 0 |
| `push_service.py` | modified | backend | 58 / 21 |
| `requirements-lite.txt` | deleted | dependencies | 0 / 10 |
| `restore_manager.py` | modified | backend | 3 / 0 |
| `scripts/print_nginx_location.py` | added | backend | 23 / 0 |
| `scripts/run_checks.py` | added | backend | 45 / 0 |
| `scripts/verify_server.py` | added | backend | 82 / 0 |
| `service-worker.js` | modified | frontend | 11 / 5 |
| `services/xui_api.py` | modified | backend | 265 / 27 |
| `static/VERSION` | added | version | 1 / 0 |
| `static/monitoring.js` | added | frontend | 95 / 0 |
| `static/panel.css` | modified | frontend | 12 / 0 |
| `static/panel.js` | modified | frontend | 97 / 6 |
| `tests/qa/backup_fixture.py` | added | tests | 48 / 0 |
| `tests/qa/panel_dom.cjs` | added | tests | 10 / 0 |
| `tests/qa/real_data_dom.cjs` | added | tests | 9 / 0 |
| `tests/qa/render_panel.py` | added | tests | 49 / 0 |
| `tests/qa/run_backup_data.py` | added | tests | 60 / 0 |
| `tests/qa/users_stress.cjs` | added | tests | 10 / 0 |
| `tests/test_backup_490.py` | modified | tests | 3 / 3 |
| `tests/test_panel_regression_492.py` | modified | tests | 1 / 1 |
| `tests/test_regressions_493.py` | modified | tests | 13 / 12 |
| `tests/test_release_475.py` | modified | tests | 11 / 10 |
| `tests/test_release_500.py` | added | tests | 40 / 0 |
| `tests/test_release_500_followup.py` | added | tests | 131 / 0 |
| `tests/test_release_501.py` | added | tests | 377 / 0 |
| `tests/test_release_502.py` | added | tests | 129 / 0 |
| `tests/test_release_503.py` | added | tests | 194 / 0 |
| `tests/test_release_504.py` | added | tests | 44 / 0 |
| `update_manager.py` | modified | backend | 261 / 108 |
| `update_worker.py` | modified | backend | 2 / 0 |
| `webapp.py` | modified | backend | 240 / 94 |

## Исправления 5.0.5

- `static/users.js`: существующая рабочая клиентская логика поиска, фильтров, сортировки и пагинации вынесена без отката новых функций. Удалён неиспользуемый на Users опрос перенесённой рассылки ссылок. Сам раздел «Ссылки подписок» сохранён.
- `static/push.js`, `static/settings.js`: реальные обработчики теперь external same-origin и `defer`; кнопки показывают loading, блокируются и выводят причину ошибки/toast. Вкладки настроек работают без inline.
- `static/panel.js`: Service Worker регистрируется из внешнего файла, версия берётся из meta; защищённые опросы не стартуют на login.
- Версии `VERSION`, `app/VERSION`, `static/VERSION`, README и релизные проверки обновлены на 5.0.5. Hash статики учитывает новые файлы.
- `diagnose.py`/`diagnose.sh`: независимый сбор служб/журналов/файлов/SHA/версий/ресурсов/БД/VAPID/CSP/HTTP. Config читается AST, не исполняется. PostgreSQL использует принудительную read-only транзакцию. Секреты/cookie/Authorization/URL/DSN/private PEM/JWT маскируются; лог приватный, путь печатается последним.
- `diagnostic_jobs.py`, `static/diagnostic.js`: тот же сборщик запускается фоном из панели, реальное состояние и elapsed time, PASS/FAIL/SKIP, скачивание. CSRF, owner check, лимит параллельных запусков и timeout 210 с.
- `/api/diagnostics/read-only`: безопасная проверка формата, непустоты, поиска/статусов и push-счётчика, без init_db/3x-ui/генерации ключей. Онлайн-статус в этом API сознательно не выдумывается. До обновления API отсутствует — SKIP.
- `scripts/print_nginx_location.py`: выдаёт location с CSP приложения. Для всего интерфейса **нужно отдельно исправить внешний строгий CSP**: другие исторические страницы и HTML style/onsubmit ещё содержат inline. Добавить более мягкий второй заголовок поверх строгого недостаточно.

Целиком production-файлы не удалялись. Удалены только перенесённые inline-скрипты и пустой SW bootstrap; их работа находится во внешних JS. Исторические отчёты оставлены как история, не подтверждены заново. Служебные `__pycache__`, тестовые БД/логи, браузер, PostgreSQL-бинарники и данные доступа исключены из релиза.

## Тесты и реальные результаты

Окружение: Windows, Python 3.12.14, Chromium 153.0.8010.12 / Playwright 1.63, переносимый PostgreSQL 17.11 из [EDB](https://www.enterprisedb.com/download-postgresql-binaries), отдельная loopback БД `fargovpn_unicode_qa`, ICU ru-RU. Runtime: FastAPI 0.141.1, Uvicorn 0.52.4, httpx 0.28.1, cryptography 50.0.1, psycopg 3.3.6, SQLAlchemy 2.1.4. Зависимости установлены в тестовую папку, системный Python не изменялся.

| Прогон | 4.9.3 | 5.0.4 со строгой CSP | 5.0.5 |
|---|---|---|---|
| Общая браузерная матрица | 32 PASS, 0 FAIL; registration-sort отсутствует и SKIP | Повторён отказ поиска; ранее расширенный negative run: 8 PASS / 23 FAIL | 34 PASS, 0 FAIL; обычная и строгая script CSP |
| PostgreSQL API/подписки/журнал/VAPID/201/404/410/auth/нагрузка | 11 PASS, 0 FAIL | Отдельно не повторялся | 11 общих PASS + 1 новый read-only API PASS |
| Ошибки браузера/консоли в позитивном стенде | 0 / 0 | CSP violations, поведение не работает | 0 / 0 |
| Реальная отправка production 08.10.2026 | Не запускалась на старом deployment | В тестовом браузере с bypass CSP: 3 подписки, 3 свежих HTTP 201 | Код push sender сохранён; новая версия на сервер не устанавливалась |
| Диагностика | Нового сборщика нет | Новый API отсутствует | Реальный subprocess, API, скачивание, CSRF/owner/error проходят; браузерные progress/download проходят |
| Дополнительные существующие pytest | Не сравнивались | — | 18 PASS: backup, live messages, messages-page structural tests |

Матрица UI использует HTML, сгенерированный настоящими функциями каждой версии, и настоящий HTTP-сервер/Service Worker; ответы API/3x-ui на этом стенде — контролируемые HTTP-фикстуры. Это не полный браузерный проход через живой PostgreSQL backend. Реальные PostgreSQL API проверены отдельно FastAPI TestClient. Ни одна синтетическая запись не отправлялась в production.

Данные: 1000 записей, кириллица/Ё/регистр/пробелы/Unicode/emoji/апостроф/HTML-символы, активные/истёкшие/блокированные/online и разные даты/трафик. UI fixture хранится в отдельной SQLite; отдельно засевались 1000 настоящих PostgreSQL users, затем очищены. Есть отдельные seed/clean скрипты.

Поиск проверен полностью/частично/регистр/кириллица/пусто/очистка; каждый статус, все сортировки, комбинация поиска/статуса/сортировки/пагинации и изменение видимых строк. Все 5 push-кнопок, app-log, отказ Notification permission с причиной, отключение без browser subscription, реальный SW и тестовый запрос проверены. 100 быстрых смен поля на 1000 строк: 1736.1 мс. 300 реальных PostgreSQL запросов в 12 потоках: 0.156 с в итоговом отдельном прогоне. Эти числа относятся к стенду, не к производительности production.

Первый DB-тест был запущен в locale C и не прошёл Unicode lower(); создан отдельный ICU ru-RU стенд, после повторного прогона ошибка отсутствует. Полный исходный pytest collection блокируется Windows `termios`/pty в 4 тестовых модулях, а более широкий промежуточный legacy run был остановлен. Поэтому **полный старый pytest suite не объявляется успешным**; окончательные 18 и новая матрица приведены отдельно.

Новая подписка через внешний FCM/Mozilla в headless не создавалась: разрешение Notification было denied. Формат/криптография/запись новой подписки проверены через настоящий PostgreSQL API с синтетической P-256 парой. Истёкшие 404/410 проверены контролируемыми provider responses. Реальная отправка существующим трём production-подпискам вернула HTTP 201 в 17:58:32 местного времени; получение/показ уведомления на пользовательском устройстве не подтверждены.

Все ожидаемые 401/403/400/404 в negative auth/error tests отделены от успешных UI-запросов. На production до исправления обнаружены ещё два лишних 401 на странице входа; это исправлено в panel.js.

## Диагностика и аудит: что остаётся проверить

`PANEL_CONTROL_AUDIT_5.0.5.csv` — честная инвентаризация 61 различимого шаблонного элемента в 14 сгенерированных страницах. 10 целевых элементов имеют свидетельства выполнения; прочие явно `NOT_EXECUTED`. Это **не полный функциональный аудит каждой кнопки**: создание/удаление пользователей, платежи, восстановление, публикация, перезапуски и прочие операции не выполнены на production и не объявляются проверенными. Реальные данные остальных страниц не проверялись полностью.

Нет SSH/root-доступа: не проверены фактический Linux systemd, nginx -t/reload, установщик/rollback на Linux, права и журналы сервера, процессы 3x-ui. На Windows fcntl в тестовом процессе заменён тестовой заглушкой: Linux file-lock concurrency не проверена, заглушка не входит в архив. Автономная диагностика запущена на Windows и публичных GET: обнаружила конфликт CSP; systemd/nginx/VAPID локальной копии без config получили честные SKIP/FAIL. `/health` на рабочем URL не удалось подтвердить как успешный; HTTP-коды будут в серверном отчёте.

Для завершения production-проверки нужны: диагностический лог до/после, вывод `nginx -T` с маскированием, имя/версия внешнего прокси, путь установки из WorkingDirectory, результат применения location и `nginx -t`, проверка уведомления на реальном устройстве. Из панели не было возможности изменить nginx.

Команды загрузки, диагностики, обновления, настройки nginx, скачивания лога и регрессии — в `COMMANDS_5.0.5.md`. Релиз подготовлен локально, не опубликован в GitHub и не установлен на рабочем сервере.
