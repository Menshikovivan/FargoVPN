# FargoVPN 5.0.3: исправления и QA-аудит

Дата: 2026-10-08. Основа — ранее подготовленный в этой беседе архив 5.0.2; новый входной архив в последнем сообщении отсутствовал. SHA-256 основы: `7a3783b9a9b8818b59e4bd0a2b906646b9e4768c45f4d63bc46d146b6e8c2f90`. Рабочий root нового пакета: `FargoVPN-5.0.3/`.

## Что действительно подтверждено

Исполнены production-функции, локальные HTTP-маршруты FastAPI и JS/DOM с изолированными внешними API. Это не запуск на вашем production-сервере и не аппаратный мобильный браузер. Там, где первопричина именно вашего инцидента не воспроизводится без лога/DevTools, это прямо отмечено; не подменяю её предположением.

Структура: `main.py` — aiogram-бот; `webapp.py` — панель/FastAPI; `services/` — 3x-ui/подписки/оплата/OCR; `static/` и `service-worker.js` — UI/PWA; `db.py`, `init_db.py`, `migrations/` — PostgreSQL; `update_manager.py` + `update_worker.py` — GitHub/обновления; `install.sh` — установщик; `backup.py`, `restore_manager.py`, workers — бэкапы/восстановление; `tests/`, `scripts/` — проверки. Конфиг, базы, ключи, логи и боевые файлы в пакет не включены.

## Шесть багов: причина, правка и доказательство

| Баг | Причина в прочитанном коде | Исправление и проверка |
|---|---|---|
| 1. Фильтр/сортировка | В 5.0.2 обработчики `change` и debounce поиска уже работоспособны: `webapp.py:2527` (5.0.2). Ошибка прежних выпусков — сервер отдавал только предварительно отфильтрованный набор — в 5.0.2 уже устранена. На чистом DOM нельзя воспроизвести «все select не работают». При пустом результате UI удалял все строки, оставляя пустой контейнер. | `webapp.py:2519` добавляет явное «Пользователи не найдены». DOM проверяет **240** комбинаций 5 статусов × 6 сортировок × 2 порядка × 4 поисков, страницы 1/2/3, сброс пагинации. Отдельно 5 000 пользователей/20 изменений. Для точной причины на вашем сервере нужен failed JS/HTTP из DevTools, версия загруженного `panel.js` и проблемный URL. |
| 2. Кнопки Push | Обработчики в основе существуют: `webapp.py:4891` (5.0.2). Базовые hover/active уже присутствовали в CSS и сами по себе не доказывают причину «не нажимается»; check/disable/log-refresh не имели согласованного loading/защиты повторных кликов. Стартовая check могла позже перезаписать состояние, полученное новым действием. Перекрытие overlay в настоящем браузере не подтверждено. | `webapp.py:4900` / `static/panel.css` / `static/panel.js:3` добавляют loading, aria-busy, guard и toast; `webapp.py:4884` использует поколение операции. Проверены mocked enable→check→test→disable→check, ошибка разрешения/сервера, re-enable и поздний ответ проверки. Это не доказательство реальной Push-доставки. |
| 3. Журнал Push/SW | Маршрут есть и защищён авторизацией. `push_service.py:165` (5.0.2) запускал полную миграцию БД при каждом чтении. Под нагрузкой это создаёт лишний DDL/возможные ожидания блокировок. Исключение БД доходило до общего HTTP 500 без пригодной диагностики: `webapp.py:3199` (5.0.2). Причина вашего конкретного «ошибка» без серверного traceback неизвестна. | `push_service.py:165` кэширует только успешную миграцию с mutex; `webapp.py:3205` возвращает empty или HTTP 503 с классом ошибки/инструкцией; JS показывает HTTP-код и причину. Проверены пустой журнал, DB failure, 401 и **200** параллельных вызовов миграции (ровно один успешный DDL-запуск). |
| 4. `/var/log/vpn_bot.log` | `webapp.py:3175` (5.0.2) использовал deque поверх итерации всего файла: память ограничена, но I/O линейный по всему логу. Отсутствующий файл превращался в пустой успех; PermissionError не обрабатывался в API. Нельзя утверждать, что у вас были неверные права: штатная служба работает root, override неизвестен. | `log_reader.py:5` читает блоками **с конца**, максимум 2 МиБ; `webapp.py:3180` возвращает 404/403/503 с понятной причиной. Путь фиксирован/allowlist, query `path` не используется. Тесты 100/500/1000 строк, UTF-8, последняя строка без newline, 512-МиБ sparse-файл, 3-МиБ строка, отсутствие файла, PermissionError, редактирование известных секретов. |
| 5. Прогресс обновления | Backend уже сохраняет этапы и проценты: `update_worker.py` и `install.sh`. Но `webapp.py:5747` (5.0.2) увеличивал процент по `Math.exp` от прошедшего времени, а не по выполненной работе. Живой stdout не был частью status polling. | `webapp.py:5769` показывает только serverProgress; удалены timer/phaseCaps и неиспользуемые переменные. `webapp.py:5570` добавляет bounded живой вывод в `/api/updates/status`. Сохранены файл состояния, job_id и reconnect. `update_worker.py:113` сохраняет exit code; UI показывает код/причину. **100** параллельных status-запросов, сохранённый status вне памяти панели и failed exit=7 проверены. Настоящий systemd restart не запускался. |
| 6. Итог GitHub | 5.0.2 показывала строку о commit, но не toast/Telegram; `update_manager.py:1548` (5.0.2) создавала Release до main-sync, поэтому новый тег мог указывать на предыдущий main. HTTP успех upload сам по себе не подтверждал имя/размер/state/digest asset. | `update_manager.py:1598` синхронизирует main **до** Release; `update_manager.py:1569` и `_verify_release_tag` проверяют SHA тега, включая annotated tag. Ответы asset сверяются с байтами. `webapp.py:5898` пишет log/audit, возвращает фактический SHA и toast «Версия X успешно загружена на GitHub»; `webapp.py:5887` — фоновая отправка ADMIN_IDS, недоставка логируется, результат GitHub не переопределяется. Тесты mock GitHub подтверждают SHA/tag, 4 assets, target_commitish, запрет старого тега и ложного asset-size; реальный токен не предоставлен. |

На GitHub несколько независимых операций: атомарной транзакции «commit+tag+Release+assets» нет. При сбое main/tag или часть assets могут уже существовать. Ошибка не объявляется успехом. Старый тег другого SHA не перезаписывается; повторный выпуск должен иметь новую версию. Код SHA после PATCH подтверждается ответом GitHub; независимое чтение main/tag с настоящим GitHub проверяется после публикации, команды ниже.

## Дополнительные находки и второй проход

| Приоритет | Находка | Решение |
|---|---|---|
| P1 | Поздний boot Push check способен скрыть результат свежего enable/disable | generation guard; Node исполняет настоящий check и подтверждает, что поздний ответ не заменяет «Включены». |
| P1 | Несколько публикаций из разных worker-процессов могут одновременно писать published `.tmp` | `update_manager.py:1581` использует nonblocking mutex + `flock`, не ставит второй upload молча в очередь; тест параллельной публикации. |
| P1 | Некорректная asset-size могла выглядеть успешно опубликованной | Проверка фактического ответа API; negative test изменяет size и требует ошибки. |
| P2 | Нули CPU/TCP/скорости при отсутствующем поле 3x-ui | `services/xui_api.py:976` добавляет `_available`; `static/monitoring.js:58` не рисует отсутствующие значения. Настоящий 0 остаётся 0; NaN не является измерением. |
| P2 | Повторный submit generic формы до ответа | `static/panel.js:34` и disabled/aria-busy до finally; штатные formaction/CSRF сохранены. |
| P2 | Исторические секреты в читаемом app/update-tail | `log_security.redact` применяется к выдаче известных config/env секретов; неизвестные секреты и старые значения без совпадения полностью гарантированно скрыть нельзя. |
| P3 | Мёртвые приватные JS `subscribeOnce` и `ensurePushSubscription`, остатки timer-progress | Вызовов нет в локальной области; удалены определения и `phaseCaps`, `goalProgress`, `lastServerUpdate`. Рабочий `subscribeFromGesture` и восстановление регистрации оставлены. |
| P3 | Неиспользуемые импорты/локальные переменные в старых модулях | Расширенный Ruff F401/F841 нашёл 51 замечание. Массовое auto-fix не применялось: некоторые импорты/совместимость могут иметь side effects; это долг, не подтверждённая runtime ошибка. Основной Ruff E9/F821/F822/F823 проходит. |

**Второй проход — отдельная повторная проверка тем же исполнителем, не проверка независимым человеком.** Первый общий запуск: 148 passed / 4 failed (неполный новый CHANGELOG и ограниченная DOM-имитация createElement). Исправлены запись 5.0.3 и mock, production-условия ради теста не ослаблялись. Повторный запуск: 157 passed. Затем добавлены availability, нагрузка 5 000 пользователей и запрет параллельной публикации: два полных прогона 160 passed, без пропусков. Финальная проверка race выявила необходимость generation guard и удаления мёртвого JS; завершающие прогоны новой полной комплектации приведены в приложении «Финальные результаты» ниже.

Неудачный промежуточный запуск нового runner с относительным PYTHONPATH терял testdeps в дочернем cwd; runner нормализует пути, после этого проходит. Первый сбор метрики памяти стресс-теста упал на недоступном `/proc`; проверка переключена на `v8.getHeapStatistics`, production-код не менялся для этого.

## Панель: страницы, действия, сохранение

`PANEL_CHECKLIST_5.0.3.csv` содержит **777 элементов** из 14 сгенерированных страниц: это в том числе повторяющиеся действия для **110 синтетических пользователей**, не 777 уникальных функций и не 777 проверенных production-кнопок. В каждом ряду указаны страница, tag/id, подпись, target и точный уровень подтверждения. Вся разметка исполнена jsdom без JS runtime errors/дублирующихся id, кроме намеренно проверяемых error-cases. jsdom не проверяет hit-testing, реальные overlay, CSS layout, touch, разрешения браузера или аппаратное PWA.

| Страница / элементы | Локальная проверка | Требует сервера / как проверить |
|---|---|---|
| Главная: метрики, quick actions, ссылки | Render/JS, пути API, auth boundary | Сверить счётчики с PostgreSQL/3x-ui; нажать quick actions под своим логином. |
| Пользователи: поиск, все status/sort/order, назад/далее, пустое состояние | 240 комбинаций, 50 строк страницы, 110 и 5 000 записей; DOM passed | Повторить с реальным списком и мобильным браузером; убедиться, что URL не вызывает потерю данных после обновления. |
| Пользователи: индивидуальные операции, регистрация/блокировка/срок/ссылки | Существующие regression contracts; auth/route инвентарь | На тестовом клиенте проверить 3x-ui mutation и reload БД; не делать массовые боевые действия ради теста. |
| Настройки: вкладки bot/payment/xui/security/updates/notifications/data; Сохранить/formaction | DOM, правильный action/CSRF; реальная запись config.py в temp и concurrent VAPID writes | Изменить REMINDER_DAYS, сохранить и открыть после restart; сравнить config.py и DB-backed настройки. Все поля в боевой БД не прогонялись. |
| Уведомления: Проверить/Включить/Тест/Выключить/журнал; log100/500/1000 | Mock браузера и FastAPI; loading/toast/race, HTTP ошибки, пустота | Десктоп + Android; iOS только установленное PWA. Получить реальный push, отписаться и сверить статус. |
| Мониторинг: CPU/скорость/онлайн/3x-ui узлы | JS SVG и independent polling; current/legacy формы API, missing fields | Сверить график с `server/status`, вызвать outage, вернуть доступность; реальные узлы/скорость/CPU нужны серверные данные. |
| Сообщения: поиск, переписка, страничность, отправка/медиа, incremental feed | Сохранены tests message journal/feed/auth/media/prefix; render/JS | Отправить тестовому пользователю, получить reply в боте, проверить service/outgoing события, /start восстановленного пользователя. |
| Рассылки: аудитория/форма/запуск/прогресс | Существующие delivery/error/double-start contracts, JS | Выбрать одного тестового адресата; проверить delivery и skip недоступного чата. |
| Оплаты: approve/decline/receipt; банк/тариф/OCR settings | Concurrency бизнес-логики receipt, меню/подписка, render | Оплата/чек/OCR/provider и фактическое продление 3x-ui на отдельном тестовом клиенте. |
| Бэкапы: создать/отправить/восстановить/скачать | split/join, journal, restore confirmation и format regression | Отправить в тестовый Telegram чат; восстановление только на копии сервера, сверить users/full выбор. |
| Журналы: service/строки/live | JS timeout/route, backup-live contracts; новый app-tail HTTP | На сервере journalctl permissions, выбранные службы и настоящее live-событие. |
| Аудит: actor/action/date/страницы | SQL parser, route/auth/render | Реальная запись успешного и неуспешного действия, фильтрация и чтение после reload. |
| Напоминания: настройки/история | Чтение configured days/журнала, reminder regression | После изменения сроков отправка реального напоминания по таймеру без дублей. |
| Ссылки подписок: запускающая форма и status/log | Render/JS + сохранённые manager/worker contracts | Проверить действующие managed inbounds и новую ссылку на тестовом клиенте. |
| Обновления: check/install/upload/publish/rollback/older releases/живой вывод | Read/write статуса, mock GitHub/permissions, restart persistence, archive inspect/staging | Реальная root установка и systemd restart; SHA main/tag/Release/asset после publish; rollback на копии. |
| Диагностика: службы/БД/хранилище/версии | Render/route и сохранённые диагностические contracts | Реальный `scripts/verify_server.py`, service_audit и healthcheck. |
| Login/logout, cabinet Telegram JWT/копирование/ссылки клиента, static/SW | HTTP auth и crypto/JS contracts; cabinet не входит в 14 admin DOM-страниц | Войти/выйти, expire session; actual Telegram login и VPN deeplink в установленных клиентах. |

Нельзя честно отметить «каждая форма сохраняет значение после production reload», потому что production PostgreSQL и config не предоставлены. Локально выполнены конфигурационные записи и ключевые isolated business/HTTP tests; для остальных строк таблицы результат — требует сервера.

## API, бот, данные, безопасность

Инвентарь `AUDIT_INVENTORY_5.0.3.json`: **108** маршрутов, **48** статически извлечённых API URL; отсутствующих статических адресов — **0**. Это сверка синтаксических адресов, а не доказательство всех динамических URL/HTTP интеграций. В инвентаре метод, строка, auth/role признаки. Login/health/cabinet имеют отдельные условия доступа; отсутствие `require_auth` в cabinet не означает свободный доступ.

Бот: **38** мест callback_data / **10** верхнеуровневых callback handlers. Динамические dispatcher-пути нельзя исчерпывающе подтвердить одним поиском. Regression suite проверяет 4-значные коды, gate/deeplink/callback, restart сохранённого состояния, ограничения неверных попыток, banned-пользователя, приглашение и повторный доступ. Покупка/продление: проверены business функции активной подписки и меню, concurrent receipt submission (20 конкурирующих отправок → один payment), не повторяется обработка approved receipt. Поддержка: journal/outgoing/failed/service/feed/media tests. Telegram transport, платежный провайдер и весь end-to-end цикл «/start→оплата→VPN-пакеты→продление→support» на настоящих системах **не выполнялись**. Нет фиктивного заявления о реальном денежном платеже или подключении к VPN.

3x-ui: повторно прочитан официальный OpenAPI `https://raw.githubusercontent.com/MHSanaei/3x-ui/main/frontend/public/openapi.json` (SHA-256 `d92c62bcf8e9f1078b77a3518845840398476d456405095ec456d4aae1dbc01c`, info.version `3.x`). Подтверждены POST `/login`, GET `/csrf-token`, POST `/panel/api/clients/add`, POST `/panel/api/clients/update/{email}`, GET `/panel/api/server/status`, GET `/panel/api/inbounds/list`. Add требует объект client + inboundIds; update — объект полей клиента: текущий `services/xui_api.py` соответствует этим payload. Cookie+CSRF/Bearer, webBasePath и fallback чтения уже реализованы 5.0.2 и проходят тесты. Запись клиентов на всех старых 3x-ui версиях и 2FA login не гарантируются без конкретной версии/настройки; нужны ответы API вашего сервера. Источники: https://github.com/MHSanaei/3x-ui и https://docs.github.com/en/rest/releases/releases .

SQL: **241** статический SQL-фрагмент/DDL разобран PostgreSQL parser без ошибок; это не миграции/транзакции реального PostgreSQL. Queries параметризованы, новые маршруты не принимают произвольный путь. Авторизация/CSRF и backend Publisher restriction на `menshikovivan` сохранены и regression-tested на синтетическом identity. Токены не включены в архив. Отправка Telegram после publish фоновая, не удлиняет HTTP до истечения timeout по числу ADMIN_IDS; недоставка логируется отдельно.

Статистика не заменена заглушками. Test doubles находятся в `tests/qa`, production сохраняет системные/DB/3x-ui источники. `_available` отличает отсутствующую метрику от измеренного нуля. Старые compatibility PRAGMA-эмуляции в `db.py` не использованы как health-проверка новой панели; реальный PostgreSQL проверяется SELECT1 через server verifier. Счётчики/размеры/real CPU невозможно сопоставить с сервером, которого здесь нет.

## Нагрузка и границы измерений

- 512-МиБ sparse log: хвост 100/500/1000 читается в тесте суммарно менее 3 сек и с пиком Python allocations <8 МиБ. Это тест алгоритма чтения, не throughput диска вашего VPS.
- 5 000 пользователей, 20 динамических changes: отдельный запуск **3 263 мс**, **127 МиБ V8 heap**; pagination максимум 50 видимых. Это jsdom без реального CSS/layout, не бюджет RAM вашего браузера.
- 100 параллельных authenticated status HTTP requests / 12 workers — 200, одинаковый persisted progress и tail.
- 200 конкурирующих migrate calls / 20 workers — один успешный migrate; неуспешный не кэшируется.
- 20 receipt submissions / 8 threads и 20 config/VAPID writes / 4 threads — existing suite; без потерянных config значений/двойного payment в isolated backend.
- Вторая публикация отказана, а не бесконечно ждёт lock.
- Это **bounded concurrency smoke**, не доказательство отсутствия любых утечек, deadlocks PostgreSQL, выдерживания тысяч сетевых Telegram updates или длительной нагрузки. Нужен staging-сервер и согласованный объём нагрузки.

## Файлы: изменено / добавлено / удалено

| Статус | Файл | Изменение |
|---|---|---|
| Изменён | `CHANGELOG.md` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `README.md` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `SECURITY.md` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `VERSION` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `app/VERSION` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `push_service.py` | Миграции один раз на процесс под mutex; неуспешная миграция не кэшируется. |
| Изменён | `services/xui_api.py` | Признак фактического наличия полей 3x-ui; API и payload сохранены. |
| Изменён | `static/VERSION` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `static/monitoring.js` | Отсутствующие CPU/TCP/network не изображаются измеренными нулями. |
| Изменён | `static/panel.css` | hover/active/focus-visible/loading и toast; мобильная ширина. |
| Изменён | `static/panel.js` | Доступные toast и защита generic POST-форм от повторных отправок. |
| Изменён | `tests/test_backup_490.py` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `tests/test_panel_regression_492.py` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `tests/test_regressions_493.py` | DOM-имитация поддерживает реальный новый элемент пустого результата. |
| Изменён | `tests/test_release_475.py` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `tests/test_release_500.py` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `tests/test_release_500_followup.py` | Версия/документация/актуальные release-contract assertions; исторические синтетические версии в тестах сохранены. |
| Изменён | `update_manager.py` | Проверка commit/tag и upload asset; main перед Release; межпроцессная и потоковая блокировка повторной публикации. |
| Изменён | `update_worker.py` | Код выхода установщика сохраняется в постоянном статусе ошибки. |
| Изменён | `webapp.py` | Пустой список пользователей; диагностика Push/лога; loading/тосты; защита от позднего Push-check; реальный процент и живой вывод установки; сообщения о публикации/Telegram в фоне; удалены приватные неиспользуемые JS helper и timer-progress переменные. |
| Добавлен | `AUDIT_INVENTORY_5.0.3.json` | Автоматический инвентаризационный материал аудита, не runtime. |
| Добавлен | `AUDIT_REPORT_5.0.3.md` | Автоматический инвентаризационный материал аудита, не runtime. |
| Добавлен | `PANEL_CHECKLIST_5.0.3.csv` | Автоматический инвентаризационный материал аудита, не runtime. |
| Добавлен | `log_reader.py` | Новый seek-tail с лимитом строк и байтов, UTF-8 и признаком обрезки. |
| Добавлен | `scripts/run_checks.py` | Одна команда: syntax, версии, pytest, DOM; JUnit-summary; явный partial режим. |
| Добавлен | `tests/qa/users_stress.cjs` | 5 000 DOM-пользователей, 20 смен фильтра, 50 строк страницы, пустой результат. |
| Добавлен | `tests/test_release_503.py` | Новые HTTP, failure, race, concurrency, tail и GitHub-тесты. |

Удалённых файлов: 0. Исторические `TEST_REPORT_*`, `AUDIT_REPORT_5.0.2.md`, старые migration/compat modules сохранены; историческая версия в таких документах не является runtime version. `nginx_panel_guard.py` используется установщиком для чтения URL и сохранения конфигурации, поэтому не удалён; автоматический пишущий guard по-прежнему отключён. Удалён только конкретный мёртвый JS/переменные, перечисленные выше; caches/pyc очищены при упаковке.

`VERSION` — канонический источник runtime версии через `update_manager.current_version()`. `app/VERSION` и `static/VERSION` — совместимые копии; все 5.0.3. В боте и панели используются runtime функции. `CHANGELOG.md` начинается секцией 5.0.3; GitHub notes извлекают только соответствующую секцию. Cache-busting fingerprint panel.js/CSS/monitoring.js и versioned SW сохранён.

## Обновление и проверка на сервере

Сначала выполните штатный бэкап и убедитесь, что он получен в Telegram. В существующей 5.0.2 панели: **Обновления → ручная загрузка и установка** → выбрать `VPN_Service_Platform_5_0_3_FULL.tar.gz` → подтвердить установку. Публикация на GitHub — отдельное действие, не установка. Чтобы получить новый исправленный publisher, сначала установите этот архив на главной панели; затем публикуйте через неё. Я не выполнял публикацию в ваш GitHub и не отправлял Telegram от вашего имени.

Консольный вариант после размещения архива и SHA256 в `/root`:

```bash
cd /root
sha256sum -c VPN_Service_Platform_5_0_3_FULL.tar.gz.sha256
tar -xzf VPN_Service_Platform_5_0_3_FULL.tar.gz
bash FargoVPN-5.0.3/install.sh --update-existing /root/vpn_bot
cd /root/vpn_bot
cat VERSION app/VERSION static/VERSION
bash scripts/healthcheck.sh
.venv/bin/python scripts/verify_server.py --public-url 'https://YOUR_DOMAIN/YOUR_PANEL_PREFIX/'
.venv/bin/python service_audit.py --json
nginx -t
systemctl is-enabled vpn-service-nginx-guard.service
```

Ожидается 5.0.3 в трёх копиях; health/серверные проверки без ошибок; nginx guard disabled/not-found. Замените домен/prefix фактическими. Установщик не переписывает внешний nginx; существующий HTTPS route сохраняется. Если root/prefix изменён или это новая установка, добавление route во внешний HTTPS server block нужно выполнить отдельно после просмотра `scripts/print_nginx_location.py`. L4/443, Hysteria2/UDP443 и XHTTP конфигурация не менялись. Работоспособность протоколов проверяется реальным VPN-клиентом после обновления.

Чтение лога от пользователя веб-службы:

```bash
systemctl show vpn-service-web.service -p User -p Group -p FragmentPath
namei -l /var/log/vpn_bot.log
stat -c '%U %G %a %s' /var/log/vpn_bot.log
panel_user=$(systemctl show vpn-service-web.service -p User --value)
runuser -u "${panel_user:-root}" -- test -r /var/log/vpn_bot.log
tail -n 100 /var/log/vpn_bot.log
```

Не делайте chmod 777. При подтверждённой PermissionError дайте read доступ конкретному service user через группу/ACL и настройте logrotate; пользователь и политика logrotate должны быть проверены на вашем VPS.

API-диагностика через browser Console **после входа**, без передачи cookie мне:

```javascript
const p = document.querySelector('meta[name="fargovpn-sw-scope"]').content.replace(/\/$/, '');
for (const route of ['/api/panel/push/logs?limit=200', '/api/panel/app-log?limit=1000', '/api/updates/status']) {
  const r = await fetch(p + route, {cache:'no-store', credentials:'same-origin'});
  console.log(route, r.status, await r.text());
}
```

Пустой push-log должен быть `ok:true,logs:[],empty:true`. Отсутствующий app-log должен показать HTTP404 с причиной, PermissionError — HTTP403. Эти ожидаемые error-cases нельзя одновременно требовать как «ни одного 4xx»: в нормальном рабочем режиме ошибочных запросов не должно быть; диагностические тесты намеренно проверяют корректные ошибки.

После **реальной публикации** проверьте на машине с установленным gh и доступом к репозиторию:

```bash
gh api repos/Menshikovivan/FargoVPN/git/ref/heads/main --jq '.object.sha'
gh api repos/Menshikovivan/FargoVPN/git/ref/tags/FargoVPN-5.0.3 --jq '.object'
gh api repos/Menshikovivan/FargoVPN/releases/tags/FargoVPN-5.0.3 --jq '{id,tag_name,target_commitish,assets:[.assets[]|{name,size,state,digest}]}'
```

Сравните SHA main/tag с hash из toast/ссылки commit, проверьте `Release 5.0.3`, четыре assets и SHA256 скачанного FULL. Команды используют существующую аутентификацию gh и не публикуют данные. Для annotated tag object типа tag дополнительно прочитайте `git/tags/<SHA>`, как делает новый код.

## Что ещё требуется для достоверной проверки

Нужны: обезличенные traceback web-службы для Push/log bugs; DevTools Console/Network failed request и HTTP body на вашем URL, версия загруженного JS; `systemctl show` user/group и `stat/namei` лога; версия/API settings/webBasePath/2FA 3x-ui; доступ к **staging** PostgreSQL/systemd/nginx, тестовый Telegram-пользователь, Push browser, payment sandbox и токен GitHub с Contents write. Не присылайте секреты в чат: сведения об ответах/ошибках можно обезличить, интеграционные проверки выполнять у себя.

Ограничения: нет реального VPS, PostgreSQL instance, GitHub credentials, Telegram recipient, Push provider subscription, payment/OCR transport и Chromium/mobile devices. Не подтверждены CSS overlay/touch, actual SW registration/delivery, настоящий installer restart/recovery/rollback, API writes вашей версии 3x-ui, страны/локации inbounds и реальные VPN-пакеты. Требуемое отсутствие **любых** ошибок production Console/HTTP на **каждой** странице нельзя гарантировать jsdom или моками; выполните строковые server checks и поэлементный CSV на staging. Конфиги и боевые данные не заменены тестовыми.

## Финальные результаты

- Финальная комплектация: **161 passed**, 0 failures/errors/skipped, **51.064 сек**; JUnit первого завершающего прогона.
- Повторный запуск через `scripts/run_checks.py`: **161 passed**, 0 failures/errors/skipped, **51.570 сек**, exit_code=0, partial=false.
- 14 admin страниц: JS/DOM исполняются без runtime errors/duplicate id; намеренные HTTP failure cases диагностируются.
- 240 комбинаций фильтра/поиска/сортировки; 5 000 строк / 20 изменений — passed.
- Ruff E9/F821/F822/F823 и ShellCheck warning — passed.
- PostgreSQL syntax parser: 241 fragments, errors=[]; реальный PostgreSQL отдельно требуется.
- Однокомандный runner подтверждён запуском; полный suite не вызывает production Telegram/GitHub/payments.

Полный набор тестов новой сборки включает regression 5.0.1/5.0.2 и 9 новых 5.0.3 тестов. Итоги относятся к локально изолированным проверкам; реальные транспорт, CSS hit-testing и systemd integration не объявляются проверенными.

## Проверка упаковки

Штатный updater 5.0.2 принимает архив 5.0.3 через `store_manual_update`, распаковывает его через `stage_update` и формирует `bash install.sh --update-existing` с target-version 5.0.3. Проверены VERSION-копии, Bash/JS syntax в staged tree, один root, нормализованные uid/gid и отсутствие runtime secrets/cache. Это inspect/stage проверка, а не запуск root-установщика. Внешний SHA256-файл приложен отдельно.
