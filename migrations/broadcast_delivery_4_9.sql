-- FargoVPN 4.9: persist terminal Telegram delivery state.
-- Safe to apply repeatedly; no existing user data is changed except for new defaults.
BEGIN;

CREATE TABLE IF NOT EXISTS fargovpn_schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_available INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_unavailable_at TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_unavailable_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_users_telegram_available
    ON users(telegram_available, tg_id);

INSERT INTO fargovpn_schema_migrations(version)
VALUES ('4.9-telegram-delivery-state')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- Rollback (manual, after stopping FargoVPN):
-- BEGIN;
-- DROP INDEX IF EXISTS idx_users_telegram_available;
-- ALTER TABLE users DROP COLUMN IF EXISTS telegram_unavailable_reason;
-- ALTER TABLE users DROP COLUMN IF EXISTS telegram_unavailable_at;
-- ALTER TABLE users DROP COLUMN IF EXISTS telegram_available;
-- DELETE FROM fargovpn_schema_migrations WHERE version='4.9-telegram-delivery-state';
-- COMMIT;
