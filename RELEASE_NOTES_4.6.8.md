# FargoVPN 4.6.8

Стабилизационный релиз поверх 4.6.7 с сохранением проверенной базы 4.5.5 и переносом накопленных изменений 4.6.x.

## Что изменено

- Telegram delivery: terminal ошибки `Forbidden` и `BadRequest` для недоступных чатов (`chat not found`, блокировка бота, деактивированный пользователь и т.п.) больше не прерывают общий workflow. Ошибка записывается в `user_events` с `user_id`.
- Restore/identity: при re-key placeholder Telegram ID переносятся связанные payments, message history, Push subscriptions, pending registrations, cabinet/access records, referral ledger и link requests; unread state объединяется безопасно.
- Billing: сохранён атомарный claim платежа `pending -> processing -> approved`; для разных платежей одного Telegram ID добавлена PostgreSQL advisory-lock сериализация полного read/update/verify цикла 3x-ui.
- Push: сервер проверяет наличие и согласованность VAPID private/public key pair; frontend отдельно диагностирует ошибку `getSubscription()` и при безопасном `AbortError`/`retrieving push subscription` пробует новую подписку.
- Updates: publisher boundary зафиксирован на `menshikovivan`; backend route guards применены не только к GitHub-настройкам, но и к журналу, release list, manual upload, force-version, rollback и legacy update routes. Остальные аккаунты получают только check/install flow.
- Nginx/L4: сохранены изменения 4.6.x для внешнего Nginx, Unix socket панели, проверок `nginx -T`, rollback конфигурации и совместимости с xHTTP/9443-сценарием.
- Release marker tests обновлены на 4.6.8; добавлены контрактные проверки новых требований и read-only restore diagnostic tool.

## Проверки

- `python -m compileall -q .` — PASS.
- Контрактные тесты релиза/Nginx/restore/security/backup/migrations: 69 PASS.
- Контрактные тесты 4.6.8: 9 PASS.
- Полный `pytest` в чистом source archive не стартует полностью без runtime `config.py` и пакета `psycopg`; это ожидаемое свойство исходного релизного архива, где `config.py` создаётся установщиком. Эти ограничения не относятся к компиляции и статическим контрактам, прошедшим успешно.

## Важное ограничение диагностики

Исходные архивы не содержат production PostgreSQL dump/SQLite DB, поэтому точное число затронутых восстановленных Telegram-пользователей и live-результаты Telegram/3x-ui/Push можно получить только на целевом сервере. Для этого в релиз добавлен `tools/diagnose_restore_identity.py`.
