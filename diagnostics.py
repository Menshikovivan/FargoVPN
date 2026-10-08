#!/usr/bin/env python3
"""Актуальная диагностика FargoVPN: PostgreSQL, storage, updates и 3x-ui."""
from __future__ import annotations

import argparse
import json
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import db as database_adapter
import update_manager


def _scalar(connection: Any, query: str, params: tuple[Any, ...] = ()) -> int:
    row = connection.execute(query, params).fetchone()
    return int(row[0] or 0) if row else 0


def database_report(_db_path: str | Path | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "engine": "PostgreSQL", "configured": False, "connected": False,
        "server_version": "", "database_name": "", "current_user": "", "schema": "",
        "writable": False, "size": 0, "users": 0, "positive_tg_ids": 0,
        "placeholder_tg_ids": 0, "missing_usernames": 0, "missing_emails": 0,
        "missing_uuids": 0, "duplicate_emails": 0, "duplicate_uuids": 0,
        "events": 0, "unread_messages": 0, "unread_users": 0,
        "untracked_incoming_messages": 0, "blocked_logins": 0,
    }
    try:
        report["configured"] = bool(database_adapter.database_url())
        connection = database_adapter.connect(timeout=20)
        try:
            row = connection.execute("SELECT current_database(), current_user, current_schema(), current_setting('server_version')").fetchone()
            if row:
                report["database_name"], report["current_user"], report["schema"], report["server_version"] = (str(row[0] or ""), str(row[1] or ""), str(row[2] or ""), str(row[3] or ""))
            connection.execute("SELECT 1").fetchone()
            report["connected"] = True
            connection.execute("CREATE TEMP TABLE fargovpn_diagnostic_probe(value INTEGER)")
            connection.execute("INSERT INTO fargovpn_diagnostic_probe(value) VALUES(?)", (1,))
            connection.rollback()
            report["writable"] = True
            tables = {str(row[0]) for row in connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'").fetchall()}
            if "users" in tables:
                report["users"] = _scalar(connection, "SELECT COUNT(*) FROM users")
                report["positive_tg_ids"] = _scalar(connection, "SELECT COUNT(*) FROM users WHERE tg_id>0")
                report["placeholder_tg_ids"] = _scalar(connection, "SELECT COUNT(*) FROM users WHERE tg_id<=0")
                report["missing_usernames"] = _scalar(connection, "SELECT COUNT(*) FROM users WHERE username IS NULL OR btrim(username)=''")
                report["missing_emails"] = _scalar(connection, "SELECT COUNT(*) FROM users WHERE email IS NULL OR btrim(email)=''")
                report["missing_uuids"] = _scalar(connection, "SELECT COUNT(*) FROM users WHERE uuid IS NULL OR btrim(uuid)=''")
                report["duplicate_emails"] = _scalar(connection, "SELECT COUNT(*) FROM (SELECT lower(btrim(email)) value FROM users WHERE email IS NOT NULL AND btrim(email)<>'' GROUP BY value HAVING COUNT(*)>1) d")
                report["duplicate_uuids"] = _scalar(connection, "SELECT COUNT(*) FROM (SELECT lower(btrim(uuid)) value FROM users WHERE uuid IS NOT NULL AND btrim(uuid)<>'' GROUP BY value HAVING COUNT(*)>1) d")
            if "user_events" in tables:
                report["events"] = _scalar(connection, "SELECT COUNT(*) FROM user_events")
            if "user_message_state" in tables:
                report["unread_messages"] = _scalar(connection, "SELECT COALESCE(SUM(unread_count),0) FROM user_message_state WHERE unread_count>0")
                report["unread_users"] = _scalar(connection, "SELECT COUNT(*) FROM user_message_state WHERE unread_count>0")
                if "user_events" in tables:
                    report["untracked_incoming_messages"] = _scalar(connection, "SELECT COUNT(*) FROM user_events events LEFT JOIN user_message_state state ON state.tg_id=events.tg_id WHERE events.direction='in' AND events.id>COALESCE(state.last_incoming_event_id,0)")
            if "login_security" in tables:
                report["blocked_logins"] = _scalar(connection, "SELECT COUNT(*) FROM login_security WHERE blocked_until IS NOT NULL AND blocked_until > EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)::BIGINT")
            report["size"] = int((connection.execute("SELECT pg_database_size(current_database())").fetchone() or [0])[0] or 0)
        finally:
            connection.close()
    except Exception as error:
        report["error"] = str(error)
    report["healthy"] = bool(report["configured"]) and bool(report["connected"]) and bool(report["writable"]) and not report["duplicate_emails"] and not report["duplicate_uuids"]
    return report


def storage_report(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path or getattr(config, "BACKUP_DIR", "/var/backups/vpn-service"))
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    percent_free = (usage.free / usage.total * 100) if usage.total else 0.0
    return {"path": str(target), "total": usage.total, "used": usage.used, "free": usage.free, "free_percent": round(percent_free, 1), "free_gb": round(usage.free / (1024**3), 2), "healthy": usage.free >= 1_073_741_824 and percent_free >= 5}


def config_permissions_report() -> dict[str, Any]:
    path = Path(getattr(config, "__file__", Path(__file__).resolve().parent / "config.py")).resolve()
    if not path.exists(): return {"path": str(path), "exists": False, "healthy": False, "mode": "missing"}
    mode = stat.S_IMODE(path.stat().st_mode)
    return {"path": str(path), "exists": True, "mode": oct(mode), "healthy": not bool(mode & 0o077)}


def update_topology_report(update_info: dict[str, Any] | None = None) -> dict[str, Any]:
    username = str(getattr(config, "WEB_USERNAME", "")).strip()
    publisher = update_manager.publisher_enabled(); token = str(getattr(config, "GITHUB_API_TOKEN", "")).strip()
    repository = f"{getattr(config, 'GITHUB_REPOSITORY_OWNER', '')}/{getattr(config, 'GITHUB_REPOSITORY_NAME', 'FargoVPN')}"
    status = update_manager.read_status(); errors: list[str] = []; warnings: list[str] = []
    if publisher and not token: warnings.append("GitHub token не настроен: публикация релизов из панели невозможна")
    status_state = str(status.get("state") or "idle")
    if status_state in update_manager.BUSY_STATES:
        if update_manager.update_job_busy(status): warnings.append(f"Сейчас выполняется задача обновления: {status_state}, {int(status.get('progress') or 0)}%")
        else: errors.append("Последняя задача обновления имеет устаревший активный статус и могла зависнуть")
    elif status_state == "failed": warnings.append("Последняя установка завершилась ошибкой: " + str(status.get("error") or status.get("message") or "подробности не указаны"))
    live = update_info if isinstance(update_info, dict) else {}
    if str(live.get("error") or "").strip(): errors.append(f"Проверка GitHub Releases: {live.get('error')}")
    return {"role": "publisher" if publisher else "follower", "username": username, "publisher_username": username if publisher else "", "github_repository": repository, "github_release_url": str(live.get("github_release_url") or ""), "remote_version": str(live.get("version") or ""), "remote_available": bool(live.get("available")), "token_configured": bool(token), "last_status": status, "warnings": warnings, "errors": errors, "healthy": not errors}


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




def build_report(snapshot: dict[str, Any] | None = None, db_path: str | Path | None = None, update_info: dict[str, Any] | None = None) -> dict[str, Any]:
    database = database_report(db_path)
    storage = storage_report(getattr(config, "BACKUP_DIR", "/var/backups/vpn-service"))
    permissions = config_permissions_report(); topology = update_topology_report(update_info); traffic = traffic_report(snapshot)
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": update_manager.current_version(), "database": database, "storage": storage, "config_permissions": permissions, "updates": topology, "traffic": traffic, "healthy": all(bool(item.get("healthy")) for item in (database, storage, permissions, topology)) and (snapshot is None or bool(traffic.get("healthy")))}


def main() -> int:
    parser = argparse.ArgumentParser(description="Диагностика VPN Service Platform")
    parser.add_argument("--live", action="store_true", help="также проверить актуальные данные 3x-ui")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(); snapshot = None
    if args.live:
        try:
            from services.xui_api import fetch_and_sync
            snapshot = fetch_and_sync(force=True)
        except Exception as error:
            snapshot = {"stale": True, "error": str(error), "clients": []}
    update_info = update_manager.check_available_update(force=True) if args.live else None
    report = build_report(snapshot=snapshot, update_info=update_info)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str)); return 0 if report.get("healthy") else 1

if __name__ == "__main__": raise SystemExit(main())
