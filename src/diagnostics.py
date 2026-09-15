#!/usr/bin/env python3
"""Local diagnostics for VPN Service Platform.

The module keeps checks small and deterministic so it can be used from the web
panel, from the console and during release validation. Live HTTP checks remain
in the web layer; this file focuses on database, identity, storage, update
configuration and traffic-accounting consistency.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import update_manager
import user_events


def _db_path(db_path: str | Path | None = None) -> Path:
    return Path(db_path or config.DB_PATH)


def _scalar(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> int:
    row = connection.execute(query, params).fetchone()
    return int(row[0] or 0) if row else 0


def database_report(db_path: str | Path | None = None) -> dict[str, Any]:
    path = _db_path(db_path)
    report: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "quick_check": "missing",
        "journal_mode": "unknown",
        "writable": False,
        "users": 0,
        "positive_tg_ids": 0,
        "placeholder_tg_ids": 0,
        "missing_usernames": 0,
        "missing_emails": 0,
        "missing_uuids": 0,
        "duplicate_emails": 0,
        "duplicate_uuids": 0,
        "events": 0,
        "unread_messages": 0,
        "unread_users": 0,
        "untracked_incoming_messages": 0,
        "unread_trigger": False,
        "unread_trigger_name": "",
        "blocked_logins": 0,
    }
    if not path.is_file():
        report["error"] = "База данных не найдена"
        return report
    try:
        # Message-state schema diagnostics
        # panel is present before inspecting it. This is idempotent.
        user_events.ensure_schema(path)
        connection = sqlite3.connect(str(path), timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        report["quick_check"] = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        report["journal_mode"] = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        trigger_row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
            (getattr(user_events, "_UNREAD_TRIGGER", ""),),
        ).fetchone()
        report["unread_trigger"] = bool(trigger_row)
        report["unread_trigger_name"] = str(trigger_row[0]) if trigger_row else ""
        connection.execute("SAVEPOINT diagnostic_write")
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS diagnostic_probe(value INTEGER)")
        connection.execute("INSERT INTO diagnostic_probe(value) VALUES(1)")
        connection.execute("ROLLBACK TO diagnostic_write")
        connection.execute("RELEASE diagnostic_write")
        report["writable"] = True
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "users" in tables:
            report["users"] = _scalar(connection, "SELECT COUNT(*) FROM users")
            report["positive_tg_ids"] = _scalar(connection, "SELECT COUNT(*) FROM users WHERE tg_id>0")
            report["placeholder_tg_ids"] = _scalar(connection, "SELECT COUNT(*) FROM users WHERE tg_id<=0")
            report["missing_usernames"] = _scalar(
                connection, "SELECT COUNT(*) FROM users WHERE username IS NULL OR trim(username)=''"
            )
            report["missing_emails"] = _scalar(
                connection, "SELECT COUNT(*) FROM users WHERE email IS NULL OR trim(email)=''"
            )
            report["missing_uuids"] = _scalar(
                connection, "SELECT COUNT(*) FROM users WHERE uuid IS NULL OR trim(uuid)=''"
            )
            report["duplicate_emails"] = _scalar(
                connection,
                "SELECT COUNT(*) FROM (SELECT lower(trim(email)) value FROM users "
                "WHERE email IS NOT NULL AND trim(email)<>'' GROUP BY value HAVING COUNT(*)>1)",
            )
            report["duplicate_uuids"] = _scalar(
                connection,
                "SELECT COUNT(*) FROM (SELECT lower(trim(uuid)) value FROM users "
                "WHERE uuid IS NOT NULL AND trim(uuid)<>'' GROUP BY value HAVING COUNT(*)>1)",
            )
        if "user_events" in tables:
            report["events"] = _scalar(connection, "SELECT COUNT(*) FROM user_events")
        if "user_message_state" in tables:
            report["unread_messages"] = _scalar(
                connection,
                "SELECT COALESCE(SUM(unread_count),0) FROM user_message_state WHERE unread_count>0",
            )
            report["unread_users"] = _scalar(
                connection,
                "SELECT COUNT(*) FROM user_message_state WHERE unread_count>0",
            )
        if "user_events" in tables and "user_message_state" in tables:
            event_types = sorted(user_events.UNREAD_EVENT_TYPES)
            placeholders = ",".join("?" for _ in event_types)
            params: list[Any] = list(event_types)
            admin_clause = ""
            report["untracked_incoming_messages"] = _scalar(
                connection,
                f"""
                SELECT COUNT(*)
                FROM user_events AS events
                LEFT JOIN user_message_state AS state ON state.tg_id=events.tg_id
                WHERE events.direction='in'
                  AND events.event_type IN ({placeholders})
                  AND events.id>COALESCE(state.last_incoming_event_id,0)
                  {admin_clause}
                """,
                tuple(params),
            )
        if "login_security" in tables:
            report["blocked_logins"] = _scalar(
                connection,
                "SELECT COUNT(*) FROM login_security WHERE blocked_until IS NOT NULL "
                "AND blocked_until > CAST(strftime('%s','now') AS INTEGER)",
            )
        connection.close()
    except Exception as error:
        report["error"] = str(error)
    report["healthy"] = (
        report.get("quick_check") == "ok"
        and bool(report.get("writable"))
        and bool(report.get("unread_trigger"))
        and not report.get("untracked_incoming_messages")
        and not report.get("duplicate_emails")
        and not report.get("duplicate_uuids")
    )
    return report


def storage_report(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path or _db_path()).parent
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    percent_free = round((usage.free / usage.total * 100), 1) if usage.total else 0.0
    return {
        "path": str(target),
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "free_percent": percent_free,
        "healthy": usage.free >= 1_073_741_824 and percent_free >= 5,
    }


def config_permissions_report() -> dict[str, Any]:
    path = Path(getattr(config, "__file__", Path(__file__).resolve().parent / "config.py")).resolve()
    if not path.exists():
        return {"path": str(path), "exists": False, "healthy": False, "mode": "missing"}
    mode = stat.S_IMODE(path.stat().st_mode)
    return {
        "path": str(path),
        "exists": True,
        "mode": oct(mode),
        "healthy": not bool(mode & 0o077),
    }


def update_topology_report(update_info: dict[str, Any] | None = None) -> dict[str, Any]:
    username = str(getattr(config, "WEB_USERNAME", "")).strip()
    publisher_name = update_manager.PUBLISHER_USERNAME
    publisher = update_manager.publisher_enabled()
    token = str(getattr(config, "GITHUB_API_TOKEN", "")).strip()
    repository = f"{getattr(config, 'GITHUB_REPOSITORY_OWNER', '')}/{getattr(config, 'GITHUB_REPOSITORY_NAME', 'FargoVPN')}"
    status = update_manager.read_status()
    errors: list[str] = []
    warnings: list[str] = []
    if publisher and username != publisher_name:
        errors.append("Логин издателя не совпадает с защищённым именем")
    if publisher and not token:
        warnings.append("GitHub token не настроен: публикация релизов из панели невозможна")
    status_state = str(status.get("state") or "idle")
    if status_state in update_manager.BUSY_STATES:
        if update_manager.update_job_busy(status):
            warnings.append(f"Сейчас выполняется задача обновления: {status_state}, {int(status.get('progress') or 0)}%")
        else:
            errors.append("Последняя задача обновления имеет устаревший активный статус и могла зависнуть")
    elif status_state == "failed":
        warnings.append("Последняя установка завершилась ошибкой: " + str(status.get("error") or status.get("message") or "подробности не указаны"))
    live = update_info if isinstance(update_info, dict) else {}
    live_error = str(live.get("error") or "").strip()
    if live_error:
        errors.append(f"Проверка GitHub Releases: {live_error}")
    return {
        "role": "publisher" if publisher else "follower",
        "username": username,
        "publisher_username": publisher_name,
        "github_repository": repository,
        "github_release_url": str(live.get("github_release_url") or ""),
        "remote_version": str(live.get("version") or ""),
        "remote_available": bool(live.get("available")),
        "token_configured": bool(token),
        "last_status": status,
        "warnings": warnings,
        "errors": errors,
        "healthy": not errors,
    }

def traffic_report(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    clients = snapshot.get("clients") if isinstance(snapshot.get("clients"), list) else []
    client_up = sum(max(0, int(item.get("up") or 0)) for item in clients if isinstance(item, dict))
    client_down = sum(max(0, int(item.get("down") or 0)) for item in clients if isinstance(item, dict))
    client_used = client_up + client_down
    summary = snapshot.get("traffic_summary") if isinstance(snapshot.get("traffic_summary"), dict) else {}
    server = snapshot.get("server_traffic_summary") if isinstance(snapshot.get("server_traffic_summary"), dict) else {}
    inbound_used = max(0, int(summary.get("used") or 0))
    server_used = max(0, int(server.get("used") or 0))
    difference = inbound_used - client_used
    ratio = round(client_used / inbound_used, 4) if inbound_used else None
    dashboard_difference = server_used - client_used if server else None
    return {
        "source_stale": bool(snapshot.get("stale")),
        "error": str(snapshot.get("error") or ""),
        "clients": len(clients),
        "duplicate_records_removed": max(0, int(snapshot.get("duplicate_records") or 0)),
        "client_up": client_up,
        "client_down": client_down,
        "client_used": client_used,
        "server_up": max(0, int(server.get("up") or 0)),
        "server_down": max(0, int(server.get("down") or 0)),
        "server_used": server_used,
        "server_counter_available": bool(server),
        "inbound_up": max(0, int(summary.get("up") or 0)),
        "inbound_down": max(0, int(summary.get("down") or 0)),
        "inbound_used": inbound_used,
        "difference": difference,
        "dashboard_difference": dashboard_difference,
        "client_to_inbound_ratio": ratio,
        # Different totals are legitimate: server status, inbound history and
        # active client rows have different scopes. The dashboard uses the same
        # server-status counter as 3x-ui when it is available; the other totals
        # remain visible for reconciliation.
        "healthy": not bool(snapshot.get("stale")) and inbound_used >= 0,
    }


def build_report(
    snapshot: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
    update_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    database = database_report(db_path)
    storage = storage_report(_db_path(db_path).parent)
    permissions = config_permissions_report()
    topology = update_topology_report(update_info=update_info)
    traffic = traffic_report(snapshot)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": update_manager.current_version(),
        "database": database,
        "storage": storage,
        "config_permissions": permissions,
        "updates": topology,
        "traffic": traffic,
        "healthy": all(
            bool(item.get("healthy"))
            for item in (database, storage, permissions, topology)
        ) and (snapshot is None or bool(traffic.get("healthy"))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="VPN Service Platform local diagnostics")
    parser.add_argument("--db", default=str(config.DB_PATH))
    parser.add_argument("--live", action="store_true", help="also query current 3x-ui traffic and users")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    snapshot: dict[str, Any] | None = None
    if args.live:
        try:
            from services.xui_api import fetch_and_sync

            snapshot = fetch_and_sync(force=True, db_path=args.db)
        except Exception as error:
            snapshot = {"stale": True, "error": str(error), "clients": []}
    update_info: dict[str, Any] | None = None
    if args.live:
        update_info = update_manager.check_available_update(force=True)
    report = build_report(snapshot=snapshot, db_path=args.db, update_info=update_info)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("healthy") else 1


if __name__ == "__main__":
    raise SystemExit(main())
