#!/usr/bin/env python3
"""Backup-first, idempotent normalization for the 5.1.37 identity policy."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import config
import db as database_adapter
from backup import postgres_snapshot
from user_identity import latest_trusted_telegram_usernames, normalize_comment, normalize_username


def _clean_full_name(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.casefold() in {"none", "null", "undefined"}:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = " ".join(text.split())[:160]
    return text or None


def run(*, dry_run: bool = False, backup_dir: str | None = None) -> dict[str, Any]:
    # Back up the *application* PostgreSQL database before reading/changing rows.
    # The archive is intentionally local and mode 0600; it is not sent anywhere.
    root = Path(
        backup_dir
        or getattr(config, "IDENTITY_MIGRATION_BACKUP_DIR", "")
        or getattr(config, "BACKUP_DIR", "/var/lib/vpn-service/backups")
    ).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    backup_path = root / f"fargovpn-identity-pre-5.1.37-{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.sql"
    if not dry_run:
        dsn = database_adapter.database_url()
        previous_umask = os.umask(0o077)
        try:
            postgres_snapshot(
                backup_path,
                dsn=dsn,
                timeout_seconds=max(30, int(getattr(config, "BACKUP_DATABASE_TIMEOUT_SECONDS", 300))),
            )
            os.chmod(backup_path, 0o600)
        finally:
            os.umask(previous_umask)

    counts: dict[str, int] = {
        "users_checked": 0,
        "users_changed": 0,
        "usernames_normalized": 0,
        "telegram_usernames_recovered": 0,
        "untrusted_panel_usernames_cleared": 0,
        "comments_normalized": 0,
        "display_names_normalized": 0,
        "events_checked": 0,
        "event_usernames_normalized": 0,
    }
    with database_adapter.connect(config.DB_PATH, timeout=30) as connection:
        # Fresh installs reach this backup-first step before init_db creates the
        # application tables. Treat that state as an intentional no-op rather
        # than failing a clean installation. Existing tables without tg_id are
        # still rejected below because that indicates schema corruption.
        users_table_exists = bool(connection.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_name='users' LIMIT 1"
        ).fetchone())
        if not users_table_exists:
            counts["backup"] = str(backup_path) if not dry_run else "not-created (dry-run)"
            counts["dry_run"] = bool(dry_run)
            counts["skipped_reason"] = "users table does not exist yet; clean install"
            return counts

        # Be safe on older installs before init_db adds optional columns/tables.
        user_columns = {
            str(row[0]) for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='users'"
            ).fetchall()
        }
        if "tg_id" not in user_columns:
            raise RuntimeError("В таблице users отсутствует обязательный столбец tg_id")
        selected_columns = [
            name for name in ("tg_id", "username", "display_name", "notes", "identity_source", "email")
            if name in user_columns
        ]
        select_sql = "SELECT " + ",".join(selected_columns) + " FROM users ORDER BY tg_id"
        rows = connection.execute(select_sql).fetchall()
        counts["users_checked"] = len(rows)
        # Historical event handles are snapshots, not current identity. A user
        # can change their Telegram username, so never backfill users.username
        # from old messages; only normalize the canonical current row. Events are
        # read separately below so malformed legacy event values can be cleaned.
        event_table_exists = bool(connection.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_name='user_events' LIMIT 1"
        ).fetchone())
        event_columns: set[str] = set()
        events = []
        if event_table_exists:
            event_columns = {
                str(row[0]) for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='user_events'"
                ).fetchall()
            }
            if {"id", "tg_id", "username", "direction", "actor"}.issubset(event_columns):
                raw_events = connection.execute(
                    "SELECT id,tg_id,username,direction,actor FROM user_events ORDER BY id ASC"
                ).fetchall()
                events = raw_events
                counts["events_checked"] = len(events)
        trusted_usernames = latest_trusted_telegram_usernames(
            [{"id": int(event[0] or 0), "tg_id": int(event[1] or 0),
              "username": event[2], "direction": event[3], "actor": event[4]}
             for event in events]
        )
        # Older migration-report keys are retained for compatibility with log parsers.
        counts["historical_usernames_recovered"] = 0
        counts["historical_usernames_ignored"] = 0
        user_updates: list[tuple[dict[str, Any], int]] = []
        for row in rows:
            values = dict(zip(selected_columns, tuple(row)))
            tg_id = int(values.get("tg_id") or 0)
            changes: dict[str, Any] = {}
            if "username" in user_columns:
                old_username = values.get("username")
                username = normalize_username(old_username, tg_id) or None
                observed_username = trusted_usernames.get(tg_id)
                source = str(values.get("identity_source") or "").strip().casefold()
                if tg_id in trusted_usernames and observed_username != (username or ""):
                    # A direct incoming Telegram update for the same immutable ID
                    # is stronger evidence than a possibly polluted local/XUI label.
                    # An empty observed value also prevents reviving a removed handle.
                    username = observed_username or None
                    counts["telegram_usernames_recovered"] += 1
                elif tg_id not in trusted_usernames and source.startswith(("3x-ui", "3xui", "xui", "panel")):
                    # A syntactically valid panel client name is not proof of a
                    # Telegram handle. If no matching inbound Telegram snapshot
                    # exists, clear it instead of linking to a potentially unrelated
                    # person. A later genuine Telegram update repopulates the field.
                    if username:
                        counts["untrusted_panel_usernames_cleared"] += 1
                    username = None
                if old_username and not normalize_username(old_username, tg_id):
                    counts["historical_usernames_ignored"] += 1
                if (old_username or None) != username:
                    changes["username"] = username
                    counts["usernames_normalized"] += 1
            if "display_name" in user_columns:
                old_display_name = values.get("display_name")
                display_name = _clean_full_name(old_display_name)
                if (old_display_name or None) != display_name:
                    changes["display_name"] = display_name
                    counts["display_names_normalized"] += 1
            if "notes" in user_columns:
                old_notes = values.get("notes")
                notes = normalize_comment(old_notes) or None
                if (old_notes or None) != notes:
                    changes["notes"] = notes
                    counts["comments_normalized"] += 1
            if changes:
                counts["users_changed"] += 1
                user_updates.append((changes, tg_id))
        if not dry_run:
            for changes, tg_id in user_updates:
                set_clause = ",".join(f"{column}=?" for column in changes)
                connection.execute(
                    f"UPDATE users SET {set_clause} WHERE tg_id=?",
                    (*changes.values(), tg_id),
                )

        # Normalize malformed event usernames in place after building the trusted
        # per-user snapshot above. Only incoming Telegram events were eligible to
        # repair users.username; bot/web events remain history, not identity evidence.
        if event_table_exists and {"id", "tg_id", "username"}.issubset(event_columns):
            event_updates: list[tuple[Any, int]] = []
            for event in events:
                event_id, event_tg_id, old_username = tuple(event)[:3]
                normalized = normalize_username(old_username, event_tg_id) or None
                if (old_username or None) != normalized:
                    counts["event_usernames_normalized"] += 1
                    event_updates.append((normalized, int(event_id)))
            if not dry_run:
                for normalized, event_id in event_updates:
                    connection.execute(
                        "UPDATE user_events SET username=? WHERE id=?",
                        (normalized, event_id),
                    )

    counts["backup"] = str(backup_path) if not dry_run else "not-created (dry-run)"
    counts["dry_run"] = bool(dry_run)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Проверить записи без изменений; бэкап не создаётся")
    parser.add_argument("--backup-dir", default="", help="Каталог для защищённой копии PostgreSQL перед миграцией")
    args = parser.parse_args(argv)
    try:
        report = run(dry_run=args.dry_run, backup_dir=args.backup_dir or None)
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
