#!/usr/bin/env python3
"""Persistent background mass-mailing jobs for the web control panel."""
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
from services.media import detect_image_media_type, detect_media_type, media_kind

APP_DIR = Path(__file__).resolve().parent
_START_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()
BUSY_STATES = {"queued", "running"}
ALLOWED_KINDS = {"auto", "text", "photo", "video", "document"}


class BroadcastError(RuntimeError):
    pass


def broadcast_dir() -> Path:
    return Path(getattr(config, "BROADCAST_DIR", "/var/lib/vpn-service/broadcasts"))


def _safe_job_id(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", str(value))[:120]


def job_dir(job_id: str) -> Path:
    return broadcast_dir() / "jobs" / _safe_job_id(job_id)


def manifest_path(job_id: str) -> Path:
    return job_dir(job_id) / "manifest.json"


def job_status_path(job_id: str) -> Path:
    return job_dir(job_id) / "status.json"


def latest_status_path() -> Path:
    return broadcast_dir() / "status.json"


def log_path(job_id: str) -> Path:
    return job_dir(job_id) / "broadcast.log"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_status(job_id: str | None = None) -> dict[str, Any]:
    path = job_status_path(job_id) if job_id else latest_status_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_status(job_id: str, state: str, **details: Any) -> dict[str, Any]:
    root = broadcast_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "status.json.lock"
    with _STATUS_LOCK, lock_path.open("a+") as lock_handle:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        previous = read_status(job_id)
        try:
            previous_progress = max(0, min(100, int(previous.get("progress") or 0)))
        except (TypeError, ValueError):
            previous_progress = 0
        requested_progress: int | None = None
        if "progress" in details:
            try:
                requested_progress = max(0, min(100, int(details.get("progress") or 0)))
            except (TypeError, ValueError):
                requested_progress = 0
        previous_state = str(previous.get("state") or "")
        same_job = str(previous.get("job_id") or job_id) == str(job_id)
        regressive = same_job and (
            (previous_state in {"completed", "failed"} and state in BUSY_STATES)
            or (
                previous_state in BUSY_STATES
                and state in BUSY_STATES
                and requested_progress is not None
                and requested_progress < previous_progress
            )
        )
        if regressive:
            state = previous_state
            for key in ("message", "error", "finished_at", "last_error"):
                details.pop(key, None)
            details["progress"] = previous_progress
        elif requested_progress is not None:
            incoming = requested_progress
            if same_job and str(state) in BUSY_STATES:
                incoming = max(incoming, previous_progress)
            details["progress"] = incoming
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
        _atomic_json(job_status_path(job_id), data)
        _atomic_json(latest_status_path(), data)
        return data


def broadcast_busy(status: dict[str, Any] | None = None) -> bool:
    current = status if isinstance(status, dict) else read_status()
    if str(current.get("state") or "") not in BUSY_STATES:
        return False
    value = str(current.get("updated_at") or "")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return True
    return age <= max(300, int(getattr(config, "BROADCAST_STALE_SECONDS", 7200)))


def read_manifest(job_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(manifest_path(job_id).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _recipients() -> list[dict[str, Any]]:
    connection = sqlite3.connect(str(config.DB_PATH), timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT tg_id, COALESCE(username,'') AS username
            FROM users
            WHERE tg_id>0
            GROUP BY tg_id
            ORDER BY tg_id
            """
        ).fetchall()
    finally:
        connection.close()
    return [{"tg_id": int(row["tg_id"]), "username": str(row["username"] or "")} for row in rows]


def _validate_payload(
    kind: str,
    source: Path | None,
    content_type: str,
    original_name: str,
) -> str:
    """Validate an optional attachment and return the Telegram send kind."""
    requested = str(kind or "auto").strip().lower()
    if source is None:
        if requested not in {"auto", "text"}:
            raise BroadcastError("Для выбранного типа рассылки необходимо загрузить файл")
        return "text"
    if not source.is_file():
        raise BroadcastError("Файл рассылки не найден")
    max_size = max(1, int(getattr(config, "BROADCAST_MEDIA_MAX_MB", 45))) * 1024 * 1024
    size = source.stat().st_size
    if size <= 0:
        raise BroadcastError("Загруженный файл пуст")
    if size > max_size:
        raise BroadcastError(
            f"Файл рассылки превышает допустимый размер {max_size // (1024 * 1024)} МБ"
        )
    with source.open("rb") as handle:
        header = handle.read(128)
    clean_type = str(content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(original_name).suffix.lower()
    detected_type = detect_media_type(header, original_name, clean_type)

    if requested in {"auto", "text"}:
        # One composer: no file means sendMessage. With a file, text becomes the
        # caption and the attachment is detected as photo/video/document.
        return media_kind(detected_type) or "document"
    if requested == "photo" and not detect_image_media_type(header, original_name, clean_type):
        raise BroadcastError("Файл для фото-рассылки не распознан как изображение")
    if requested == "video" and not (
        (detected_type and detected_type.startswith("video/"))
        or clean_type.startswith("video/")
        or suffix in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg"}
    ):
        raise BroadcastError("Файл для видео-рассылки не распознан как видео")
    return requested


def _cleanup_old_jobs(keep: int = 12) -> None:
    root = broadcast_dir() / "jobs"
    if not root.is_dir():
        return
    entries = sorted(
        (item for item in root.iterdir() if item.is_dir()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in entries[max(2, keep):]:
        try:
            shutil.rmtree(old)
        except OSError:
            pass


def start_broadcast(
    *,
    actor: str,
    message: str,
    kind: str = "text",
    source: Path | None = None,
    original_name: str = "",
    content_type: str = "",
) -> dict[str, Any]:
    kind = str(kind or "auto").strip().lower()
    if kind not in ALLOWED_KINDS:
        raise BroadcastError("Неизвестный тип рассылки")
    message = str(message or "").strip()
    safe_name = Path(original_name or "broadcast.bin").name[:180]
    kind = _validate_payload(kind, source, content_type, safe_name)
    max_text = 4096 if kind == "text" else 1024
    if len(message) > max_text:
        raise BroadcastError(f"Текст превышает ограничение {max_text} символов")
    if kind == "text" and not message:
        raise BroadcastError("Введите текст рассылки или прикрепите файл")
    recipients = _recipients()
    if not recipients:
        raise BroadcastError("В базе нет пользователей с положительным Telegram ID")

    root = broadcast_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "start.lock"
    with _START_LOCK, lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if broadcast_busy():
            raise BroadcastError("Другая массовая рассылка уже выполняется")
        job_id = f"mail-{int(time.time())}-{secrets.token_hex(4)}"
        current_dir = job_dir(job_id)
        current_dir.mkdir(parents=True, exist_ok=False)
        payload_path = ""
        if source is not None:
            extension = "".join(Path(safe_name).suffixes[-2:])[:20] or ".bin"
            target = current_dir / f"payload{extension}"
            temp = target.with_suffix(target.suffix + ".part")
            shutil.copy2(source, temp)
            os.chmod(temp, 0o600)
            temp.replace(target)
            payload_path = str(target)
        manifest = {
            "job_id": job_id,
            "actor": str(actor)[:100],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
            "payload_path": payload_path,
            "filename": safe_name,
            "content_type": str(content_type or "application/octet-stream")[:120],
            "recipients": recipients,
        }
        _atomic_json(manifest_path(job_id), manifest)
        write_status(
            job_id,
            "queued",
            progress=0,
            total=len(recipients),
            processed=0,
            delivered=0,
            failed=0,
            kind=kind,
            message="Рассылка поставлена в очередь",
            actor=str(actor)[:100],
            error="",
        )

        python = APP_DIR / ".venv" / "bin" / "python"
        if not python.is_file():
            python = Path(sys.executable)
        worker = APP_DIR / "broadcast_worker.py"
        command = [str(python), str(worker), "--job-id", job_id, "--startup-delay", "1.0"]
        transient_unit = f"vpn-service-broadcast-{int(time.time())}-{secrets.token_hex(2)}"
        unit = ""
        launcher = ""
        launcher_error = ""
        process_id = 0
        try:
            template = Path("/etc/systemd/system/vpn-service-broadcast@.service")
            if (
                shutil.which("systemctl")
                and Path("/run/systemd/system").exists()
                and template.is_file()
            ):
                unit = f"vpn-service-broadcast@{job_id}.service"
                result = subprocess.run(
                    ["systemctl", "--no-block", "start", unit],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if result.returncode == 0:
                    launcher = "systemd-template"
                else:
                    launcher_error = (result.stdout + result.stderr).strip()
                    unit = ""

            if not launcher:
                try:
                    launcher = launch_detached(
                        transient_unit,
                        command,
                        description=f"Массовая Telegram-рассылка VPN Service ({job_id})",
                        working_directory=APP_DIR,
                    )
                    unit = transient_unit
                except DetachedJobError as error:
                    details = "; ".join(
                        item for item in (launcher_error, str(error)) if item
                    )
                    raise BroadcastError(
                        details or "Не удалось запустить отдельную systemd-службу рассылки"
                    ) from error
            status = write_status(
                job_id,
                "queued",
                progress=1,
                message="Фоновый процесс рассылки запущен",
                launcher=launcher,
                unit=unit,
                pid=process_id,
            )
        except Exception as error:
            write_status(
                job_id,
                "failed",
                progress=0,
                message="Не удалось запустить рассылку",
                error=str(error),
                finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            if isinstance(error, BroadcastError):
                raise
            raise BroadcastError(str(error)) from error
        _cleanup_old_jobs()
        return {"job_id": job_id, "status": status}
