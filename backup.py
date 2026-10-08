#!/usr/bin/env python3
"""Create and deliver a full PostgreSQL-first FargoVPN backup."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import html
import importlib
import json
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time

from sqlalchemy.engine import make_url
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import db as database_adapter
import config
import message_journal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from log_security import install_log_redaction
install_log_redaction()
logger = logging.getLogger("vpn-service-backup")

APP_DIR = Path(__file__).resolve().parent
LOCK_PATH = Path(getattr(config, "BACKUP_LOCK_PATH", "/run/vpn-service-backup.lock"))
STATE_PATH = Path(getattr(config, "BACKUP_STATE_PATH", "/var/lib/vpn-service/backup-state.json"))
LIVE_STATE_PATH = Path(getattr(config, "BACKUP_LIVE_STATE_PATH", "/var/lib/vpn-service/backup-live.json"))
FORCE_PATH = Path(getattr(config, "BACKUP_FORCE_PATH", "/run/vpn-service-backup.force"))


def _pg_cli_dsn(value: str) -> str:
    dsn = str(value or "").strip()
    if dsn.startswith("postgresql+psycopg://"):
        return "postgresql://" + dsn[len("postgresql+psycopg://"):]
    return dsn


def _postgres_dsn_for_cli() -> str:
    return _pg_cli_dsn(str(getattr(config, "DATABASE_URL", "") or ""))


def _xui_env_values() -> tuple[Path, dict[str, str]]:
    env_path = Path(str(getattr(config, "XUI_DB_ENV_FILE", "/etc/default/x-ui"))).expanduser()
    if not env_path.is_file():
        return env_path, {}
    values: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return env_path, values


def _xui_dsn_from_env_file() -> str:
    override = str(getattr(config, "XUI_POSTGRES_DSN", "") or "").strip()
    if override:
        return _pg_cli_dsn(override)
    env_path, values = _xui_env_values()
    dsn = str(values.get("XUI_DB_DSN", "") or "").strip()
    if dsn:
        return _pg_cli_dsn(dsn)
    if values.get("XUI_DB_TYPE", "").strip().lower() in {"postgres", "postgresql"}:
        raise RuntimeError(f"XUI_DB_TYPE=postgres, но XUI_DB_DSN не найден в {env_path}")
    raise RuntimeError(f"XUI_DB_DSN не найден в {env_path}")


def xui_postgres_dsn_for_cli() -> str:
    """Return the active 3x-ui PostgreSQL DSN for CLI tools such as psql/pg_dump."""
    return _xui_dsn_from_env_file()


def sqlite_snapshot(source: Path, destination: Path, timeout_seconds: int = 300) -> Path:
    """Create a consistent read-only SQLite snapshot, including active WAL state."""
    source = Path(source).expanduser().resolve()
    destination = Path(destination).resolve()
    if not source.is_file():
        raise RuntimeError(f"SQLite база не найдена: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    timeout = max(30.0, float(timeout_seconds))
    try:
        source_uri = source.as_uri() + "?mode=ro"
        source_db = sqlite3.connect(source_uri, uri=True, timeout=timeout)
        target_db = sqlite3.connect(str(destination), timeout=timeout)
        try:
            source_db.execute("PRAGMA query_only=ON")
            source_db.backup(target_db, pages=1000, sleep=0.05)
            target_db.commit()
            integrity = str(target_db.execute("PRAGMA integrity_check").fetchone()[0]).strip().lower()
            if integrity != "ok":
                raise RuntimeError(f"SQLite PRAGMA integrity_check: {integrity}")
        finally:
            target_db.close()
            source_db.close()
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"SQLite snapshot завершился ошибкой: {exc}") from exc
    if not destination.is_file() or destination.stat().st_size < 100:
        destination.unlink(missing_ok=True)
        raise RuntimeError("SQLite snapshot создал пустой или повреждённый файл")
    logger.info("SQLite snapshot создан: %s -> %s (%d байт)", source, destination, destination.stat().st_size)
    return destination


def postgres_snapshot(destination: Path, dsn: str | None = None, timeout_seconds: int = 300) -> Path:
    dsn = _pg_cli_dsn(dsn or _postgres_dsn_for_cli())
    if not dsn:
        raise RuntimeError("PostgreSQL DSN не настроен")
    if shutil.which("pg_dump") is None:
        raise RuntimeError("Команда pg_dump не установлена")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    cli_dsn = dsn
    env = os.environ.copy()
    try:
        parsed = make_url(dsn)
        if parsed.password is not None:
            env["PGPASSWORD"] = parsed.password
            cli_dsn = parsed.set(password=None).render_as_string(hide_password=False)
    except Exception:
        # Leave unusual libpq DSNs untouched; do not fail a valid pg_dump DSN just because
        # SQLAlchemy could not parse its optional password representation.
        pass
    command = ["pg_dump", "--format=plain", "--no-owner", "--no-privileges", "--dbname", cli_dsn, "--file", str(destination)]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=max(30, int(timeout_seconds)), env=env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("pg_dump превысил timeout") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "неизвестная ошибка").strip()
        raise RuntimeError(f"pg_dump завершился с ошибкой: {detail[-2000:]}") from exc
    if not destination.is_file() or destination.stat().st_size < 32:
        raise RuntimeError("pg_dump создал пустой или повреждённый dump")
    logger.info("PostgreSQL snapshot создан: %s байт", destination.stat().st_size)
    return destination


def xui_postgres_snapshot(destination: Path, timeout_seconds: int = 300) -> Path:
    override = str(getattr(config, "XUI_POSTGRES_DSN", "") or "").strip()
    env_path, values = _xui_env_values()
    dsn = _pg_cli_dsn(override) if override else _pg_cli_dsn(str(values.get("XUI_DB_DSN", "") or ""))
    if dsn:
        return postgres_snapshot(destination, dsn=dsn, timeout_seconds=timeout_seconds)
    db_type = values.get("XUI_DB_TYPE", "").strip().lower()
    if db_type in {"postgres", "postgresql"}:
        raise RuntimeError(f"3x-ui настроен на PostgreSQL, но XUI_DB_DSN не найден в {env_path}")
    sqlite_path = Path(str(getattr(config, "XUI_DB_PATH", "/etc/x-ui/x-ui.db"))).expanduser()
    if not sqlite_path.is_file():
        raise RuntimeError(f"Не найдена база 3x-ui: PostgreSQL XUI_DB_DSN отсутствует, SQLite файл отсутствует: {sqlite_path}")
    logger.info("3x-ui работает через legacy SQLite; создаётся согласованный snapshot %s", sqlite_path)
    return sqlite_snapshot(sqlite_path, destination, timeout_seconds=timeout_seconds)


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(STATE_PATH)


def reload_runtime_config() -> None:
    global LOCK_PATH, STATE_PATH, LIVE_STATE_PATH, FORCE_PATH
    importlib.invalidate_caches()
    if getattr(config, "__spec__", None) is not None:
        importlib.reload(config)
    LOCK_PATH = Path(getattr(config, "BACKUP_LOCK_PATH", "/run/vpn-service-backup.lock"))
    STATE_PATH = Path(getattr(config, "BACKUP_STATE_PATH", "/var/lib/vpn-service/backup-state.json"))
    LIVE_STATE_PATH = Path(getattr(config, "BACKUP_LIVE_STATE_PATH", "/var/lib/vpn-service/backup-live.json"))
    FORCE_PATH = Path(getattr(config, "BACKUP_FORCE_PATH", "/run/vpn-service-backup.force"))


def _write_live_state(payload: dict[str, Any]) -> None:
    """Atomically publish a tiny backup progress snapshot for the web panel."""
    try:
        LIVE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = LIVE_STATE_PATH.with_name(LIVE_STATE_PATH.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o640)
        temporary.replace(LIVE_STATE_PATH)
    except OSError as exc:
        logger.warning("Не удалось обновить live-статус бэкапа: %s", exc)


def _live_event(phase: str, message: str, *, status: str = "running", progress: int | None = None, detail: str = "") -> None:
    now = datetime.now().isoformat(timespec="seconds")
    current: dict[str, Any]
    try:
        current = json.loads(LIVE_STATE_PATH.read_text(encoding="utf-8")) if LIVE_STATE_PATH.is_file() else {}
    except (OSError, ValueError, TypeError):
        current = {}
    events = current.get("events") if isinstance(current.get("events"), list) else []
    events = [item for item in events if isinstance(item, dict)][-59:]
    event = {"at": now, "phase": str(phase), "status": str(status), "message": str(message)[:500]}
    if detail:
        event["detail"] = str(detail)[:1000]
    events.append(event)
    payload = {
        "updated_at": now,
        "status": str(status),
        "phase": str(phase),
        "progress": max(0, min(100, int(progress))) if progress is not None else current.get("progress", 0),
        "archive": current.get("archive", ""),
        "events": events,
    }
    if detail:
        payload["detail"] = str(detail)[:1000]
    _write_live_state(payload)


def read_live_state() -> dict[str, Any]:
    try:
        payload = json.loads(LIVE_STATE_PATH.read_text(encoding="utf-8")) if LIVE_STATE_PATH.is_file() else {}
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def request_backup_now() -> str:
    """Request the canonical backup service; no detached per-request service is created."""
    global FORCE_PATH
    reload_runtime_config()
    FORCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"requested_at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False)
    temporary = FORCE_PATH.with_name(FORCE_PATH.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(FORCE_PATH)
    completed = subprocess.run(
        ["systemctl", "start", "--no-block", "vpn-service-backup.service"],
        check=False, capture_output=True, text=True, timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "systemctl не запустил vpn-service-backup.service").strip())
    return "Единая служба vpn-service-backup запущена"


def _consume_force_request() -> bool:
    try:
        FORCE_PATH.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Не удалось обработать запрос ручного бэкапа %s: %s", FORCE_PATH, exc)
        return False


def scheduled_backup_due(now: float | None = None) -> bool:
    reload_runtime_config()
    now = now or time.time()
    interval_days = max(1, int(getattr(config, "BACKUP_INTERVAL_DAYS", 3)))
    last = float(_read_state().get("last_backup_ts", 0) or 0)
    return not last or now - last >= interval_days * 86400


def _copy_application_tree(source: Path, destination: Path) -> None:
    """Copy application source without following symlinks into the backup."""
    source = source.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    ignore_names = {".env"}
    for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            src = current_path / dirname
            if dirname in ignore_names or dirname in {"__pycache__", ".pytest_cache", ".mypy_cache", "update_staging"}:
                continue
            if dirname == ".venv" and not bool(getattr(config, "BACKUP_INCLUDE_VENV", False)):
                continue
            if src.is_symlink():
                raise RuntimeError(f"Символическая ссылка запрещена в исходниках бэкапа: {src}")
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            if filename in ignore_names or filename.endswith((".pyc", ".pyo")):
                continue
            src = current_path / filename
            if src.is_symlink():
                raise RuntimeError(f"Символическая ссылка запрещена в исходниках бэкапа: {src}")
        rel = current_path.relative_to(source)
        dest_dir = destination / rel
        dest_dir.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            if filename in ignore_names or filename.endswith((".pyc", ".pyo")):
                continue
            shutil.copy2(current_path / filename, dest_dir / filename)
        try:
            shutil.copystat(current_path, dest_dir, follow_symlinks=False)
        except OSError:
            pass


def _copy_systemd(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for pattern in ("vpn-service*", "fargovpn*", "x-ui*", "xray*", "3x-ui*"):
        for path in Path("/etc/systemd/system").glob(pattern):
            if path.is_symlink():
                logger.warning("Символическая ссылка systemd пропущена: %s", path)
                continue
            if path.is_file():
                try:
                    shutil.copy2(path, destination / path.name)
                except OSError as exc:
                    logger.warning("Не удалось скопировать %s: %s", path, exc)


BACKUP_SINGLE_FILE_BYTES = 45_000_000  # Telegram-safe decimal MB threshold.

def split_for_telegram(path: Path, destination: Path, part_bytes: int) -> list[Path]:
    part_bytes = max(1, int(part_bytes))
    size = path.stat().st_size
    if size <= BACKUP_SINGLE_FILE_BYTES:
        return [path]
    # Above 45 MB: two parts for normal backups; larger archives need more parts to stay
    # below Telegram's per-document limit. Joining restores the original tar.gz.
    part_bytes = min(part_bytes, (size + 1) // 2)
    destination.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    with path.open("rb") as source:
        index = 1
        while True:
            chunk = source.read(part_bytes)
            if not chunk:
                break
            part = destination / f"{path.name}.part{index:03d}"
            part.write_bytes(chunk)
            os.chmod(part, 0o600)
            parts.append(part)
            index += 1
    if not parts:
        raise RuntimeError("Не удалось разделить архив для Telegram")
    return parts


def _stream_file(path: Path, chunk_size: int = 1024 * 1024) -> Iterable[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            yield chunk


def _record_backup_journal_best_effort(
    admin: int,
    *,
    event_type: str,
    text: str,
    success: bool,
    telegram_message_id: int | None = None,
    delivery_status: str | None = None,
    delivery_error: str = "",
) -> None:
    """Persist backup Telegram events without ever changing delivery outcome."""
    try:
        message_journal.record_outgoing_sync(
            int(admin),
            event_type=event_type,
            text=text,
            actor="telegram_bot:backup",
            success=bool(success),
            message_kind="service",
            delivery_status=delivery_status or ("delivered" if success else "failed"),
            telegram_message_id=telegram_message_id,
            delivery_error=delivery_error,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось записать backup-событие в message_journal: %s", exc)


def notify_backup_status_sync(archive: Path | None, telegram: tuple[bool, str], error: str = "", pending: int = 0) -> None:
    """Send a compact backup result to administrators without exposing secrets."""
    if not bool(getattr(config, "BACKUP_TELEGRAM", True)):
        return
    token = str(getattr(config, "BOT_TOKEN", "") or "").strip()
    admins = []
    for item in getattr(config, "ADMIN_IDS", []):
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            admins.append(value)
    if not token or not admins:
        return
    archive_text = archive.name if archive and archive.is_file() else "не создан"
    size_text = f"{archive.stat().st_size} байт" if archive and archive.is_file() else "—"
    status = (
        "📦 <b>Результат бэкапа</b>\n"
        f"Архив: <code>{html.escape(archive_text)}</code>\n"
        f"Размер: {size_text}\n"
        f"Telegram: {'✅' if telegram[0] else '❌'} {html.escape(str(telegram[1]))}"
    )
    if pending:
        status += f"\nОчередь повторов: {pending}"
    if error:
        status += f"\nОшибка создания: {html.escape(str(error))}"

    async def _send() -> None:
        from aiogram import Bot
        bot = Bot(token)
        try:
            for admin in admins:
                try:
                    result = await bot.send_message(admin, status, parse_mode="HTML")
                    _record_backup_journal_best_effort(
                        int(admin),
                        event_type="backup_status",
                        text=status,
                        success=result is not None,
                        delivery_status="delivered" if result is not None else "failed",
                        telegram_message_id=int(getattr(result, "message_id", 0) or 0) or None,
                    )
                except Exception as exc:  # noqa: BLE001
                    _record_backup_journal_best_effort(
                        int(admin),
                        event_type="backup_status",
                        text=status,
                        success=False,
                        delivery_status="failed",
                        delivery_error=str(exc),
                    )
                    logger.warning("Не удалось отправить администратору статус бэкапа: %s", exc)
        finally:
            await bot.session.close()

    try:
        asyncio.run(_send())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось отправить сводку бэкапа в Telegram: %s", exc)


def send_to_telegram_sync(path: Path) -> tuple[bool, str]:
    _live_event("telegram", "Начата отправка архива в Telegram", progress=0)
    async def _send() -> tuple[bool, str]:
        if not bool(getattr(config, "BACKUP_TELEGRAM", True)):
            _live_event("telegram", "Отправка в Telegram отключена", status="success", progress=100)
            return False, "Отключено"
        admins = [int(item) for item in getattr(config, "ADMIN_IDS", []) if int(item) > 0]
        if not admins or not str(getattr(config, "BOT_TOKEN", "")).strip():
            _live_event("telegram", "Отправка в Telegram не выполнена: не настроены BOT_TOKEN/ADMIN_IDS", status="error", progress=100)
            return False, "Не настроены BOT_TOKEN/ADMIN_IDS"
        from aiogram import Bot
        from aiogram.types import FSInputFile
        part_mb = min(49, max(1, int(getattr(config, "BACKUP_TELEGRAM_PART_MB", 45))))
        bot = Bot(str(config.BOT_TOKEN))
        delivered = 0
        errors: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="vpn_backup_telegram_") as temp_name:
                docs = split_for_telegram(path, Path(temp_name), part_mb * 1_000_000)
                _live_event("telegram", f"Подготовлено частей для Telegram: {len(docs)}", progress=20, detail=f"лимит {part_mb} MB на часть")
                for admin in admins:
                    complete = True
                    for idx, doc in enumerate(docs, 1):
                        caption = f"✅ Полный бэкап {config.SERVICE_NAME}"
                        if len(docs) > 1:
                            caption += f"\nЧасть {idx}/{len(docs)}. Архив: {path.name}"
                        sent = False
                        last_error = ""
                        for attempt in range(1, 4):
                            try:
                                result = await bot.send_document(admin, FSInputFile(doc), caption=caption)
                                sent = result is not None
                                if not sent:
                                    raise RuntimeError("Telegram вернул пустой результат отправки документа")
                                # Telegram delivery is the primary operation. Journal persistence
                                # is observability only and must never trigger a duplicate send.
                                _record_backup_journal_best_effort(
                                    int(admin),
                                    event_type="backup_document",
                                    text=caption,
                                    success=True,
                                    delivery_status="delivered",
                                    telegram_message_id=int(getattr(result, "message_id", 0) or 0) or None,
                                )
                                break
                            except Exception as exc:  # noqa: BLE001
                                last_error = str(exc)
                                _live_event("telegram", f"Telegram: ошибка части {idx}/{len(docs)}, повтор {attempt}/3", progress=min(95, 20 + idx * 70 // max(1, len(docs))), detail=last_error)
                                if attempt < 3:
                                    await asyncio.sleep(2 * attempt)
                        if not sent:
                            complete = False
                            errors.append(f"{admin}, часть {idx}/{len(docs)}: {last_error}")
                            break
                    if complete:
                        delivered += 1
        finally:
            await bot.session.close()
        detail = f"Доставлено администраторам: {delivered}; частей на архив: {len(docs)}"
        if errors:
            detail += "; ошибки: " + "; ".join(errors)
        return delivered > 0, detail
    try:
        result = asyncio.run(_send())
    except Exception as exc:  # noqa: BLE001
        _live_event("telegram", "Telegram: ошибка отправки", status="error", progress=100, detail=str(exc))
        raise
    _live_event(
        "telegram",
        "Telegram: архив отправлен" if result[0] else "Telegram: архив не отправлен",
        status="success" if result[0] else "error",
        progress=100,
        detail=result[1],
    )
    return result


def _record_run(archive: Path | None, telegram: tuple[bool, str], error: str = "") -> None:
    try:
        with database_adapter.connect(config.DB_PATH, timeout=20) as connection:
            connection.execute(
                "INSERT INTO backup_runs(filename,size,telegram_ok,telegram_detail,error) VALUES(?,?,?,?,?)",
                (archive.name if archive else "", archive.stat().st_size if archive and archive.exists() else 0,
                 int(bool(telegram[0])), telegram[1], error),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось записать журнал бэкапа: %s", exc)


def _pending_deliveries() -> list[dict[str, Any]]:
    raw = _read_state().get("pending_deliveries", [])
    return [item for item in raw if isinstance(item, dict) and str(item.get("archive", "")).strip()]


def _save_delivery_state(*, last_backup_ts: float | None = None, last_archive: str | None = None, pending: list[dict[str, Any]] | None = None) -> None:
    state = _read_state()
    if last_backup_ts is not None:
        state["last_backup_ts"] = float(last_backup_ts)
    if last_archive is not None:
        state["last_archive"] = str(last_archive)
    if pending is not None:
        state["pending_deliveries"] = pending
    _write_state(state)


def _destination_enabled(name: str) -> bool:
    return name == "telegram" and bool(getattr(config, "BACKUP_TELEGRAM", True))


def _deliver_archive(archive: Path, destinations: set[str] | None = None) -> dict[str, tuple[bool, str]]:
    destinations = destinations or {"telegram"}
    _live_event("delivery", f"Начата доставка архива {archive.name}", progress=0, detail="Telegram")
    result: dict[str, tuple[bool, str]] = {}
    if "telegram" in destinations:
        if _destination_enabled("telegram"):
            try:
                result["telegram"] = send_to_telegram_sync(archive)
            except Exception as exc:  # noqa: BLE001
                result["telegram"] = (False, str(exc))
        else:
            result["telegram"] = (True, "Отключено")
    _live_event("delivery", "Доставка архива завершена", status="success" if all(v[0] for v in result.values()) else "error", progress=100)
    return result


def _update_pending_after_delivery(archive: Path, results: dict[str, tuple[bool, str]]) -> None:
    state = _read_state()
    pending = _pending_deliveries()
    failed = [name for name, value in results.items() if _destination_enabled(name) and not value[0]]
    existing = next((item for item in pending if str(item.get("archive")) == str(archive)), None)
    if failed:
        item = existing or {"archive": str(archive), "created_ts": time.time(), "attempts": 0}
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["last_attempt_ts"] = time.time()
        item["remaining"] = failed
        item["details"] = {name: results[name][1] for name in failed}
        pending = [entry for entry in pending if str(entry.get("archive")) != str(archive)]
        pending.append(item)
    else:
        pending = [entry for entry in pending if str(entry.get("archive")) != str(archive)]
    state["pending_deliveries"] = pending
    state["last_backup_ts"] = time.time()
    state["last_archive"] = str(archive)
    _write_state(state)


def retry_pending_deliveries() -> int:
    pending = _pending_deliveries()
    if not pending:
        return 0
    now = time.time()
    retry_interval = max(60, int(getattr(config, "BACKUP_RETRY_INTERVAL_SECONDS", 900)))
    keep_days = max(1, int(getattr(config, "BACKUP_PENDING_KEEP_DAYS", 30)))
    remaining: list[dict[str, Any]] = []
    retried = 0
    for item in pending:
        archive = Path(str(item.get("archive", ""))).expanduser()
        created = float(item.get("created_ts", now) or now)
        last_attempt = float(item.get("last_attempt_ts", 0) or 0)
        if not archive.is_file() or now - created > keep_days * 86400:
            logger.warning("Удалена просроченная запись очереди бэкапов: %s", archive)
            continue
        if last_attempt and now - last_attempt < retry_interval:
            remaining.append(item)
            continue
        names = {str(name) for name in item.get("remaining", []) if str(name)}
        if not names:
            continue
        _live_event("retry", f"Повторная доставка архива {archive.name}", progress=0, detail=", ".join(sorted(names)))
        results = _deliver_archive(archive, names)
        retried += 1
        still_failed = [name for name, value in results.items() if _destination_enabled(name) and not value[0]]
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["last_attempt_ts"] = now
        item["details"] = {name: results[name][1] for name in still_failed}
        if still_failed:
            item["remaining"] = still_failed
            remaining.append(item)
        _record_run(archive, results.get("telegram", (True, "Не выполнялось")), "Повторная доставка")
    _save_delivery_state(pending=remaining)
    return retried


def _copy_tree_without_db(source: Path, destination: Path, *, excludes: set[str]) -> bool:
    """Copy a configuration tree without following symbolic links.

    Configuration directories are restored as trusted local state. Following a
    symlink here could silently include files outside the intended backup scope,
    so symlinks are rejected instead of dereferenced.
    """
    if not source.is_dir():
        return False
    for current, dirnames, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        for dirname in list(dirnames):
            item = current_path / dirname
            if item.is_symlink():
                raise RuntimeError(f"Символическая ссылка запрещена в конфигурации: {item}")
        for filename in filenames:
            item = current_path / filename
            if item.is_symlink():
                raise RuntimeError(f"Символическая ссылка запрещена в конфигурации: {item}")
    if destination.exists():
        shutil.rmtree(destination)
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in excludes or name.endswith(("-wal", "-shm"))}
    shutil.copytree(source, destination, symlinks=True, ignore=ignore)
    return True


def _file_inventory(stage: Path) -> list[str]:
    result = []
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            result.append(path.relative_to(stage).as_posix())
    return result


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of a regular file using bounded memory."""
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"Файл для SHA-256 не найден: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _inventory_checksums(stage: Path, inventory: list[str]) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for rel in inventory:
        checksums[rel] = _sha256_file(stage / rel)
    return checksums


def _application_row_counts(dsn: str) -> dict[str, int]:
    import psycopg
    raw = _pg_cli_dsn(dsn)
    with psycopg.connect(raw) as connection:
        counts = {}
        for table in ("users", "payments", "user_events", "audit_log"):
            if connection.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0]:
                counts[table] = int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        return counts


def _sqlite_integrity(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0]).strip().lower()
    finally:
        connection.close()


def create_backup() -> Path:
    started = time.monotonic()
    _write_live_state({
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "running",
        "phase": "start",
        "progress": 0,
        "archive": "",
        "events": [],
    })
    _live_event("start", "Запущено создание полного бэкапа", progress=0)
    root = Path(config.BACKUP_DIR)
    root.mkdir(parents=True, exist_ok=True)
    version = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip() if (APP_DIR / "VERSION").is_file() else "unknown"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = root / f"vpn_service_full_backup_{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="vpn_service_backup_") as temp_name:
        stage = Path(temp_name) / "vpn_service_backup"
        bot_copy = stage / "bot"
        databases = stage / "databases"
        configs = stage / "configs"
        system = stage / "system"
        stage.mkdir(parents=True, exist_ok=True)

        _copy_application_tree(APP_DIR, bot_copy)
        # .env is deliberately a separate payload item so the manifest makes the
        # secret-bearing runtime environment explicit rather than hiding it in source.
        app_env = APP_DIR / ".env"
        if app_env.is_file():
            configs.mkdir(parents=True, exist_ok=True)
            target_env = configs / "application.env"
            shutil.copy2(app_env, target_env)
            os.chmod(target_env, 0o600)
        _live_event("files", "Исходники и runtime-конфигурация FargoVPN подготовлены", progress=12)

        _live_event("database", "Снимается согласованная копия базы FargoVPN", progress=20)
        app_dump = postgres_snapshot(databases / "fargovpn.sql", timeout_seconds=int(getattr(config, "BACKUP_DATABASE_TIMEOUT_SECONDS", 300)))
        app_counts = _application_row_counts(_postgres_dsn_for_cli())
        _live_event("database", "Копия FargoVPN готова; таблица users проверена", progress=40, detail=json.dumps(app_counts, ensure_ascii=False))

        env_path, env_values = _xui_env_values()
        xui_db_type = env_values.get("XUI_DB_TYPE", "").strip().lower()
        if str(getattr(config, "XUI_POSTGRES_DSN", "") or "").strip() or str(env_values.get("XUI_DB_DSN", "") or "").strip() or xui_db_type in {"postgres", "postgresql"}:
            xui_dump = databases / "xui.sql"
            _live_event("database", "Снимается согласованная копия базы 3x-ui (PostgreSQL)", progress=45)
            xui_postgres_snapshot(xui_dump, timeout_seconds=int(getattr(config, "BACKUP_DATABASE_TIMEOUT_SECONDS", 300)))
            xui_database = "PostgreSQL"
            xui_backup_name = "databases/xui.sql"
            xui_integrity = "dump-ok"
        else:
            xui_dump = databases / "xui.db"
            sqlite_path = Path(str(getattr(config, "XUI_DB_PATH", "/etc/x-ui/x-ui.db"))).expanduser()
            _live_event("database", "Снимается согласованная копия базы 3x-ui (SQLite)", progress=45)
            sqlite_snapshot(sqlite_path, xui_dump, timeout_seconds=int(getattr(config, "BACKUP_DATABASE_TIMEOUT_SECONDS", 300)))
            xui_database = "SQLite"
            xui_backup_name = "databases/xui.db"
            xui_integrity = _sqlite_integrity(xui_dump)
            if xui_integrity != "ok":
                raise RuntimeError(f"SQLite 3x-ui integrity_check: {xui_integrity}")
        _live_event("database", "Копия базы 3x-ui готова", progress=62)

        if env_path.is_file():
            configs.mkdir(parents=True, exist_ok=True)
            target_env = configs / "x-ui-default.env"
            shutil.copy2(env_path, target_env)
            os.chmod(target_env, 0o600)

        copied_xui = _copy_tree_without_db(Path("/etc/x-ui"), system / "etc-x-ui", excludes={"x-ui.db"})
        copied_xray = _copy_tree_without_db(Path("/etc/xray"), system / "etc-xray", excludes=set())
        vapid = Path(str(getattr(config, "PUSH_VAPID_PRIVATE_KEY_PATH", "/var/lib/vpn-service/vapid_private.pem"))).expanduser()
        vapid_present = vapid.is_file()
        if vapid_present:
            runtime = stage / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vapid, runtime / "vapid_private.pem")
            os.chmod(runtime / "vapid_private.pem", 0o600)

        _live_event("package", "Добавляются systemd-конфигурации и manifest", progress=70)
        _copy_systemd(stage / "systemd")
        systemd_state: dict[str, dict[str, bool]] = {}
        for unit_file in sorted((stage / "systemd").glob("*")):
            if not unit_file.is_file():
                continue
            unit = unit_file.name
            # Template units such as vpn-service-broadcast@.service are not
            # independently startable units. Querying is-active/is-enabled on
            # the template path produces noisy systemd errors; the unit file
            # itself is still backed up and restored normally.
            if unit.endswith("@.service"):
                continue
            active = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False, timeout=20).returncode == 0
            enabled = subprocess.run(["systemctl", "is-enabled", "--quiet", unit], check=False, timeout=20).returncode == 0
            systemd_state[unit] = {"active": active, "enabled": enabled}
        (configs / "systemd-state.json").parent.mkdir(parents=True, exist_ok=True)
        (configs / "systemd-state.json").write_text(json.dumps(systemd_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        inventory = _file_inventory(stage)
        # metadata files are excluded from their own checksums by construction.
        checksums = _inventory_checksums(stage, inventory)
        components = {
            "fargovpn_source": True,
            "users_db": True,
            "xui_db": True,
            "xui_config": copied_xui,
            "xray_config": copied_xray,
            "systemd": bool((stage / "systemd").is_dir()),
            "systemd_state": True,
            "vapid_key": vapid_present,
            "application_env": app_env.is_file(),
            "external_proxy": False,
        }
        manifest = {
            "format": f"FargoVPN backup {version}",
            "format_version": 2,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "service": str(config.SERVICE_NAME),
            "version": version,
            "application_database": "PostgreSQL",
            "xui_database": xui_database,
            "application_dump": "databases/fargovpn.sql",
            "xui_dump": xui_backup_name,
            "xui_database_env": "configs/x-ui-default.env" if (configs / "x-ui-default.env").is_file() else "",
            "application_env": "configs/application.env" if (configs / "application.env").is_file() else "",
            "includes_venv": bool(getattr(config, "BACKUP_INCLUDE_VENV", False)),
            "includes_external_proxy": False,
            "application_row_counts": app_counts,
            "xui_integrity": xui_integrity,
            "components": components,
            "inventory": inventory,
            "telegram_delivery": {"part_mb": min(49, max(5, int(getattr(config, "BACKUP_TELEGRAM_PART_MB", 45))))},
            "restore_notes": [
                "users-only восстанавливает users/payments/referral_rewards и не заменяет security/audit/push/worker состояние.",
                "full восстанавливает FargoVPN PostgreSQL, 3x-ui database, configs, Xray configs, VAPID key и FargoVPN/3x-ui systemd units.",
                "Внешний Nginx/L4 reverse-proxy намеренно не входит в backup/restore.",
                "Секреты из config.py/.env/x-ui environment являются частью полного восстановления и должны храниться как конфиденциальные данные.",
            ],
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / "checksums.json").write_text(json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        _live_event("package", f"Упаковывается архив {archive.name}", progress=84)
        with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
            tar.add(stage, arcname="vpn_service_backup", recursive=True)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                members = tar.getmembers()
                if not any(member.name == "vpn_service_backup/manifest.json" for member in members):
                    raise RuntimeError("Созданный архив не содержит manifest.json")
                if not any(member.name == "vpn_service_backup/checksums.json" for member in members):
                    raise RuntimeError("Созданный архив не содержит checksums.json")
                total = 0
                for member in members:
                    name = member.name.replace("\\", "/")
                    parts = name.split("/")
                    # tar.add(stage, arcname="vpn_service_backup") intentionally
                    # creates a directory member named exactly vpn_service_backup.
                    # That root entry is safe and must be accepted; every payload
                    # member still has to stay strictly below it.
                    if name != "vpn_service_backup" and not name.startswith("vpn_service_backup/"):
                        raise RuntimeError(f"Созданный архив содержит путь вне корня резервной копии: {name!r}")
                    if any(part in {"", ".", ".."} for part in parts):
                        raise RuntimeError(f"Созданный архив содержит небезопасный путь: {name!r}")
                    if member.issym() or member.islnk() or member.isdev() or not (member.isfile() or member.isdir()):
                        raise RuntimeError("Созданный архив содержит небезопасные специальные файлы")
                    if member.isfile():
                        total += int(member.size or 0)
                        # Force gzip/tar decompression through the full payload once.
                        with tar.extractfile(member) as stream:
                            if stream is not None:
                                while stream.read(1024 * 1024):
                                    pass
                if total <= 0:
                    raise RuntimeError("Созданный архив пуст")
        except (tarfile.TarError, OSError) as exc:
            raise RuntimeError(f"Проверка созданного tar.gz не удалась: {exc}") from exc
    os.chmod(archive, 0o600)
    _live_event("package", "Архив создан и проверен", status="created", progress=100, detail=f"{archive.stat().st_size} байт")
    _write_live_state({**read_live_state(), "archive": str(archive), "status": "created", "phase": "package", "progress": 100})
    logger.info("performance operation=backup_create archive=%s duration_ms=%s size_bytes=%s", archive.name, int((time.monotonic()-started)*1000), archive.stat().st_size)
    return archive

def cleanup_old_backups() -> None:
    root = Path(config.BACKUP_DIR)
    cutoff = datetime.now() - timedelta(days=max(1, int(getattr(config, "BACKUP_KEEP_DAYS", 14))))
    # Remove current-format archives by retention period.
    for path in root.glob("vpn_service_full_backup_*.tar.gz"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
        except OSError as exc:
            logger.warning("Не удалось удалить старый архив %s: %s", path, exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--service", action="store_true", help="Канонический режим systemd-сервиса")
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args(argv)
    reload_runtime_config()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Другой процесс бэкапа уже выполняется; повторный запуск пропущен")
            return 0
        force_requested = _consume_force_request() if args.service else False
        effective_force = bool(args.force or force_requested)
        pending_before = retry_pending_deliveries()
        if not effective_force and not scheduled_backup_due():
            logger.info("Новый архив по интервалу пока не нужен; очередь доставки проверена: %s", pending_before)
            return 0
        archive: Path | None = None
        telegram = (True, "Не выполнялось")
        try:
            archive = create_backup()
            _write_live_state({**read_live_state(), "archive": str(archive), "status": "delivering", "phase": "delivery", "progress": 0})
            created_ts = time.time()
            cleanup_old_backups()
            if args.local_only:
                _save_delivery_state(last_backup_ts=created_ts, last_archive=str(archive))
                print(archive)
                return 0
            results = _deliver_archive(archive)
            telegram = results.get("telegram", telegram)
            _update_pending_after_delivery(archive, results)
            _record_run(archive, telegram)
            state = _read_state()
            pending_count = len(state.get("pending_deliveries", [])) if isinstance(state.get("pending_deliveries", []), list) else 0
            _live_event("done", "Бэкап полностью завершён", status="success" if telegram[0] else "error", progress=100)
            _write_live_state({**read_live_state(), "archive": str(archive), "status": "success" if telegram[0] else "error", "phase": "done", "progress": 100})
            notify_backup_status_sync(archive, telegram, pending=pending_count)
            print(json.dumps({"archive": str(archive), "telegram": telegram[0], "pending": pending_count}, ensure_ascii=False))
            return 0
        except Exception as exc:  # noqa: BLE001
            _live_event("error", "Бэкап завершился ошибкой", status="error", progress=100, detail=str(exc))
            logger.exception("Бэкап завершился ошибкой")
            _record_run(archive, telegram, str(exc))
            notify_backup_status_sync(archive, telegram, str(exc))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
