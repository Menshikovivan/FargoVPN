-- FargoVPN 4.7.3 — persistent Telegram message journal expansion.
-- Safe for existing PostgreSQL installations: additive columns/indexes only.
BEGIN;

CREATE TABLE IF NOT EXISTS fargovpn_schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE user_events ADD COLUMN IF NOT EXISTS message_kind TEXT NOT NULL DEFAULT 'message';
ALTER TABLE user_events ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE user_events ADD COLUMN IF NOT EXISTS delivery_error TEXT;

UPDATE user_events
   SET message_kind = CASE WHEN direction='system' THEN 'service' ELSE 'message' END
 WHERE message_kind IS NULL OR TRIM(message_kind)='';

UPDATE user_events
   SET delivery_status = CASE
        WHEN direction='in' THEN 'received'
        WHEN direction='out' AND COALESCE(success,0)<>0 THEN 'delivered'
        WHEN direction='out' THEN 'failed'
        ELSE 'unknown' END
 WHERE delivery_status IS NULL OR TRIM(delivery_status)='' OR delivery_status='unknown';

CREATE INDEX IF NOT EXISTS idx_user_events_direction_kind_id
    ON user_events(direction, message_kind, id DESC);
CREATE INDEX IF NOT EXISTS idx_user_events_tg_direction_kind_id
    ON user_events(tg_id, direction, message_kind, id DESC);

INSERT INTO fargovpn_schema_migrations(version)
VALUES ('4.7.3-message-journal')
ON CONFLICT(version) DO NOTHING;

COMMIT;
