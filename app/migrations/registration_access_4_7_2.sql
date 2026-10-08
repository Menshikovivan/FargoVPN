-- FargoVPN 4.7.2 — durable Telegram invitation gate
-- PostgreSQL migration. The application installer runs the equivalent
-- migration automatically from init_db.py; this file is provided for manual
-- inspection/recovery and for operators who prefer an explicit SQL step.
--
-- IMPORTANT: execute this once against an existing 4.7.1 database before
-- starting the 4.7.2 bot. Existing non-pending users remain active; rows that
-- still exist in pending_registrations become awaiting_invite.

BEGIN;

-- The marker table makes the legacy promotion idempotent. This matters because
-- the new column has a default of awaiting_invite: after ALTER TABLE we cannot
-- distinguish an old row from a genuinely new 4.7.2 row by the column value.
CREATE TABLE IF NOT EXISTS fargovpn_schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS registration_status TEXT DEFAULT 'awaiting_invite';
ALTER TABLE users ADD COLUMN IF NOT EXISTS registration_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS registration_attempts_reset_at BIGINT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS registration_blocked_until BIGINT NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM fargovpn_schema_migrations
        WHERE version='4.7.2-registration-access'
    ) THEN
        -- First application to an existing database: preserve already-known
        -- users as active, except rows still marked in the legacy pending table.
        UPDATE users u
           SET registration_status='active'
         WHERE COALESCE(NULLIF(TRIM(u.registration_status), ''), 'awaiting_invite')='awaiting_invite'
           AND NOT EXISTS (
               SELECT 1 FROM pending_registrations p WHERE p.tg_id=u.tg_id
           );

        INSERT INTO fargovpn_schema_migrations(version)
        VALUES ('4.7.2-registration-access');
    END IF;
END $$;

-- Any legacy pending row remains locked until its code is verified. This is
-- deliberately executed on every run, so a stale/manual migration cannot
-- accidentally unlock a pending registration.
UPDATE users u
   SET registration_status='awaiting_invite',
       registration_attempts=COALESCE(registration_attempts,0),
       registration_attempts_reset_at=COALESCE(registration_attempts_reset_at,0),
       registration_blocked_until=COALESCE(registration_blocked_until,0)
 WHERE EXISTS (SELECT 1 FROM pending_registrations p WHERE p.tg_id=u.tg_id)
   AND COALESCE(u.registration_status,'') <> 'banned';

CREATE INDEX IF NOT EXISTS idx_users_registration_status
    ON users(registration_status, registration_blocked_until, tg_id);

COMMIT;

-- Rollback (only if 4.7.2 has not been used in production):
-- BEGIN;
-- DROP INDEX IF EXISTS idx_users_registration_status;
-- ALTER TABLE users DROP COLUMN IF EXISTS registration_blocked_until;
-- ALTER TABLE users DROP COLUMN IF EXISTS registration_attempts_reset_at;
-- ALTER TABLE users DROP COLUMN IF EXISTS registration_attempts;
-- ALTER TABLE users DROP COLUMN IF EXISTS registration_status;
-- DROP TABLE IF EXISTS fargovpn_schema_migrations;
-- COMMIT;
