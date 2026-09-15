#!/usr/bin/env python3
"""Create a consistent full backup and deliver it to configured destinations."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import importlib
import json
import logging
import os
import shutil
import shlex
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import httpx

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vpn-service-backup")

APP_DIR = Path(__file__).resolve().parent
LOCK_PATH = Path(getattr(config, "BACKUP_LOCK_PATH", "/run/vpn-service-backup.lock"))
STATE_PATH = Path(getattr(config, "BACKUP_STATE_PATH", "/var/lib/vpn-service/backup-state.json"))
YANDEX_API = "https://cloud-api.yandex.net/v1/disk"


def sqlite_snapshot(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    source_conn = sqlite3.connect(str(source), timeout=30)
    destination_conn = sqlite3.connect(str(destination), timeout=30)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()


def _read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(STATE_PATH)


def reload_runtime_config() -> None:
    """Перечитать config.py перед запуском планировщика/доставки."""
    global LOCK_PATH, STATE_PATH
    importlib.invalidate_caches()
    if getattr(config, "__spec__", None) is not None:
        importlib.reload(config)
    LOCK_PATH = Path(getattr(config, "BACKUP_LOCK_PATH", "/run/vpn-service-backup.lock"))
    STATE_PATH = Path(getattr(config, "BACKUP_STATE_PATH", "/var/lib/vpn-service/backup-state.json"))


def scheduled_backup_due(now: float | None = None) -> bool:
    reload_runtime_config()
    now = now or time.time()
    interval_days = max(1, int(getattr(config, "BACKUP_INTERVAL_DAYS", 3)))
    last = float(_read_state().get("last_backup_ts", 0) or 0)
    return not last or now - last >= interval_days * 86400


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}
    ignored.update(name for name in names if name in {".pytest_cache", ".mypy_cache", "update_staging"})
    if not bool(getattr(config, "BACKUP_INCLUDE_VENV", True)) and ".venv" in names:
        ignored.add(".venv")
    return ignored


def _replace_copied_database(source: Path, bot_copy: Path, external_dir: Path, name: str) -> Path | None:
    if not source.exists():
        return None
    try:
        relative = source.resolve().relative_to(APP_DIR.resolve())
        destination = bot_copy / relative
        destination.unlink(missing_ok=True)
        destination.with_name(destination.name + "-wal").unlink(missing_ok=True)
        destination.with_name(destination.name + "-shm").unlink(missing_ok=True)
    except ValueError:
        destination = external_dir / name
    sqlite_snapshot(source, destination)
    return destination


def _copy_systemd(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for pattern in ("vpn-service*", "fargovpn*"):
        for path in Path("/etc/systemd/system").glob(pattern):
            if path.is_file():
                try:
                    shutil.copy2(path, destination / path.name)
                except OSError as error:
                    logger.warning("Не удалось скопировать %s: %s", path, error)


def _stream_file(path: Path, chunk_size: int = 1024 * 1024) -> Iterable[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            yield chunk


def split_for_telegram(path: Path, destination: Path, part_bytes: int) -> list[Path]:
    """Return the original file or deterministic parts below Telegram's upload limit."""
    part_bytes = max(1, int(part_bytes))
    if path.stat().st_size <= part_bytes:
        return [path]
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


def _yandex_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"OAuth {token}", "Accept": "application/json"}


def _disk_path(path: str) -> str:
    clean = path.strip().strip("/")
    return f"disk:/{clean}" if clean else "disk:/"


def _clean_remote_path(path: str) -> str:
    return "/".join(part for part in str(path).strip().strip("/").split("/") if part)


def _webdav_url(base_url: str, remote_path: str = "") -> str:
    base = str(base_url).strip().rstrip("/")
    clean = _clean_remote_path(remote_path)
    if not clean:
        return base + "/"
    return base + "/" + "/".join(quote(part, safe="") for part in clean.split("/"))


def _davfs_credentials(local_root: Path) -> tuple[str, str] | None:
    """Read credentials written by configure_yandex_webdav.sh without logging them."""
    secrets_path = Path(
        getattr(config, "YANDEX_DAVFS_SECRETS_PATH", "/etc/davfs2/secrets")
    ).expanduser()
    if not secrets_path.is_file():
        return None
    webdav_url = str(getattr(config, "YANDEX_WEBDAV_URL", "https://webdav.yandex.ru")).strip()
    targets = {
        str(local_root),
        str(local_root).rstrip("/"),
        webdav_url,
        webdav_url.rstrip("/"),
    }
    yandex_fallback: tuple[str, str] | None = None
    try:
        for raw_line in secrets_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                fields = shlex.split(line, comments=True, posix=True)
            except ValueError:
                continue
            if len(fields) < 3:
                continue
            target, username, password = fields[0], fields[1], fields[2]
            normalized = target.rstrip("/")
            if target in targets or normalized in targets:
                return username, password
            if "webdav.yandex." in normalized.lower():
                yandex_fallback = (username, password)
    except OSError as error:
        logger.warning("Не удалось прочитать davfs2 secrets: %s", error)
        return None
    return yandex_fallback


def ensure_yandex_directory(client: httpx.Client, directory: str) -> None:
    parts = [part for part in directory.strip().strip("/").split("/") if part]
    current: list[str] = []
    for part in parts:
        current.append(part)
        response = client.put(f"{YANDEX_API}/resources", params={"path": _disk_path("/".join(current))})
        if response.status_code not in (201, 409):
            response.raise_for_status()


def ensure_yandex_webdav_directory(client: httpx.Client, base_url: str, directory: str) -> None:
    current: list[str] = []
    for part in _clean_remote_path(directory).split("/"):
        if not part:
            continue
        current.append(part)
        url = _webdav_url(base_url, "/".join(current))
        info = _webdav_resource_info(client, url)
        if info["exists"]:
            if not info["is_collection"]:
                raise RuntimeError(f"WebDAV-ресурс {url} существует, но не является каталогом")
            continue
        response = client.request("MKCOL", url)
        if response.status_code in (200, 201, 204):
            continue
        if response.status_code == 405:
            # RFC 4918 permits 405 when the URL is already mapped. Verify the
            # resource instead of treating an existing Yandex.Disk folder as an
            # upload failure.
            confirmed = _webdav_resource_info(client, url)
            if confirmed["exists"] and confirmed["is_collection"]:
                continue
            raise RuntimeError(
                f"WebDAV вернул HTTP 405 для {url}, но каталог не подтверждён"
            )
        response.raise_for_status()


def _webdav_resource_info(client: httpx.Client, url: str) -> dict[str, Any]:
    request_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/>'
        '<d:resourcetype/></d:prop></d:propfind>'
    )
    response = client.request(
        "PROPFIND",
        url,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        content=request_body.encode("utf-8"),
    )
    if response.status_code == 404:
        return {"exists": False, "is_collection": False, "size": None}
    if response.status_code not in (200, 207):
        response.raise_for_status()
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return {"exists": True, "is_collection": False, "size": None}
    is_collection = any(
        element.tag.rsplit("}", 1)[-1].lower() == "collection"
        for element in root.iter()
    )
    size: int | None = None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() == "getcontentlength":
            value = (element.text or "").strip()
            if value.isdigit():
                size = int(value)
                break
    return {"exists": True, "is_collection": is_collection, "size": size}


def _webdav_resource_size(client: httpx.Client, url: str) -> int | None:
    info = _webdav_resource_info(client, url)
    return int(info["size"]) if info["exists"] and info["size"] is not None else None


def _webdav_timeout() -> httpx.Timeout:
    return httpx.Timeout(900.0, connect=20.0, read=900.0, write=900.0, pool=20.0)


def _webdav_client(
    username: str,
    password: str,
    *,
    timeout: httpx.Timeout | None = None,
) -> httpx.Client:
    """Create an isolated WebDAV connection.

    Yandex may close a long-lived HTTP connection after accepting a large PUT.
    Disabling keep-alive ensures the subsequent PROPFIND is performed through a
    fresh connection and can confirm an upload even when the PUT response was
    lost in transit.
    """
    return httpx.Client(
        auth=httpx.BasicAuth(username, password),
        timeout=timeout or _webdav_timeout(),
        follow_redirects=True,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=2,
            max_keepalive_connections=0,
            keepalive_expiry=0.0,
        ),
        headers={"Accept": "*/*", "Connection": "close"},
    )


def _verify_yandex_webdav_upload(
    username: str,
    password: str,
    remote_url: str,
    expected_size: int,
    *,
    attempts: int | None = None,
    delay: float | None = None,
) -> tuple[bool, str]:
    attempts = max(
        1,
        int(
            attempts
            if attempts is not None
            else getattr(config, "YANDEX_UPLOAD_VERIFY_ATTEMPTS", 8)
        ),
    )
    delay = max(
        0.0,
        float(
            delay
            if delay is not None
            else getattr(config, "YANDEX_UPLOAD_VERIFY_DELAY", 1.5)
        ),
    )
    last_detail = "Файл ещё не появился в WebDAV-метаданных"
    for attempt in range(attempts):
        try:
            with _webdav_client(username, password) as client:
                info = _webdav_resource_info(client, remote_url)
                if not info["exists"]:
                    last_detail = "Файл ещё не найден на Яндекс.Диске"
                elif info["is_collection"]:
                    last_detail = "Удалённый ресурс неожиданно является каталогом"
                else:
                    actual_size = info["size"]
                    if actual_size is None:
                        # Some WebDAV implementations omit getcontentlength in a
                        # multistatus body. HEAD is a safe secondary check.
                        head = client.head(remote_url)
                        if head.status_code == 200:
                            raw_size = str(head.headers.get("Content-Length", "")).strip()
                            if raw_size.isdigit():
                                actual_size = int(raw_size)
                        elif head.status_code != 404:
                            head.raise_for_status()
                    if actual_size == expected_size:
                        return True, f"{expected_size} байт, проверено и подтверждено WebDAV"
                    if actual_size is None:
                        last_detail = "Яндекс.Диск не вернул размер удалённого файла"
                    else:
                        last_detail = (
                            f"На Яндекс.Диске {actual_size} байт вместо {expected_size}"
                        )
        except Exception as error:
            last_detail = f"Ошибка серверной проверки: {error}"
        if attempt + 1 < attempts and delay:
            time.sleep(delay)
    return False, last_detail


def _delete_yandex_webdav_resource(
    username: str,
    password: str,
    remote_url: str,
) -> None:
    try:
        with _webdav_client(username, password) as client:
            response = client.delete(remote_url)
            if response.status_code not in (200, 202, 204, 404):
                response.raise_for_status()
    except Exception as error:
        logger.warning("Не удалось удалить неполный WebDAV-файл: %s", error)


def _upload_to_yandex_webdav(
    path: Path,
    directory: str,
    local_root: Path,
) -> tuple[bool, str] | None:
    """Upload directly through WebDAV when davfs2 credentials are available.

    Returning None means that no credentials were found and the mounted-filesystem
    fallback should be used. A tuple means the direct attempt was performed.
    """
    credentials = _davfs_credentials(local_root)
    if not credentials:
        return None
    username, password = credentials
    base_url = str(getattr(config, "YANDEX_WEBDAV_URL", "https://webdav.yandex.ru")).strip()
    remote = "/".join(part for part in (directory, path.name) if part)
    remote_url = _webdav_url(base_url, remote)
    expected_size = path.stat().st_size
    retries = max(1, int(getattr(config, "YANDEX_UPLOAD_RETRIES", 3)))
    last_error: Exception | None = None
    try:
        with _webdav_client(username, password) as directory_client:
            ensure_yandex_webdav_directory(directory_client, base_url, directory)
    except Exception as error:
        logger.warning("Не удалось подготовить каталог Яндекс.Диска: %s", error)
        return False, str(error)

    for attempt in range(1, retries + 1):
        put_error: Exception | None = None
        try:
            with _webdav_client(username, password) as client:
                headers = {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(expected_size),
                    "Connection": "close",
                }
                response = client.put(remote_url, headers=headers, content=_stream_file(path))
                if response.status_code not in (200, 201, 202, 204):
                    response.raise_for_status()
        except Exception as error:
            put_error = error

        verified, verify_detail = _verify_yandex_webdav_upload(
            username,
            password,
            remote_url,
            expected_size,
        )
        if verified:
            if put_error is not None:
                return (
                    True,
                    f"WebDAV: {remote} ({verify_detail}); ответ PUT был потерян, файл сохранён",
                )
            return True, f"WebDAV: {remote} ({verify_detail})"

        last_error = put_error or RuntimeError(verify_detail)
        if attempt < retries:
            _delete_yandex_webdav_resource(username, password, remote_url)
            logger.warning(
                "Повтор WebDAV-загрузки на Яндекс.Диск %s/%s: %s; проверка: %s",
                attempt,
                retries,
                put_error or "сервер не подтвердил файл",
                verify_detail,
            )
            time.sleep(min(5.0, 1.5 * float(attempt)))
    logger.warning("Ошибка прямой WebDAV-загрузки на Яндекс.Диск: %s", last_error)
    return False, str(last_error or "Неизвестная ошибка WebDAV")


def _copy_to_yandex_mount(path: Path, local_root: Path, directory: str) -> tuple[bool, str]:
    """Fallback for custom mounts: write directly under the final file name.

    davfs2 deliberately delays uploads of recently closed temporary files. Writing
    a hidden temporary file followed by an immediate rename can therefore leave
    only the directory on the remote Disk. The final-name write avoids that path.
    """
    destination_dir = local_root / directory if directory else local_root
    destination = destination_dir / path.name
    expected_size = path.stat().st_size
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        with path.open("rb") as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        actual_size = destination.stat().st_size
        if actual_size != expected_size:
            raise RuntimeError(
                f"После копирования размер {actual_size} байт вместо {expected_size}"
            )
        return True, f"Смонтированный WebDAV: {destination} ({actual_size} байт)"
    except Exception as error:
        try:
            if destination.exists() and destination.stat().st_size != expected_size:
                destination.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning("Ошибка копирования в смонтированный Яндекс.Диск: %s", error)
        return False, str(error)


def _test_yandex_local_path(
    local_path: str | Path | None = None,
    require_mount: bool | None = None,
) -> tuple[bool, str]:
    root = Path(local_path or getattr(config, "YANDEX_LOCAL_PATH", "/mnt/yandex-disk")).expanduser()
    require_mount = (
        bool(getattr(config, "YANDEX_LOCAL_REQUIRE_MOUNT", True))
        if require_mount is None
        else bool(require_mount)
    )
    try:
        if not root.exists():
            return False, f"Каталог {root} не существует"
        if not root.is_dir():
            return False, f"{root} не является каталогом"
        if require_mount and not os.path.ismount(root):
            return False, f"{root} не является активной точкой монтирования"
        test_file = root / f"vpn-service-write-test-{os.getpid()}.tmp"
        with test_file.open("wb") as handle:
            handle.write(b"ok")
            handle.flush()
            os.fsync(handle.fileno())
        if test_file.read_bytes() != b"ok":
            raise RuntimeError("Проверочный файл прочитан с ошибкой")
        test_file.unlink(missing_ok=True)
        usage = shutil.disk_usage(root)
        return True, f"Локальный диск подключён. Свободно {usage.free / (1024**3):.1f} ГБ"
    except Exception as error:
        return False, str(error)


def _test_yandex_webdav(local_root: Path, directory: str) -> tuple[bool, str] | None:
    credentials = _davfs_credentials(local_root)
    if not credentials:
        return None
    username, password = credentials
    base_url = str(getattr(config, "YANDEX_WEBDAV_URL", "https://webdav.yandex.ru")).strip()
    probe_name = f"vpn-service-write-test-{os.getpid()}-{int(time.time())}.tmp"
    remote = "/".join(part for part in (directory, probe_name) if part)
    url = _webdav_url(base_url, remote)
    uploaded_or_confirmed = False
    try:
        with _webdav_client(
            username,
            password,
            timeout=httpx.Timeout(30.0, connect=15.0),
        ) as client:
            ensure_yandex_webdav_directory(client, base_url, directory)
        put_error: Exception | None = None
        try:
            with _webdav_client(
                username,
                password,
                timeout=httpx.Timeout(30.0, connect=15.0),
            ) as client:
                response = client.put(
                    url,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": "2",
                        "Connection": "close",
                    },
                    content=b"ok",
                )
                if response.status_code not in (200, 201, 202, 204):
                    response.raise_for_status()
        except Exception as error:
            put_error = error
        verified, detail = _verify_yandex_webdav_upload(
            username,
            password,
            url,
            2,
            attempts=3,
            delay=0.5,
        )
        if not verified:
            raise RuntimeError(f"{put_error or 'PUT выполнен'}, но проверка не пройдена: {detail}")
        uploaded_or_confirmed = True
        suffix = "; ответ PUT был потерян" if put_error else ""
        return True, f"WebDAV-запись и серверная проверка выполнены успешно{suffix}"
    except Exception as error:
        return False, str(error)
    finally:
        if uploaded_or_confirmed:
            _delete_yandex_webdav_resource(username, password, url)


def test_yandex_write(token: str | None = None, mode: str | None = None, local_path: str | Path | None = None, require_mount: bool | None = None) -> tuple[bool, str]:
    """Perform a real write + remote verification, then remove the probe file."""
    probe = Path(tempfile.mkstemp(prefix="vpn_yandex_probe_", suffix=".bin")[1])
    try:
        payload = ("FargoVPN-YANDEX-PROBE-" + str(time.time_ns())).encode("utf-8")
        probe.write_bytes(payload)
        mode_value = yandex_mode(mode)
        directory = str(getattr(config, "YANDEX_DISK_PATH", "VPN-Service-Backups")).strip().strip("/")
        if mode_value == "local":
            root = Path(local_path or getattr(config, "YANDEX_LOCAL_PATH", "/mnt/yandex-disk")).expanduser()
            direct = _upload_to_yandex_webdav(probe, directory, root)
            result = direct if direct is not None else _copy_to_yandex_mount(probe, root, directory)
            if result[0] and direct is None:
                destination = root / directory / probe.name if directory else root / probe.name
                destination.unlink(missing_ok=True)
            elif result[0] and direct is not None:
                credentials = _davfs_credentials(root)
                if credentials:
                    remote = "/".join(part for part in (directory, probe.name) if part)
                    _delete_yandex_webdav_resource(credentials[0], credentials[1], _webdav_url(str(getattr(config, "YANDEX_WEBDAV_URL", "https://webdav.yandex.ru")).strip(), remote))
            return result
        token_value = str(token if token is not None else getattr(config, "YANDEX_DISK_TOKEN", "")).strip()
        if not token_value:
            return False, "OAuth-токен не указан"
        ok, detail = upload_to_yandex_with_name(probe, token_value, directory)
        return ok, detail
    except Exception as error:
        return False, str(error)
    finally:
        probe.unlink(missing_ok=True)


def upload_to_yandex_with_name(path: Path, token: str, directory: str) -> tuple[bool, str]:
    """Run the exact production REST uploader with an explicit token/path.

    Settings -> test upload intentionally passes credentials which may not yet be
    saved. The actual transport is the same production uploader used by manual and
    scheduled backups; only cleanup of the probe file is test-specific.
    """
    ok, detail = upload_to_yandex(path, token_override=token, directory_override=directory)
    if ok:
        remote = "/".join(part for part in (str(directory).strip().strip("/"), path.name) if part)
        try:
            with httpx.Client(headers=_yandex_headers(token), timeout=15.0, trust_env=False, follow_redirects=True) as client:
                client.delete(f"{YANDEX_API}/resources", params={"path": _disk_path(remote)})
        except Exception as error:
            logger.warning("Тестовая загрузка Яндекс.Диска выполнена, но probe-файл не удалён: %s", error)
        return True, f"Реальная тестовая загрузка и проверка Яндекс.Диска успешны: {detail}"
    return False, detail


def yandex_mode(value: str | None = None) -> str:
    mode = str(value if value is not None else getattr(config, "YANDEX_DISK_MODE", "oauth")).strip().lower()
    return mode if mode in {"oauth", "local"} else "oauth"


def test_yandex_connection(
    token: str | None = None,
    mode: str | None = None,
    local_path: str | Path | None = None,
    require_mount: bool | None = None,
) -> tuple[bool, str]:
    if yandex_mode(mode) == "local":
        root = Path(local_path or getattr(config, "YANDEX_LOCAL_PATH", "/mnt/yandex-disk")).expanduser()
        directory = str(getattr(config, "YANDEX_DISK_PATH", "VPN-Service-Backups")).strip().strip("/")
        direct = _test_yandex_webdav(root, directory)
        if direct is not None:
            return direct
        return _test_yandex_local_path(root, require_mount=require_mount)
    token = (token if token is not None else getattr(config, "YANDEX_DISK_TOKEN", "")).strip()
    if not token:
        return False, "OAuth-токен не указан"
    try:
        with httpx.Client(headers=_yandex_headers(token), timeout=15.0) as client:
            response = client.get(YANDEX_API)
            response.raise_for_status()
            data = response.json()
            total = int(data.get("total_space", 0) or 0)
            used = int(data.get("used_space", 0) or 0)
        return True, f"Подключено. Свободно {max(0, total - used) / (1024**3):.1f} ГБ"
    except Exception as error:
        return False, str(error)


def _verify_yandex_rest_upload(
    client: httpx.Client,
    remote: str,
    expected_size: int,
) -> tuple[bool, str]:
    attempts = max(1, int(getattr(config, "YANDEX_UPLOAD_VERIFY_ATTEMPTS", 8)))
    delay = max(0.0, float(getattr(config, "YANDEX_UPLOAD_VERIFY_DELAY", 1.5)))
    last_detail = "Файл ещё не появился в метаданных"
    for attempt in range(attempts):
        response = client.get(
            f"{YANDEX_API}/resources",
            params={"path": _disk_path(remote), "fields": "name,path,type,size"},
        )
        if response.status_code == 200:
            data = response.json()
            if str(data.get("type", "")) != "file":
                last_detail = "Созданный ресурс не является файлом"
            else:
                actual_size = int(data.get("size", -1) or 0)
                if actual_size == expected_size:
                    return True, f"{remote} ({actual_size} байт, проверено)"
                last_detail = f"На Диске {actual_size} байт вместо {expected_size}"
        elif response.status_code == 404:
            last_detail = "Файл ещё не найден на Диске"
        else:
            response.raise_for_status()
        if attempt + 1 < attempts and delay:
            time.sleep(delay)
    return False, last_detail


def yandex_upload_enabled() -> bool:
    """Return the effective setting used by the production backup uploader."""
    return bool(getattr(config, "YANDEX_DISK_ENABLED", False))


def upload_to_yandex(
    path: Path,
    token_override: str | None = None,
    directory_override: str | None = None,
) -> tuple[bool, str]:
    """Upload an archive to Yandex.Disk with a fresh upload URL on every retry.

    The upload URL returned by the Disk API is temporary. Reusing it after a
    transient PUT/connection error can fail even when the credentials are fine,
    so every retry obtains a new URL and then verifies the final remote size.
    """
    if token_override is None and not yandex_upload_enabled():
        return False, "Отключено: загрузка на Яндекс.Диск выключена в настройках"
    path = Path(path)
    if not path.is_file():
        return False, f"Архив не найден: {path}"
    expected_size = path.stat().st_size
    if expected_size <= 0:
        return False, "Архив пуст"

    mode = yandex_mode()
    directory = str(directory_override if directory_override is not None else getattr(config, "YANDEX_DISK_PATH", "VPN-Service-Backups")).strip().strip("/")
    if mode == "local":
        local_root = Path(getattr(config, "YANDEX_LOCAL_PATH", "/mnt/yandex-disk")).expanduser()
        direct = _upload_to_yandex_webdav(path, directory, local_root)
        if direct is not None:
            if direct[0]:
                return direct
            direct_detail = direct[1]
            mount_ok, mount_detail = _test_yandex_local_path(local_root)
            if not mount_ok:
                return False, f"Прямой WebDAV: {direct_detail}; резервный путь: {mount_detail}"
            logger.warning("Прямой WebDAV не подтвердил загрузку; выполняется резервная запись через точку монтирования")
            copied, copied_detail = _copy_to_yandex_mount(path, local_root, directory)
            if not copied:
                return False, f"Прямой WebDAV: {direct_detail}; резервный путь: {copied_detail}"
            credentials = _davfs_credentials(local_root)
            if credentials:
                remote = "/".join(part for part in (directory, path.name) if part)
                remote_url = _webdav_url(str(getattr(config, "YANDEX_WEBDAV_URL", "https://webdav.yandex.ru")).strip(), remote)
                verified, verify_detail = _verify_yandex_webdav_upload(credentials[0], credentials[1], remote_url, expected_size)
                if verified:
                    return True, f"{copied_detail}; удалённо подтверждено: {verify_detail}"
                return False, f"Файл записан через точку монтирования, но Яндекс.Диск не подтвердил его: {verify_detail}. Прямой WebDAV: {direct_detail}"
            return True, copied_detail
        ok, detail = _test_yandex_local_path(local_root)
        if not ok:
            return False, detail
        logger.warning("Учётные данные davfs2 не найдены; используется резервная запись через точку монтирования")
        return _copy_to_yandex_mount(path, local_root, directory)

    token = str(token_override if token_override is not None else getattr(config, "YANDEX_DISK_TOKEN", "")).strip()
    if not token:
        return False, "Не задан OAuth-токен"
    remote = "/".join(part for part in (directory, path.name) if part)
    retries = max(1, int(getattr(config, "YANDEX_UPLOAD_RETRIES", 5)))
    last_error = "Неизвестная ошибка"

    for attempt in range(1, retries + 1):
        try:
            timeout = _webdav_timeout()
            with httpx.Client(
                headers=_yandex_headers(token),
                timeout=timeout,
                follow_redirects=True,
                trust_env=False,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=0, keepalive_expiry=0),
            ) as client:
                ensure_yandex_directory(client, directory)
                response = client.get(
                    f"{YANDEX_API}/resources/upload",
                    params={"path": _disk_path(remote), "overwrite": "true"},
                )
                response.raise_for_status()
                upload_info = response.json()
                href = str(upload_info.get("href") or "").strip()
                method = str(upload_info.get("method") or "PUT").upper()
                if not href:
                    raise RuntimeError("Яндекс.Диск не вернул URL загрузки")
                if method != "PUT":
                    raise RuntimeError(f"Неожиданный метод загрузки Яндекс.Диска: {method}")

                logger.info(
                    "Яндекс.Диск: начинаю загрузку %s (%d байт) -> %s",
                    path.name, expected_size, remote,
                )
                with httpx.Client(
                    timeout=timeout,
                    follow_redirects=True,
                    trust_env=False,
                    limits=httpx.Limits(max_connections=2, max_keepalive_connections=0, keepalive_expiry=0),
                ) as upload_client:
                    upload = upload_client.put(
                        href,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(expected_size),
                            "Connection": "close",
                        },
                        content=_stream_file(path),
                    )
                    if upload.status_code not in (200, 201, 202, 204):
                        body = (upload.text or "").strip().replace("\n", " ")[:500]
                        raise RuntimeError(
                            f"Яндекс.Диск вернул HTTP {upload.status_code}"
                            + (f": {body}" if body else "")
                        )

                verified, detail = _verify_yandex_rest_upload(client, remote, expected_size)
                if verified:
                    return True, f"Попытка {attempt}/{retries}: {detail}"
                raise RuntimeError(detail)
        except Exception as error:
            last_error = str(error)
            logger.warning("Яндекс.Диск: попытка %s/%s не удалась: %s", attempt, retries, error)
            if attempt < retries:
                time.sleep(min(12.0, 2.0 ** min(attempt - 1, 3)))

    size_gib = expected_size / (1024 ** 3)
    size_hint = (
        f" Размер архива: {size_gib:.2f} ГБ. "
        "Без Yandex 360 одиночный файл свыше 1 ГБ не принимается; с активным планом лимит может быть до 50 ГБ."
        if size_gib >= 1.0 else ""
    )
    return False, f"Яндекс.Диск: после {retries} попыток файл не подтверждён: {last_error}.{size_hint}"


async def send_to_telegram(path: Path) -> tuple[bool, str]:
    if not bool(getattr(config, "BACKUP_TELEGRAM", True)):
        return False, "Отключено"
    admins = [int(item) for item in getattr(config, "ADMIN_IDS", []) if int(item) > 0]
    if not admins or not str(getattr(config, "BOT_TOKEN", "")).strip():
        return False, "Не настроены BOT_TOKEN/ADMIN_IDS"
    from aiogram import Bot
    from aiogram.types import FSInputFile

    # The cloud Bot API accepts documents up to 50 MB. Use a conservative
    # default and split large full-folder archives into reconstructable parts.
    part_mb = min(49, max(1, int(getattr(config, "BACKUP_TELEGRAM_PART_MB", 45))))
    bot = Bot(str(config.BOT_TOKEN))
    errors: list[str] = []
    delivered = 0
    try:
        with tempfile.TemporaryDirectory(prefix="vpn_backup_telegram_") as temp_name:
            documents = split_for_telegram(path, Path(temp_name), part_mb * 1_000_000)
            total_parts = len(documents)
            for admin in admins:
                admin_complete = True
                for index, document in enumerate(documents, 1):
                    try:
                        caption = (
                            f"✅ Полный бэкап {config.SERVICE_NAME}\n"
                            f"Версия: {(APP_DIR / 'VERSION').read_text().strip() if (APP_DIR / 'VERSION').exists() else '—'}"
                        )
                        if total_parts > 1:
                            caption += (
                                f"\nЧасть {index}/{total_parts}. После скачивания всех частей: "
                                f"cat {path.name}.part* > {path.name}"
                            )
                        await bot.send_document(admin, FSInputFile(document), caption=caption)
                    except Exception as error:
                        admin_complete = False
                        errors.append(f"{admin}, часть {index}/{total_parts}: {error}")
                        break
                if admin_complete:
                    delivered += 1
    finally:
        await bot.session.close()
    detail = f"Доставлено администраторам: {delivered}; частей на архив: {total_parts}"
    if errors:
        detail += f"; ошибки: {'; '.join(errors)}"
    return delivered > 0, detail


def _record_run(
    archive: Path,
    telegram: tuple[bool, str],
    yandex: tuple[bool, str],
    error: str = "",
) -> None:
    try:
        connection = sqlite3.connect(config.DB_PATH, timeout=15)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    filename TEXT,size INTEGER,telegram_ok INTEGER,telegram_detail TEXT,
                    yandex_ok INTEGER,yandex_detail TEXT,error TEXT
                )
                """
            )
            connection.execute(
                """INSERT INTO backup_runs(
                       filename,size,telegram_ok,telegram_detail,yandex_ok,yandex_detail,error
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    archive.name if archive else "",
                    archive.stat().st_size if archive and archive.exists() else 0,
                    int(telegram[0]), telegram[1], int(yandex[0]), yandex[1], error,
                ),
            )
            connection.commit()
        finally:
            connection.close()
    except Exception as record_error:
        logger.warning("Не удалось записать журнал бэкапов: %s", record_error)



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


def _remote_yandex_file_matches(archive: Path) -> tuple[bool, str]:
    """Check whether the pending archive is already present on Yandex.Disk.

    A delivery can succeed while the original request times out before the
    caller receives its final response. Reconciliation converts that ambiguous
    state into success instead of leaving a permanent pending warning.
    """
    if not archive.is_file() or not bool(getattr(config, "YANDEX_DISK_ENABLED", False)):
        return False, "Проверка не выполнялась"
    directory = _clean_remote_path(getattr(config, "YANDEX_DISK_PATH", "VPN-Service-Backups"))
    remote = "/".join(part for part in (directory, archive.name) if part)
    expected_size = archive.stat().st_size
    try:
        if yandex_mode() == "local":
            root = Path(getattr(config, "YANDEX_LOCAL_PATH", "/mnt/yandex-disk")).expanduser()
            candidate = root.joinpath(*remote.split("/"))
            if candidate.is_file() and candidate.stat().st_size == expected_size:
                return True, f"Подтверждено локальным WebDAV-каталогом: {candidate}"
            return False, "Файл не найден или размер отличается"
        token = str(getattr(config, "YANDEX_DISK_TOKEN", "")).strip()
        if not token:
            return False, "OAuth-токен не задан"
        with httpx.Client(headers=_yandex_headers(token), timeout=httpx.Timeout(12.0, connect=5.0), follow_redirects=True, trust_env=False) as client:
            response = client.get(
                f"{YANDEX_API}/resources",
                params={"path": _disk_path(remote), "fields": "name,path,type,size"},
            )
            if response.status_code == 404:
                return False, "Файл ещё не найден на Яндекс.Диске"
            response.raise_for_status()
            data = response.json()
            actual_size = int(data.get("size", -1) or 0)
            if str(data.get("type") or "") == "file" and actual_size == expected_size:
                return True, f"Файл подтверждён на Яндекс.Диске ({actual_size} байт)"
            return False, f"Файл есть, но размер {actual_size} вместо {expected_size} байт"
    except Exception as exc:
        return False, f"Проверка Яндекс.Диска недоступна: {exc}"


def reconcile_pending_deliveries(max_items: int = 3) -> int:
    """Reconcile ambiguous deliveries against the actual remote state.

    Yandex uploads may complete before a network timeout is returned. For each
    confirmed archive we remove only the Yandex destination from the pending
    queue and update the matching backup_runs row. Telegram failures remain
    pending independently.
    """
    pending = _pending_deliveries()
    if not pending:
        return 0
    changed = False
    confirmed = 0
    checked = 0
    remaining: list[dict[str, Any]] = []
    reconciled_files: list[str] = []
    limit = max(1, int(max_items))
    for original in pending:
        item = dict(original)
        names = {str(name) for name in item.get("remaining", []) if str(name)}
        if "yandex" not in names or checked >= limit:
            remaining.append(item)
            continue
        archive = Path(str(item.get("archive", ""))).expanduser()
        ok, detail = _remote_yandex_file_matches(archive)
        checked += 1
        if ok:
            names.discard("yandex")
            details = dict(item.get("details") or {})
            details.pop("yandex", None)
            item["details"] = details
            item["reconciled_at"] = time.time()
            changed = True
            confirmed += 1
            if names:
                item["remaining"] = sorted(names)
                remaining.append(item)
            reconciled_files.append(archive.name)
            logger.info("Очередь бэкапа сверена: %s подтверждён на Яндекс.Диске", archive.name)
        else:
            details = dict(item.get("details") or {})
            previous = str(details.get("yandex") or "")
            details["yandex"] = detail
            item["details"] = details
            item["last_reconciled_at"] = time.time()
            if previous != detail:
                changed = True
            else:
                # Persist the last reconciliation timestamp as well, so the
                # queue state records that we really checked the remote file.
                changed = True
            remaining.append(item)
    if changed:
        _save_delivery_state(pending=remaining)
        try:
            with sqlite3.connect(str(config.DB_PATH), timeout=20) as connection:
                for filename in reconciled_files:
                    connection.execute(
                        "UPDATE backup_runs SET yandex_ok=1,yandex_detail=? WHERE filename=?",
                        ("Подтверждено повторной сверкой на Яндекс.Диске", filename),
                    )
                connection.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось обновить журнал сверки Яндекс.Диска: %s", exc)
    return confirmed


def _destination_enabled(name: str) -> bool:
    if name == "yandex":
        return bool(getattr(config, "YANDEX_DISK_ENABLED", False))
    if name == "telegram":
        return bool(getattr(config, "BACKUP_TELEGRAM", True))
    return False


def _deliver_archive(archive: Path, destinations: set[str] | None = None) -> dict[str, tuple[bool, str]]:
    destinations = destinations or {"telegram", "yandex"}
    result: dict[str, tuple[bool, str]] = {}
    delivery_started = time.monotonic()
    logger.info("performance operation=backup_delivery_start archive=%s destinations=%s size_bytes=%s", archive.name, ",".join(sorted(destinations)), archive.stat().st_size if archive.is_file() else -1)
    if "telegram" in destinations and _destination_enabled("telegram"):
        try:
            started = time.monotonic()
            result["telegram"] = asyncio.run(send_to_telegram(archive))
            logger.info("performance operation=backup_delivery destination=telegram archive=%s duration_ms=%s success=%s", archive.name, int((time.monotonic()-started)*1000), bool(result["telegram"][0]))
        except Exception as error:
            result["telegram"] = (False, str(error))
            logger.error("performance operation=backup_delivery destination=telegram archive=%s duration_ms=%s success=false error=%s", archive.name, int((time.monotonic()-started)*1000), error)
    elif "telegram" in destinations:
        result["telegram"] = (True, "Отключено")
    if "yandex" in destinations and _destination_enabled("yandex"):
        started = time.monotonic()
        result["yandex"] = upload_to_yandex(archive)
        logger.info("performance operation=backup_delivery destination=yandex archive=%s duration_ms=%s success=%s", archive.name, int((time.monotonic()-started)*1000), bool(result["yandex"][0]))
        if result["yandex"][0]:
            logger.info("Яндекс.Диск: бэкап %s успешно доставлен: %s", archive.name, result["yandex"][1])
        else:
            logger.error("Яндекс.Диск: бэкап %s не доставлен: %s", archive.name, result["yandex"][1])
    elif "yandex" in destinations:
        result["yandex"] = (True, "Отключено")



    remote = str(getattr(config, "RCLONE_REMOTE", "")).strip()
    if remote and archive.is_file():
        try:
            rclone = subprocess.run(
                ["rclone", "copyto", str(archive), f"{remote}:{getattr(config, 'RCLONE_PATH', 'VPN-Service-Backups').strip('/')}/{archive.name}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            if rclone.returncode:
                logger.warning("rclone backup failed: %s", (rclone.stderr or rclone.stdout or "unknown")[-500:])
            else:
                logger.info("rclone backup uploaded: %s", archive.name)
        except Exception as error:
            logger.warning("rclone backup failed: %s", error)
    logger.info("performance operation=backup_delivery_end archive=%s duration_ms=%s", archive.name, int((time.monotonic()-delivery_started)*1000))
    return result


def _update_pending_after_delivery(archive: Path, results: dict[str, tuple[bool, str]]) -> None:
    state = _read_state()
    pending = _pending_deliveries()
    enabled_failed = [name for name, result in results.items() if _destination_enabled(name) and not result[0]]
    existing = next((item for item in pending if str(item.get("archive")) == str(archive)), None)
    if enabled_failed:
        item = existing or {"archive": str(archive), "created_ts": time.time(), "attempts": 0}
        item["attempts"] = int(item.get("attempts", 0) or 0) + 1
        item["last_attempt_ts"] = time.time()
        item["remaining"] = enabled_failed
        item["details"] = {name: results[name][1] for name in enabled_failed}
        pending = [entry for entry in pending if str(entry.get("archive")) != str(archive)]
        pending.append(item)
    else:
        pending = [entry for entry in pending if str(entry.get("archive")) != str(archive)]
    state["pending_deliveries"] = pending
    # The archive timestamp is the point from which BACKUP_INTERVAL_DAYS is
    # measured.  Previously this code preserved the old value, so every daily
    # timer run looked like a new backup was due and BACKUP_INTERVAL_DAYS was
    # effectively ignored after the first successful backup.
    state["last_backup_ts"] = time.time()
    state["last_archive"] = str(archive)
    _write_state(state)


def retry_pending_deliveries() -> int:
    reconcile_pending_deliveries()
    pending = _pending_deliveries()
    if not pending:
        return 0
    now = time.time()
    retry_interval = max(60, int(getattr(config, "BACKUP_RETRY_INTERVAL_SECONDS", 900)))
    keep_days = max(1, int(getattr(config, "BACKUP_PENDING_KEEP_DAYS", 30)))
    remaining: list[dict[str, Any]] = []
    retried = 0
    changed = False
    for item in pending:
        archive = Path(str(item.get("archive", ""))).expanduser()
        created = float(item.get("created_ts", now) or now)
        last_attempt = float(item.get("last_attempt_ts", 0) or 0)
        if not archive.is_file() or now - created > keep_days * 86400:
            changed = True
            logger.warning("Удалена просроченная запись очереди бэкапов: %s", archive)
            continue
        if last_attempt and now - last_attempt < retry_interval:
            remaining.append(item)
            continue
        names = {str(name) for name in item.get("remaining", []) if str(name)}
        if not names:
            changed = True
            continue
        results = _deliver_archive(archive, names)
        retried += 1
        still_failed = [name for name, result in results.items() if _destination_enabled(name) and not result[0]]
        item["attempts"] = int(item.get("attempts", 0) or 0) + 1
        item["last_attempt_ts"] = now
        item["details"] = {name: results[name][1] for name in still_failed}
        if still_failed:
            item["remaining"] = still_failed
            remaining.append(item)
        else:
            changed = True
        changed = True
        _record_run(archive, results.get("telegram", (False, "Не выполнялось")), results.get("yandex", (False, "Не выполнялось")), "Повторная доставка")
    if changed:
        _save_delivery_state(pending=remaining)
    return retried

def create_backup() -> Path:
    started = time.monotonic()
    root = Path(config.BACKUP_DIR)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = root / f"vpn_service_full_backup_{stamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="vpn_service_backup_") as temp_name:
        stage = Path(temp_name) / "vpn_service_backup"
        bot_copy = stage / "bot"
        databases = stage / "databases"
        stage.mkdir(parents=True, exist_ok=True)
        shutil.copytree(APP_DIR, bot_copy, symlinks=False, ignore=_copy_ignore)

        _replace_copied_database(Path(config.DB_PATH), bot_copy, databases, "vpn_bot.db")
        _replace_copied_database(Path(config.XUI_DB_PATH), bot_copy, databases, "x-ui.db")
        _copy_systemd(stage / "systemd")

        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "service": str(config.SERVICE_NAME),
            "version": (APP_DIR / "VERSION").read_text().strip() if (APP_DIR / "VERSION").exists() else "unknown",
            "bot_directory": str(APP_DIR),
            "bot_database": str(config.DB_PATH),
            "xui_database": str(config.XUI_DB_PATH),
            "includes_venv": bool(getattr(config, "BACKUP_INCLUDE_VENV", True)),
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
            tar.add(stage, arcname="vpn_service_backup", recursive=True)
        with tarfile.open(archive, "r:gz") as tar:
            if not tar.getmembers():
                raise RuntimeError("Создан пустой архив")
    os.chmod(archive, 0o600)
    logger.info("performance operation=backup_create archive=%s duration_ms=%s size_bytes=%s", archive.name, int((time.monotonic()-started)*1000), archive.stat().st_size)
    return archive


def cleanup_old_backups() -> None:
    root = Path(config.BACKUP_DIR)
    cutoff = datetime.now() - timedelta(days=max(1, int(getattr(config, "BACKUP_KEEP_DAYS", 14))))
    for path in root.glob("vpn_service_*backup_*.tar.gz"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
        except OSError as error:
            logger.warning("Не удалось удалить старый архив %s: %s", path, error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="создать архив независимо от интервала")
    parser.add_argument("--scheduled", action="store_true", help="запуск из таймера")
    parser.add_argument("--local-only", action="store_true", help="не отправлять архив наружу")
    parser.add_argument(
        "--test-yandex",
        action="store_true",
        help="создать, проверить и удалить небольшой тестовый файл на Яндекс.Диске",
    )
    args = parser.parse_args(argv)

    if args.test_yandex:
        ok, detail = test_yandex_connection()
        print(json.dumps({"ok": bool(ok), "detail": str(detail)}, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    reload_runtime_config()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Другой процесс бэкапа уже выполняется; повторный запуск пропущен")
            return 0

        pending_before = retry_pending_deliveries()
        if not args.force and not scheduled_backup_due():
            interval_days = max(1, int(getattr(config, "BACKUP_INTERVAL_DAYS", 3)))
            logger.info("Новый архив по интервалу пока не нужен; текущая настройка BACKUP_INTERVAL_DAYS=%s; очередь доставки проверена (повторено: %s)", interval_days, pending_before)
            return 0

        archive: Path | None = None
        telegram = (False, "Не выполнялось")
        yandex = (False, "Не выполнялось")
        try:
            archive = create_backup()
            created_ts = time.time()
            cleanup_old_backups()
            if args.local_only:
                _save_delivery_state(last_backup_ts=created_ts, last_archive=str(archive))
                print(archive)
                return 0

            results = _deliver_archive(archive)
            telegram = results.get("telegram", telegram)
            yandex = results.get("yandex", yandex)
            _update_pending_after_delivery(archive, results)
            _record_run(archive, telegram, yandex)
            state = _read_state()
            pending_count = len(state.get("pending_deliveries", []))
            summary = {
                "archive": str(archive),
                "telegram": telegram[0],
                "yandex": yandex[0],
                "pending": pending_count,
            }
            print(json.dumps(summary, ensure_ascii=False))
            # A backup that is safely queued for retry is not considered a failed
            # backup job; systemd will continue to wake the daily retry loop.
            return 0
        except Exception as error:
            logger.exception("Бэкап завершился ошибкой")
            if archive is not None:
                _record_run(archive, telegram, yandex, str(error))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
