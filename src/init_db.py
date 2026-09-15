#!/usr/bin/env python3
"""Create and migrate the VPN Service Platform SQLite database in place."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import config
import referral_rewards


USER_COLUMNS: dict[str, str] = {
    "username": "TEXT",
    "uuid": "TEXT",
    "email": "TEXT",
    "expiry_time": "INTEGER DEFAULT 0",
    "enable": "INTEGER DEFAULT 1",
    "up": "INTEGER DEFAULT 0",
    "down": "INTEGER DEFAULT 0",
    "total": "INTEGER DEFAULT 0",
    "sub_id": "TEXT",
    "last_online": "TEXT",
    "last_online_ts": "INTEGER DEFAULT 0",
    "last_sync_at": "TEXT",
    "last_reminder_days": "INTEGER DEFAULT -1",
    "identity_source": "TEXT",
    "identity_updated_at": "TEXT",
    "notes": "TEXT",
    "referral_code": "TEXT",
    "referral_code_updated_at": "INTEGER DEFAULT 0",
    "referred_by_tg_id": "INTEGER",
    "referred_by_code": "TEXT",
    "registered_at": "TEXT",
}

PAYMENT_COLUMNS: dict[str, str] = {
    "last_error": "TEXT",
    "receipt_sha256": "TEXT",
    "receipt_text_sha256": "TEXT",
    "receipt_phash": "TEXT",
    "receipt_amount": "REAL",
    "receipt_date": "TEXT",
    "receipt_phone": "TEXT",
    "receipt_receiver": "TEXT",
    "ocr_status": "TEXT DEFAULT 'not_checked'",
    "ocr_text": "TEXT",
    "ocr_details": "TEXT",
    "auto_approved": "INTEGER DEFAULT 0",
}

BACKUP_RUN_COLUMNS: dict[str, str] = {
    "google_ok": "INTEGER",
    "google_detail": "TEXT",
}

USER_EVENT_COLUMNS: dict[str, str] = {
    "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    "tg_id": "INTEGER",
    "username": "TEXT",
    "direction": "TEXT NOT NULL DEFAULT 'system'",
    "event_type": "TEXT NOT NULL DEFAULT 'event'",
    "text": "TEXT",
    "actor": "TEXT",
    "success": "INTEGER DEFAULT 1",
    "metadata": "TEXT",
}

USER_MESSAGE_STATE_COLUMNS: dict[str, str] = {
    "last_read_event_id": "INTEGER NOT NULL DEFAULT 0",
    "last_incoming_event_id": "INTEGER NOT NULL DEFAULT 0",
    "unread_count": "INTEGER NOT NULL DEFAULT 0",
    "last_message_at": "TEXT",
    "last_message_text": "TEXT",
}


def ensure_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migrate() -> None:
    path = Path(config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        message_state_existed = bool(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_message_state'"
            ).fetchone()
        )
        connection.execute("PRAGMA busy_timeout=30000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                uuid TEXT,
                email TEXT,
                expiry_time INTEGER DEFAULT 0,
                enable INTEGER DEFAULT 1,
                up INTEGER DEFAULT 0,
                down INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                sub_id TEXT,
                last_online TEXT,
                last_online_ts INTEGER DEFAULT 0,
                last_sync_at TEXT,
                last_reminder_days INTEGER DEFAULT -1
            );

            CREATE TABLE IF NOT EXISTS manual_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT,
                remark TEXT
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                username TEXT,
                telegram_file_id TEXT,
                status TEXT DEFAULT 'pending',
                amount INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                processed_by TEXT,
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                cpu REAL,
                ram REAL,
                disk REAL,
                net_in INTEGER,
                net_out INTEGER
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                actor TEXT,
                action TEXT,
                details TEXT
            );

            CREATE TABLE IF NOT EXISTS backup_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                filename TEXT,
                size INTEGER,
                telegram_ok INTEGER,
                telegram_detail TEXT,
                yandex_ok INTEGER,
                yandex_detail TEXT,
                google_ok INTEGER,
                google_detail TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS message_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                actor TEXT,
                tg_id INTEGER,
                username TEXT,
                message TEXT,
                success INTEGER,
                detail TEXT
            );

            CREATE TABLE IF NOT EXISTS user_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                tg_id INTEGER NOT NULL,
                username TEXT,
                direction TEXT NOT NULL DEFAULT 'system',
                event_type TEXT NOT NULL DEFAULT 'event',
                text TEXT,
                actor TEXT,
                success INTEGER DEFAULT 1,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS user_message_state (
                tg_id INTEGER PRIMARY KEY,
                last_read_event_id INTEGER NOT NULL DEFAULT 0,
                last_incoming_event_id INTEGER NOT NULL DEFAULT 0,
                unread_count INTEGER NOT NULL DEFAULT 0,
                last_message_at TEXT,
                last_message_text TEXT
            );

            CREATE TABLE IF NOT EXISTS panel_notification_state (
                channel TEXT PRIMARY KEY,
                last_read_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_success_at TEXT,
                last_error TEXT,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS panel_push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_success_at TEXT,
                last_error TEXT,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_panel_push_username ON panel_push_subscriptions(username);
            CREATE INDEX IF NOT EXISTS idx_panel_push_enabled ON panel_push_subscriptions(enabled);

            CREATE TABLE IF NOT EXISTS login_security (
                identity TEXT PRIMARY KEY,
                failures INTEGER NOT NULL DEFAULT 0,
                first_failed_at INTEGER NOT NULL DEFAULT 0,
                last_failed_at INTEGER NOT NULL DEFAULT 0,
                blocked_until INTEGER NOT NULL DEFAULT 0,
                blocks INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cabinet_login_requests (
                token_hash TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at INTEGER NOT NULL,
                resolved_tg_id INTEGER,
                consumed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_cabinet_login_expires ON cabinet_login_requests(expires_at);

            CREATE TABLE IF NOT EXISTS identity_import_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                source_name TEXT,
                scanned INTEGER DEFAULT 0,
                matched INTEGER DEFAULT 0,
                updated INTEGER DEFAULT 0,
                conflicts INTEGER DEFAULT 0,
                panel_updated INTEGER DEFAULT 0,
                details TEXT
            );
            """
        )
        ensure_columns(connection, "users", USER_COLUMNS)
        ensure_columns(connection, "payments", PAYMENT_COLUMNS)
        ensure_columns(connection, "backup_runs", BACKUP_RUN_COLUMNS)
        ensure_columns(connection, "user_events", USER_EVENT_COLUMNS)
        ensure_columns(connection, "user_message_state", USER_MESSAGE_STATE_COLUMNS)
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_users_expiry ON users(expiry_time);
            CREATE INDEX IF NOT EXISTS idx_users_last_online ON users(last_online_ts);
            CREATE TABLE IF NOT EXISTS pending_registrations (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code);
            CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by_tg_id);

            CREATE TABLE IF NOT EXISTS telegram_link_requests (token TEXT PRIMARY KEY, local_tg_id INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT, resolved_tg_id INTEGER);
            CREATE INDEX IF NOT EXISTS idx_tg_link_local ON telegram_link_requests(local_tg_id);
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, id DESC);
            CREATE INDEX IF NOT EXISTS idx_payments_receipt_sha ON payments(receipt_sha256);
            CREATE INDEX IF NOT EXISTS idx_payments_receipt_text_sha ON payments(receipt_text_sha256);
            CREATE INDEX IF NOT EXISTS idx_backup_runs_created ON backup_runs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_user_events_tg_id_id ON user_events(tg_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_user_events_message_lookup ON user_events(tg_id, direction, event_type, id DESC);
            CREATE INDEX IF NOT EXISTS idx_user_events_created ON user_events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_user_message_state_unread ON user_message_state(unread_count, last_incoming_event_id DESC);
            CREATE INDEX IF NOT EXISTS idx_panel_notification_state_updated ON panel_notification_state(updated_at);
            CREATE INDEX IF NOT EXISTS idx_push_subscriptions_tg ON push_subscriptions(tg_id);
            CREATE INDEX IF NOT EXISTS idx_push_subscriptions_enabled ON push_subscriptions(enabled);
            CREATE INDEX IF NOT EXISTS idx_login_security_last_failed ON login_security(last_failed_at);
            CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_log_actor_action ON audit_log(actor, action, created_at DESC);
            CREATE TABLE IF NOT EXISTS referral_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_tg_id INTEGER NOT NULL,
                referred_tg_id INTEGER NOT NULL UNIQUE,
                reward_days INTEGER NOT NULL DEFAULT 10,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                granted_at TEXT,
                target_expiry_ms INTEGER,
                observed_expiry_before_ms INTEGER,
                details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer ON referral_rewards(referrer_tg_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_referral_rewards_status ON referral_rewards(status, id DESC);
            CREATE INDEX IF NOT EXISTS idx_identity_import_created ON identity_import_runs(created_at DESC);
            """
        )
        # A missing cursor means the feature has just been installed. Baseline
        # all historical receipts as already seen, while preserving an existing
        # cursor on every later migration.
        connection.execute(
            """
            INSERT OR IGNORE INTO panel_notification_state(channel,last_read_id,updated_at)
            SELECT 'payments', COALESCE(MAX(id),0), CURRENT_TIMESTAMP FROM payments
            """
        )
        if not message_state_existed:
            # Existing history predates unread notifications and must not appear
            # as a flood of new messages immediately after the upgrade.
            connection.execute(
                """
                INSERT OR IGNORE INTO user_message_state(
                    tg_id,last_read_event_id,last_incoming_event_id,unread_count,
                    last_message_at,last_message_text
                )
                SELECT
                    events.tg_id,
                    MAX(events.id),
                    MAX(events.id),
                    0,
                    MAX(events.created_at),
                    COALESCE((
                        SELECT latest.text
                        FROM user_events AS latest
                        WHERE latest.tg_id=events.tg_id
                          AND latest.direction='in'
                          AND latest.event_type IN (
                              'telegram_message','telegram_photo','telegram_video',
                              'telegram_document','telegram_audio','telegram_voice','telegram_sticker'
                          )
                        ORDER BY latest.id DESC
                        LIMIT 1
                    ), '')
                FROM user_events AS events
                WHERE events.direction='in'
                  AND events.event_type IN (
                      'telegram_message','telegram_photo','telegram_video',
                      'telegram_document','telegram_audio','telegram_voice','telegram_sticker'
                  )
                GROUP BY events.tg_id
                """
            )
        # Normalize invitation codes to exactly four numeric digits.
        # Existing alphanumeric/long codes are rotated and referral references
        # are updated through the recorded owner Telegram ID.
        import secrets
        import re
        rows = connection.execute("SELECT tg_id, referral_code FROM users WHERE tg_id > 0").fetchall()
        existing_codes = {
            str(row[1]).strip() for row in rows
            if re.fullmatch(r"[0-9]{4}", str(row[1] or "").strip())
        }
        invalid = [(int(row[0]), str(row[1] or "").strip()) for row in rows if not re.fullmatch(r"[0-9]{4}", str(row[1] or "").strip())]
        needed = len(invalid)
        available = [f"{n:04d}" for n in range(10000) if f"{n:04d}" not in existing_codes]
        if needed > len(available):
            raise RuntimeError("Не хватает уникальных 4-значных кодов приглашения (лимит 10000 пользователей)")
        secrets.SystemRandom().shuffle(available)
        for (tg_id, old_code), new_code in zip(invalid, available):
            connection.execute("UPDATE users SET referral_code=?, registered_at=COALESCE(registered_at,CURRENT_TIMESTAMP) WHERE tg_id=?", (new_code, tg_id))
            connection.execute("UPDATE users SET referred_by_code=? WHERE referred_by_tg_id=?", (new_code, tg_id))
            existing_codes.add(new_code)
        # Fill codes for newly-created rows that still have no code.
        missing = connection.execute("SELECT tg_id FROM users WHERE tg_id > 0 AND (referral_code IS NULL OR TRIM(referral_code)=\"\")").fetchall()
        for row in missing:
            while not available:
                raise RuntimeError("Не хватает уникальных 4-значных кодов приглашения")
            new_code = available.pop()
            connection.execute("UPDATE users SET referral_code=?, registered_at=COALESCE(registered_at,CURRENT_TIMESTAMP) WHERE tg_id=?", (new_code, int(row[0])))
            existing_codes.add(new_code)
        # Start the 24-hour rotation window for existing codes without a timestamp.
        connection.execute(
            "UPDATE users SET referral_code_updated_at=? "
            "WHERE tg_id>0 AND TRIM(COALESCE(referral_code,''))<>'' AND COALESCE(referral_code_updated_at,0)<=0",
            (int(time.time()),),
        )
        # Historical approved payments must never trigger a new referral reward after upgrade.
        # These rows are recorded as legacy_ignored, preserving the no-retroactive-bonus rule.
        connection.execute(
            """
            INSERT OR IGNORE INTO referral_rewards(
                referrer_tg_id,referred_tg_id,reward_days,status,granted_at,details
            )
            SELECT u.referred_by_tg_id,u.tg_id,10,'legacy_ignored',CURRENT_TIMESTAMP,
                   'существующая подтверждённая оплата до внедрения реферального бонуса'
              FROM users u
             WHERE u.tg_id>0 AND COALESCE(u.referred_by_tg_id,0)>0
               AND EXISTS (SELECT 1 FROM payments p WHERE p.tg_id=u.tg_id AND p.status='approved')
            """
        )
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    migrate()
