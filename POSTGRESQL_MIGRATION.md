# PostgreSQL migration / recovery

FargoVPN и 3x-ui могут работать с отдельными PostgreSQL-базами на одном сервере PostgreSQL.

## FargoVPN

`DATABASE_URL` определяет PostgreSQL backend FargoVPN. Legacy SQLite files используются только как источники для миграции и не являются runtime database.

## 3x-ui

Для 3x-ui PostgreSQL DSN хранится в `/etc/default/x-ui` (`XUI_DB_TYPE=postgres`, `XUI_DB_DSN=...`). После успешной миграции исходный SQLite можно оставить как rollback copy.

## Full backup

Архив FargoVPN содержит отдельные logical dumps:

- `databases/fargovpn.sql` — база FargoVPN;
- `databases/xui.sql` — база 3x-ui;
- `configs/x-ui-default.env` — конфигурация PostgreSQL 3x-ui;
- исходники FargoVPN и systemd units.

## Restore

Восстановление использует `psql` для каждой базы. После восстановления `/etc/default/x-ui` перезапускается `x-ui`. До удаления исходных копий нужно проверить API 3x-ui, Xray, Telegram-бота и веб-панель.

