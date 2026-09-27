# FargoVPN 4.6.10

## Push и Telegram-подключение

- Переработано Telegram-меню подключения: сначала выбор устройства, затем отдельные кнопки HAPP/INCY с автозапуском и импортом подписки.
- Добавлен подписанный короткоживущий HTTPS launcher для native deep-link, совместимый с ограничениями Telegram Bot API.
- Launcher получает актуальную подписку пользователя на момент клика; URL подписки не передаётся в `callback_data`.
- В браузерный Push flow добавлена диагностика внешнего push-сервиса и валидация VAPID P-256 на клиенте.
- После `AbortError: Error retrieving push subscription` выполняется проверка доступности внешнего push-сервиса; при недоступности пользователю показывается конкретная сетевая причина, при доступности делается одна повторная попытка без лишнего сброса Service Worker.

# FargoVPN 4.6.9

- Push/VAPID reliability and Safari/iOS `BadJwtToken` diagnostics hardened.
- Browser `AbortError: Error retrieving push subscription` gets one controlled Service Worker reset/retry.

## 4.6.8

- Исправлена изоляция недоступных Telegram-чатов: terminal `Forbidden/BadRequest` больше не роняют рассылки, напоминания и другие workflow; ошибки журналируются с `user_id`.
- Restore identity re-key расширен на историю сообщений, платежи, Push subscriptions, pending registrations, access/cabinet records, referral ledger и Telegram link requests.
- Добавлена сериализация продлений подписки по Telegram ID через PostgreSQL advisory lock, чтобы два разных платежа одного клиента не потеряли дни при конкурентной записи в 3x-ui.
- Добавлена проверка целостности VAPID private/public pair и улучшена диагностика браузерной ошибки `Error retrieving push subscription`.
- Раздел обновлений и backend mutation routes ограничены publisher-аккаунтом `menshikovivan`; обычным аккаунтам доступны только проверка и установка обновления.
- Добавлен read-only диагностический скрипт `tools/diagnose_restore_identity.py` и внешний concurrency smoke-test `tools/stress_test_panel.py`.

## 4.6.7

- Устранён аварийный перезапуск бота из-за интервала PostgreSQL и добавлено независимое сохранение подключения 3x-ui.

## 4.6.6

- Проверка записанного config.py и секретов, резервный API 3x-ui, вкладки настроек и отмена запуска полного бэкапа при сохранении.

## 4.6.5

- Исправлено сохранение настроек (CSRF формы), отображение статуса секретов и надёжный перезапуск служб через systemd.

## 4.6.4

- Исправлена группа socket для Nginx и добавлена проверка подключения от имени его рабочего пользователя.

## 4.6.3

- Исправлена вставка location в HTTP-контекст из-за захваченного парсером перевода строки перед server.
- На внешнем L4 router основной HTTPS host определяется по реальной схеме nginx -T.

## 4.6.2

- Приоритет Unix HTTPS host над 9443 fallback при восстановлении на внешнем L4 router.

## 4.6.1

- Совместимость с Unix HTTPS host внешнего L4 router и отдельным fallback 9443; диагностика при неудачном добавлении маршрута.

## 4.6.0

- Исправлено подключение к внешнему Nginx: маршрут добавляется только в загруженный конфиг и проверяется по точному socket.
- При неудачной проверке маршрут и исходная конфигурация возвращаются к прежнему состоянию.
- При полном восстановлении сохраняется действующий DSN PostgreSQL.
- Проверяются пути артефактов и контрольных сумм резервной копии.

## 4.5.6

- Fixed legacy backup restore without PostgreSQL dump.
- Fixed empty-PostgreSQL detection and one-time SQLite migration logic.
- Hardened external Nginx route verification.
- Added PostgreSQL common tooling predependency.

# Changelog

## 4.5.5
- Fixed explicit bot restart after upgrade.
- Improved web/bot failure diagnostics and startup wait.
- Fixed PostgreSQL summary detection.
