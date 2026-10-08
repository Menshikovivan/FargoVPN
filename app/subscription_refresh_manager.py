"""Background job manager for sending live 3x-ui subscription URLs to users."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from detached_jobs import DetachedJobError, launch_detached

APP_DIR = Path(__file__).resolve().parent
_LOCK = threading.RLock()
_BUSY_STATES = {"queued", "running"}


def root_dir() -> Path:
    return Path(getattr(config, "SUBSCRIPTION_REFRESH_DIR", "/var/lib/vpn-service/subscription-refresh"))


def _safe_job_id(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", str(value))[:120]


def job_dir(job_id: str) -> Path:
    return root_dir() / "jobs" / _safe_job_id(job_id)


def status_path(job_id: str) -> Path:
    return job_dir(job_id) / "status.json"


def latest_path() -> Path:
    return root_dir() / "status.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_status(job_id: str | None = None) -> dict[str, Any]:
    path = status_path(job_id) if job_id else latest_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def busy(status: dict[str, Any] | None = None) -> bool:
    current = status if isinstance(status, dict) else read_status()
    state = str(current.get("state") or "")
    if state not in _BUSY_STATES:
        return False
    value = str(current.get("updated_at") or "")
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - when.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return True
    return age <= max(300, int(getattr(config, "SUBSCRIPTION_REFRESH_STALE_SECONDS", 7200)))


def write_status(job_id: str, state: str, **details: Any) -> dict[str, Any]:
    root = root_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "status.lock"
    with _LOCK, lock_path.open("a+") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        previous = read_status(job_id)
        try:
            revision = int(previous.get("revision") or 0) + 1
        except (TypeError, ValueError):
            revision = 1
        data = {
            **previous,
            "job_id": str(job_id),
            "state": str(state),
            "revision": revision,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **details,
        }
        try:
            data["progress"] = max(0, min(100, int(data.get("progress") or 0)))
        except (TypeError, ValueError):
            data["progress"] = 0
        _atomic_json(status_path(job_id), data)
        _atomic_json(latest_path(), data)
        return data


def start(*, actor: str) -> dict[str, Any]:
    """Create and enqueue a refresh job without blocking the web request on systemd startup."""
    root = root_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "start.lock"
    with _LOCK, lock_path.open("a+") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if busy():
            raise RuntimeError("Обновление ссылок уже выполняется")
        job_id = f"subrefresh-{int(time.time())}-{secrets.token_hex(4)}"
        current = job_dir(job_id)
        current.mkdir(parents=True, exist_ok=False)
        manifest = {
            "job_id": job_id,
            "actor": str(actor)[:100],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _atomic_json(current / "manifest.json", manifest)
        # Mark the job queued before leaving the lock so a second click is rejected immediately.
        write_status(
            job_id,
            "queued",
            progress=0,
            total=0,
            processed=0,
            delivered=0,
            failed=0,
            skipped=0,
            actor=str(actor)[:100],
            message="Подготовка рассылки актуальных ссылок…",
            error="",
        )

    python = APP_DIR / ".venv" / "bin" / "python"
    if not python.is_file():
        python = Path(sys.executable)
    worker = APP_DIR / "subscription_refresh_worker.py"
    command = [str(python), str(worker), "--job-id", job_id]
    unit = f"vpn-service-subrefresh-{int(time.time())}-{secrets.token_hex(2)}"
    try:
        launcher = launch_detached(
            unit,
            command,
            description=f"Актуализация ссылок подписок FargoVPN ({job_id})",
            working_directory=APP_DIR,
        )
    except DetachedJobError as exc:
        write_status(job_id, "failed", progress=0, message="Не удалось запустить фоновую задачу", error=str(exc))
        shutil.rmtree(current, ignore_errors=True)
        raise RuntimeError(str(exc)) from exc
    return write_status(job_id, "queued", launcher=launcher, unit=unit, message="Фоновая рассылка запущена")


def cleanup(keep: int = 12) -> None:
    root = root_dir() / "jobs"
    if not root.is_dir():
        return
    entries = sorted((x for x in root.iterdir() if x.is_dir()), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in entries[max(2, keep):]:
        shutil.rmtree(old, ignore_errors=True)
