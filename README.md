# FargoVPN 4.7.6

Платформа продажи VPN-подписок через Telegram-бота и web-панель с интеграцией 3x-ui.

## Установка

Используйте `install.sh` на чистой системе или штатный механизм обновления существующей установки.
Production-секреты хранятся вне архива: `.env`/`config.py` не перезаписывайте значениями из примеров.

## Важные компоненты

- `main.py` — Telegram-бот.
- `webapp.py` — web-панель и cabinet.
- `cabinet_service.py` — персональные ссылки кабинета и app-launch tokens.
- `push_service.py` и `service-worker.js` — Web Push.
- `update_manager.py` / `update_worker.py` — обновления.
- `restore_manager.py` / `identity_migration.py` — восстановление и Telegram identity.
- `services/xui_api.py` — 3x-ui.

## Web Push

Для браузера требуется HTTPS. Firefox создаёт PushSubscription из пользовательского клика; при первом запросе разрешения после выдачи permission может потребоваться повторное нажатие кнопки. Mozilla Push Service должен быть доступен из сети браузера.

## Telegram-кабинет

Кнопка из бота использует короткоживущий подписанный HTTPS access token. Повторные открытия одной ссылки работают в пределах TTL и не зависят от кеша страницы.

## Nginx L4 / 3x-ui

Не добавляйте второй reverse-proxy поверх существующей production-схемы. Сохраняйте действующую конфигурацию Itman75/Nginx-L4-Stream-Router-Mask-for-3x-ui и соответствие PROXY protocol на участке Nginx → 3x-ui/inbound.

## Релизная сборка

В production-архиве нет старых audit/diff/test-релизов и исторических patch-файлов.

Текущая версия: `4.7.5`.

Полный changelog релиза: `CHANGELOG.md`.
