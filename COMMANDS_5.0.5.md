# FargoVPN 5.0.5 — проверка и установка

Пакет подготовлен и проверен локально. На рабочий сервер он не установлен; конфигурация внешнего nginx не изменена.

До обновления загрузите diagnose.py и diagnose.sh с приложенных файлов на сервер. Они работают независимо от наличия кнопки в панели. Команды выполняйте в локальном терминале, заменив SERVER на SSH-адрес:

```bash
scp diagnose.py diagnose.sh SERVER:/tmp/
```

На сервере:

```bash
APP_DIR=$(systemctl show vpn-service-web.service -p WorkingDirectory --value)
test -n "$APP_DIR" && test -f "$APP_DIR/config.py" || exit 1
sudo bash /tmp/diagnose.sh --app-dir "$APP_DIR" --url 'https://menshikovivan.site/fargovpn-admin-4etluxi6ghlicd9b'
```

Последняя строка содержит полный путь `LOG_FILE=...`. Скачать этот файл на свой компьютер:

```bash
scp SERVER:/var/log/fargovpn_diag_YYYYMMDD_HHMMSS_ffffff.log ./
```

Если у SSH-пользователя нет доступа к root-файлу, на сервере выполните `sudo cat /полный/путь/из/LOG_FILE`, либо скачайте отчёт через новую кнопку панели после обновления. `/var/log` — основной каталог; при отсутствии прав сборщик использует системный временный каталог. Не подставляйте в команды пароль панели.

Архив не опубликован в GitHub. Ссылки `wget` на несуществующий релиз здесь намеренно нет. Загрузка подготовленного архива:

```bash
scp VPN_Service_Platform_5.0.5_FULL.tar.gz SERVER:/tmp/
```

Установка на сервере:

```bash
mkdir -p /tmp/fargovpn-5.0.5
tar -xzf /tmp/VPN_Service_Platform_5.0.5_FULL.tar.gz -C /tmp/fargovpn-5.0.5
APP_DIR=$(systemctl show vpn-service-web.service -p WorkingDirectory --value)
test -n "$APP_DIR" && test -f "$APP_DIR/config.py" || exit 1
sudo bash /tmp/fargovpn-5.0.5/FargoVPN-5.0.5/install.sh --update-existing "$APP_DIR"
```

Штатный установщик создаёт предобновительный бэкап. Не запускайте установку поверх пустого пути. Linux/systemd-сценарий установки в этой сессии не прогонялся.

Для полного устранения обнаруженного конфликта CSP получите блок FargoVPN для внешнего HTTPS nginx:

```bash
sudo "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/print_nginx_location.py" > /tmp/fargovpn-location.conf
cat /tmp/fargovpn-location.conf
```

В существующем HTTPS server-блоке nginx замените только location панели на этот фрагмент. Не добавляйте второй такой же location, не изменяйте stream/L4 и другие сайты. Внутри location не должен оставаться дополнительный `script-src 'self'`/`style-src 'self'`, запрещающий inline. Если CSP добавляется внешним CDN/прокси, исправление нужно там. Несколько CSP применяются одновременно; более мягкий дополнительный заголовок не отменяет строгий. На новом nginx с `add_header_inherit merge` наследование также нужно явно отключить в этом location.

```bash
sudo nginx -t
# Выполнять только если проверка конфигурации успешна:
sudo systemctl reload nginx
curl -sS -D - -o /dev/null 'https://menshikovivan.site/fargovpn-admin-4etluxi6ghlicd9b/login'
sudo bash "$APP_DIR/diagnose.sh" --app-dir "$APP_DIR" --url 'https://menshikovivan.site/fargovpn-admin-4etluxi6ghlicd9b'
```

В браузере откройте «Пользователи», выполните поиск и очистку, смените статус и сортировку. В «Настройки → Уведомления» проверьте статус, разрешите уведомления, включите подписку и нажмите «Тест». В «Диагностика» запустите сбор и скачайте лог. `FAIL`/`SKIP` показываются явно, отчёт не обещает исправить конфигурацию.

До обновления старые push config/status/logs GET могут генерировать ключи, запускать миграции или писать журнал. Автономный сборщик их не вызывает: он читает БД в принудительном read-only режиме. После обновления новый `/api/diagnostics/read-only` проверяет формат, поиск, фильтры и счётчик push без изменений. Для консольных авторизованных проверок можно передать `--cookie-file /защищённый/файл`; файл содержит только значение HTTP Cookie из своей сессии. Cookie не выводится в отчёт. Без cookie авторизованные проверки отмечаются SKIP. Из панели cookie передаётся дочернему процессу только через окружение, URL строится из доверенного WEB_DOMAIN.

Регрессия одной командой (на отдельном стенде, в распакованном исходном коде):

```bash
QA_DATABASE_URL='postgresql+psycopg://qa:QA_PASSWORD@127.0.0.1:5432/fargovpn_qa' bash run_regression.sh --old /tmp/FargoVPN-4.9.3 --current /tmp/FargoVPN-5.0.4 --output work/run-001
```

Создайте отдельную PostgreSQL БД с Unicode/ICU-локалью. Сидер отказывается подключаться к не-loopback хосту и БД без суффикса `_qa`. Скрипт ставит зависимости в `.qa-venv`, Chromium и системные пакеты Playwright. Старые архивы распакуйте отдельно. Не запускайте тесты с боевым DATABASE_URL. Для повторного запуска укажите новый каталог `--output`; тестовая SQLite-фикстура не перезаписывается.

Отдельное заполнение и очистка только помеченных QA-строк:

```bash
QA_DATABASE_URL='postgresql+psycopg://qa:QA_PASSWORD@127.0.0.1:5432/fargovpn_qa' .qa-venv/bin/python tests/qa/seed_postgres.py --count 1000
QA_DATABASE_URL='postgresql+psycopg://qa:QA_PASSWORD@127.0.0.1:5432/fargovpn_qa' .qa-venv/bin/python tests/qa/seed_postgres.py --clean
```
