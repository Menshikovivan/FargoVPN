#!/usr/bin/env python3
"""Публикация, поиск, проверка и запуск обновлений VPN Service Platform."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

import config
from detached_jobs import DetachedJobError, launch_detached

APP_DIR = Path(__file__).resolve().parent
_CHECK_LOCK = threading.Lock()
_START_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()
_CHECK_CACHE: dict[str, Any] = {"ts": 0.0, "info": None}
PUBLISHER_USERNAME = str(getattr(config, "UPDATE_PUBLISHER_USERNAME", "") or "").strip()


class UpdateError(RuntimeError):
    pass


class RussianArgumentParser(argparse.ArgumentParser):
    """ArgumentParser с русскими заголовками, справкой и типовыми ошибками."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "позиционные аргументы"
        self._optionals.title = "параметры"
        self.add_argument("-h", "--help", action="help", help="показать эту справку и выйти")

    @staticmethod
    def _translate_error(message: str) -> str:
        replacements = (
            ("the following arguments are required: ", "не указаны обязательные аргументы: "),
            ("unrecognized arguments: ", "неизвестные аргументы: "),
            ("expected one argument", "требуется одно значение"),
            ("invalid choice:", "недопустимое значение:"),
            ("argument ", "параметр "),
        )
        for source, target in replacements:
            message = message.replace(source, target)
        return message

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "использование:", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "использование:", 1)

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: ошибка: {self._translate_error(message)}\n")


def update_dir() -> Path:
    return Path(getattr(config, "UPDATE_DIR", "/var/lib/vpn-service/updates"))


def current_version() -> str:
    try:
        return (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def installed_changelog() -> dict[str, str]:
    """Return the changelog captured when the currently installed version was installed."""
    try:
        version = (APP_DIR / "INSTALLED_CHANGELOG_VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = current_version()
    try:
        text = (APP_DIR / "INSTALLED_CHANGELOG.md").read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if not text:
        # Backward-compatible fallback for installations that already contain release notes.
        # Prefer the exact installed version before considering any historical notes.
        exact = APP_DIR.parent / f"RELEASE_NOTES_{version}.md"
        if exact.is_file():
            try:
                text = exact.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
        candidates = []
        for path in APP_DIR.parent.glob("RELEASE_NOTES_*.md"):
            m = re.match(r"RELEASE_NOTES_(.+)\.md$", path.name, re.I)
            if m:
                candidates.append((version_key(m.group(1)), m.group(1), path))
        if candidates:
            _, guessed_version, path = sorted(candidates, reverse=True, key=lambda item: item[0])[0]
            version = version or guessed_version
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
    return {"version": version or current_version(), "text": text[:30000]}


def version_key(value: str) -> tuple[int, ...]:
    parts = [int(item) for item in re.findall(r"\d+", str(value))]
    return tuple((parts + [0, 0, 0])[:6])


def is_newer(candidate: str, installed: str | None = None) -> bool:
    return version_key(candidate) > version_key(installed or current_version())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cached_update_info() -> dict[str, Any]:
    """Return cached GitHub release metadata without a network request."""
    with _CHECK_LOCK:
        info = _CHECK_CACHE.get("info")
        if isinstance(info, dict):
            return dict(info)
    installed = current_version()
    return {"installed_version": installed, "available": False, "source": "github", "error": "", "cache_only": True}


def _safe_member_name(name: str) -> str:
    normalized = str(name).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or pure.is_absolute()
        or ".." in pure.parts
        or any(part in ("", ".") for part in pure.parts)
    ):
        raise UpdateError(f"Небезопасный путь в архиве: {name}")
    return normalized


def inspect_archive(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise UpdateError("Архив обновления не найден")
    max_size = max(1, int(getattr(config, "UPDATE_MAX_ARCHIVE_MB", 1024))) * 1024 * 1024
    if path.stat().st_size > max_size:
        raise UpdateError(f"Архив превышает допустимый размер {max_size // (1024 * 1024)} МБ")

    version_member: tarfile.TarInfo | None = None
    version_path = ""
    root_prefix = ""
    install_names: set[str] = set()
    total_unpacked = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                name = _safe_member_name(member.name)
                if member.issym() or member.islnk() or member.isdev() or not (member.isfile() or member.isdir()):
                    raise UpdateError(f"Ссылки и специальные файлы запрещены: {member.name}")
                total_unpacked += max(0, int(member.size or 0))
                if total_unpacked > max_size * 8:
                    raise UpdateError("Слишком большой распакованный объём архива")
                install_names.add(name.rstrip("/"))
                # Supported release layouts:
                #   VERSION + install.sh + application files at the archive root (current format)
                #   app/VERSION + install.sh + application files at the archive root (legacy format)
                #   <prefix>/app/VERSION + <prefix>/install.sh (packaged/legacy format)
                if name == "VERSION" or name.endswith("/VERSION"):
                    candidate_prefix = name[:-len("VERSION")].rstrip("/")
                    candidate_is_app_layout = candidate_prefix == "app" or candidate_prefix.endswith("/app")
                    candidate_root = candidate_prefix[:-4].rstrip("/") if candidate_is_app_layout else candidate_prefix
                    if version_member is None or len(candidate_root) < len(root_prefix):
                        version_member = member
                        version_path = name
                        root_prefix = candidate_root
            if version_member is None:
                raise UpdateError("В архиве не найден VERSION (поддерживаются VERSION и app/VERSION)")
            install_name = f"{root_prefix + '/' if root_prefix else ''}install.sh"
            if install_name not in install_names:
                raise UpdateError("В архиве не найден install.sh")
            extracted = archive.extractfile(version_member)
            if extracted is None:
                raise UpdateError("Не удалось прочитать версию обновления")
            version = extracted.read(128).decode("utf-8", errors="replace").strip()
    except UpdateError:
        raise
    except (tarfile.TarError, OSError, EOFError) as error:
        raise UpdateError(f"Архив повреждён или не является корректным .tar.gz: {error}") from error
    if not re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", version):
        raise UpdateError("Некорректная версия в архиве")
    return {
        "version": version,
        "root_prefix": root_prefix,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "filename": path.name,
    }


def safe_extract(
    path: Path,
    destination: Path,
    progress: Callable[[int, str], None] | None = None,
) -> Path:
    info = inspect_archive(path)
    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve()
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        total_members = max(1, len(members))
        for index, member in enumerate(members, start=1):
            name = _safe_member_name(member.name)
            target = (destination / name).resolve()
            if destination_resolved != target and destination_resolved not in target.parents:
                raise UpdateError(f"Выход за каталог распаковки: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise UpdateError(f"Недопустимый тип файла: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise UpdateError(f"Не удалось прочитать файл: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, member.mode & 0o777)
            if progress and (index == total_members or index % max(1, total_members // 20) == 0):
                progress(int(index * 100 / total_members), f"Распаковано файлов: {index}/{total_members}")
    root = destination / info["root_prefix"] if info["root_prefix"] else destination
    version_ok = (root / "VERSION").is_file() or (root / "app" / "VERSION").is_file()
    if not (root / "install.sh").is_file() or not version_ok:
        raise UpdateError("После распаковки структура обновления не найдена: нужен install.sh и VERSION/app/VERSION")
    return root


def _latest_path() -> Path:
    return update_dir() / "latest.json"


def latest_local_update(*, verify: bool = True) -> dict[str, Any] | None:
    try:
        data = json.loads(_latest_path().read_text(encoding="utf-8"))
        archive = Path(data.get("path", ""))
        if not archive.is_file():
            return None
        if verify and sha256_file(archive) != data.get("sha256"):
            return None
        data["source"] = "local"
        return data
    except Exception:
        return None


def _read_changelog_from_archive(path: Path, version: str) -> str:
    """Read release notes embedded in the release archive."""
    candidates = [f"RELEASE_NOTES_{version}.md"]
    try:
        with tarfile.open(path, "r:gz") as archive:
            files = [member for member in archive.getmembers() if member.isfile()]
            selected = None
            for member in files:
                name = member.name.replace("\\", "/")
                if name == candidates[0] or name.endswith("/" + candidates[0]):
                    selected = member
                    break
            if selected is None:
                return ""
            source = archive.extractfile(selected)
            return source.read().decode("utf-8", errors="replace").strip()[:30_000] if source else ""
    except Exception:
        return ""


def _preupdate_backup_root() -> Path:
    return Path(str(getattr(config, "BACKUP_DIR", "/var/backups/vpn-service")).strip()).expanduser().resolve()


def _preupdate_systemd_path(path: Path) -> Path:
    name = path.name
    return path.with_name(name[:-7] + ".systemd.tar.gz") if name.endswith(".tar.gz") else path.with_suffix(".systemd.tar.gz")


def _preupdate_version(path: Path) -> str:
    """Best-effort version detection for historical pre-update archives.

    Old installers did not always preserve app/VERSION in a form that this
    parser could see. Rollback must remain possible when the archive is
    structurally valid, so version discovery is deliberately non-blocking.
    """
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = [m for m in archive.getmembers() if m.isfile()]
            candidates: list[str] = []
            for member in members:
                name = member.name.replace("\\", "/").strip("/")
                lower = name.casefold()
                if lower == "app/version" or lower.endswith("/app/version") or lower.endswith("/version"):
                    source = archive.extractfile(member)
                    if source:
                        value = source.read(128).decode("utf-8", errors="replace").strip()
                        if re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", value):
                            return value
                m = re.search(r"(?:^|/)RELEASE_NOTES_([0-9A-Za-z._+-]{1,64})\.md$", name, re.IGNORECASE)
                if m:
                    candidates.append(m.group(1))
                m = re.search(r"(?:^|/)version[_-]?([0-9][0-9A-Za-z._+-]{0,63})$", name, re.IGNORECASE)
                if m:
                    candidates.append(m.group(1))
            if candidates:
                return sorted(candidates, key=version_key, reverse=True)[0]
    except Exception:
        pass
    return ""


def _preupdate_archive_root(path: Path) -> str:
    """Detect the actual application snapshot inside a pre-update archive.

    The installer archives ``OLD`` (which is the application directory itself),
    so real backups commonly look like ``vpn_bot/main.py`` + ``vpn_bot/config.py``.
    Older/full snapshots may instead contain ``root/app/main.py``. We identify
    the directory that directly contains both main.py and config.py and restore
    that directory's contents into APP_DIR.
    """
    with tarfile.open(path, "r:gz") as archive:
        names = {m.name.replace("\\", "/").strip("/") for m in archive.getmembers() if m.name}

    if "main.py" in names and "config.py" in names:
        return ""

    # Find every directory prefix that directly contains both application files.
    candidates: set[str] = set()
    for name in names:
        parts = name.split("/")
        if not parts or parts[-1] not in {"main.py", "config.py"} or len(parts) < 2:
            continue
        parent = "/".join(parts[:-1])
        if f"{parent}/main.py" in names and f"{parent}/config.py" in names:
            candidates.add(parent)

    if not candidates:
        # Support a legacy full-tree snapshot where application files are under
        # an ``app`` directory even when only one of the two markers was retained.
        for suffix in ("/app/main.py", "/app/config.py"):
            for name in names:
                if name.endswith(suffix):
                    candidate = name[: -len(suffix)] + "/app"
                    if f"{candidate}/main.py" in names or f"{candidate}/config.py" in names:
                        candidates.add(candidate.strip("/"))

    if not candidates:
        raise UpdateError("Архив предыдущей установки не содержит снимок каталога приложения")

    # Prefer the shallowest matching application directory.
    return sorted(candidates, key=lambda value: (value.count("/"), len(value)))[0]

def _validate_preupdate_structure(path: Path) -> str:
    """Validate a historical installation archive without requiring a fixed root name."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise UpdateError("Архив предыдущей установки пуст")
            for member in members:
                name = member.name.replace("\\", "/")
                # Old snapshots could include a Python venv with interpreter
                # symlinks. The rollback worker ignores that whole directory
                # and preserves the currently working venv instead.
                in_venv = "/.venv/" in f"/{name.strip('/')}" or name.rstrip("/").endswith("/.venv")
                if (member.issym() or member.islnk()) and in_venv:
                    continue
                if member.issym() or member.islnk() or member.isdev() or not (member.isfile() or member.isdir()):
                    raise UpdateError("Архив предыдущей установки содержит ссылки или специальные файлы")
                if not name or name.startswith("/") or ".." in Path(name).parts:
                    raise UpdateError("Архив предыдущей установки содержит небезопасный путь")
        return _preupdate_archive_root(path)
    except UpdateError:
        raise
    except (tarfile.TarError, OSError, EOFError) as error:
        raise UpdateError(f"Архив предыдущей установки повреждён: {error}") from error


def list_preupdate_backups() -> list[dict[str, Any]]:
    """List rollback snapshots using filesystem metadata only.

    Opening and validating multi-gigabyte archives while rendering the Updates
    page caused long request stalls.  The selected archive is still fully
    validated by ``validate_preupdate_backup`` immediately before rollback.
    """
    root = _preupdate_backup_root()
    if not root.is_dir():
        return []
    result = []
    recent_paths = sorted(
        (
            path
            for path in root.glob("pre_update_*.tar.gz")
            if not path.name.endswith(".systemd.tar.gz")
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )[:10]
    for path in recent_paths:
        try:
            sidecar = _preupdate_systemd_path(path)
            result.append({
                "path": str(path.resolve(strict=True)),
                "filename": path.name,
                "version": "предыдущая версия",
                "created_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                "size": path.stat().st_size,
                "systemd_path": str(sidecar) if sidecar.is_file() else "",
                "valid": True,
                "validation_error": "",
                "archive_root": "",
            })
        except OSError:
            continue
    return result[:10]


def validate_preupdate_backup(path: str) -> Path:
    target = Path(path).expanduser().resolve()
    root = _preupdate_backup_root()
    if target.parent != root or not target.name.startswith("pre_update_") or not target.name.endswith(".tar.gz") or not target.is_file():
        raise UpdateError("Недопустимый архив предыдущей версии")
    _validate_preupdate_structure(target)
    return target


def start_rollback_job(actor: str, backup_path: str) -> dict[str, Any]:
    target = validate_preupdate_backup(backup_path)
    if update_job_busy():
        raise UpdateError("Другая задача обновления уже выполняется")
    job_id = secrets.token_hex(12)
    version = _preupdate_version(target)
    write_job_manifest(job_id, actor, {"source": "rollback", "version": version, "filename": target.name})
    from detached_jobs import DetachedJobError, launch_detached
    command = [str(Path(sys.executable)), str(APP_DIR / "rollback_worker.py"), "--job-id", job_id, "--backup", str(target)]
    unit = f"vpn-service-rollback-{int(time.time())}"
    try:
        launcher = launch_detached(unit, command, description=f"Откат VPN Service ({version})", working_directory=APP_DIR)
    except DetachedJobError as error:
        raise UpdateError(str(error)) from error
    write_status("queued", job_id=job_id, unit=unit, launcher=launcher, progress=2, phase="queue", message="Запущен откат к предыдущей версии", version=version)
    return {"job_id": job_id, "unit": unit, "launcher": launcher, "state": "queued"}


def store_manual_update(
    source: Path,
    original_name: str = "update.tar.gz",
    *,
    allow_reinstall: bool = False,
) -> dict[str, Any]:
    """Store a manually uploaded package for one follower-panel job.

    The archive is inspected before it enters the trusted update directory and
    will be inspected again by the detached worker.  Manual upload therefore
    uses the same traversal, special-file, size and version checks as an online
    update from the publisher.
    """
    info = inspect_archive(source)
    installed = current_version()
    if not is_newer(str(info["version"]), installed) and not (
        allow_reinstall and version_key(str(info["version"])) == version_key(installed)
    ):
        raise UpdateError(
            f"В архиве версия {info['version']}, установлена {installed}; "
            "для обновления нужна более новая версия"
        )
    root = update_dir() / "manual"
    root.mkdir(parents=True, exist_ok=True)
    safe_version = re.sub(r"[^0-9A-Za-z._+-]", "_", str(info["version"]))
    target = root / f"manual_{safe_version}_{str(info['sha256'])[:12]}.tar.gz"
    temp = target.with_suffix(target.suffix + ".part")
    shutil.copy2(source, temp)
    os.chmod(temp, 0o600)
    temp.replace(target)
    metadata = {
        "version": str(info["version"]),
        "filename": target.name,
        "original_filename": Path(original_name).name,
        "size": int(target.stat().st_size),
        "sha256": sha256_file(target),
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "path": str(target),
        "source": "manual",
        "allow_reinstall": bool(allow_reinstall),
        "changelog": _read_changelog_from_archive(target, str(info["version"])),
    }
    # Keep only a few recent manual packages. Never delete the archive pinned
    # by the currently running job: a second administrator tab or a cleanup
    # race must not remove the package while the detached worker is reading it.
    protected: set[Path] = {target.resolve(strict=False)}
    try:
        active_status = read_status()
        if update_job_busy(active_status):
            active_manifest = read_job_manifest(str(active_status.get("job_id") or ""))
            active_info = active_manifest.get("update") if isinstance(active_manifest, dict) else None
            active_path = str(active_info.get("path") or "") if isinstance(active_info, dict) else ""
            if active_path:
                protected.add(Path(active_path).resolve(strict=False))
    except Exception:
        pass
    candidates = sorted(
        (
            item for item in root.glob("manual_*.tar.gz")
            if item.resolve(strict=False) not in protected
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in candidates[4:]:
        try:
            old.unlink()
        except OSError:
            pass
    return metadata


def job_manifest_path(job_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", str(job_id))[:120]
    return update_dir() / "jobs" / f"{safe}.json"


def _job_update_info(info: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(info, dict):
        return {}
    allowed = {
        "source",
        "version",
        "filename",
        "original_filename",
        "size",
        "sha256",
        "path",
        "master_url",
        "configured_master_url",
        "metadata_url",
        "download_url",
        "download_absolute_url",
        "published_at",
        "uploaded_at",
        "allow_reinstall",
        "changelog",
        "github_release_id",
        "github_tag",
        "github_release_url",
        "github_asset_url",
        "github_asset_api_url",
    }
    result = {key: value for key, value in info.items() if key in allowed}
    if result.get("path"):
        path = Path(str(result["path"]))
        if not _path_inside_update_dir(path):
            raise UpdateError("Локальный архив задачи находится вне каталога обновлений")
        result["path"] = str(path.resolve(strict=False))
    return result


def write_job_manifest(job_id: str, actor: str, info: dict[str, Any] | None) -> Path:
    path = job_manifest_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": str(job_id),
        "actor": str(actor)[:100],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "update": _job_update_info(info),
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)
    return path


def read_job_manifest(job_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(job_manifest_path(job_id).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def publisher_enabled() -> bool:
    """Только назначенная учётная запись может публиковать обновления.

    Параметр UPDATE_IS_PUBLISHER сохраняется для совместимости со старыми
    файлами конфигурации, но намеренно игнорируется. Это не позволяет ведомой
    панели открыть ленту пакетов после случайной или злонамеренной смены роли.
    """
    username = str(getattr(config, "WEB_USERNAME", "")).strip()
    return bool(username and hmac_compare(username, PUBLISHER_USERNAME))


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))



# Legacy API helpers retained only for compatibility with older diagnostics/tests.
# They are not used by the 3.0 update path; all live updates use GitHub Releases.
def _auth_headers() -> dict[str, str]:
    token = str(getattr(config, "UPDATE_API_TOKEN", "")).strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def normalize_master_url(value: str) -> str:
    """Validate the publisher URL and remove accidentally pasted UI/API paths."""
    clean = str(value or "").strip().rstrip("/")
    if not clean:
        return ""
    parts = urlsplit(clean)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise UpdateError("Нужен полный URL главной панели с http:// или https://")
    if parts.username or parts.password:
        raise UpdateError("Логин и пароль нельзя указывать внутри URL главной панели")
    if parts.query or parts.fragment:
        raise UpdateError("URL главной панели не должен содержать параметры запроса или якорь")
    path = (parts.path or "").rstrip("/")
    lower_path = path.lower()
    for suffix in ("/api/updates/latest", "/api/updates", "/updates"):
        if lower_path.endswith(suffix):
            path = path[:-len(suffix)].rstrip("/")
            break
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, "", "")).rstrip("/")


def master_url_candidates(value: str) -> list[str]:
    """Return compatible API bases, preserving a real /panel proxy prefix first."""
    clean = normalize_master_url(value)
    if not clean:
        return []
    candidates = [clean]
    parts = urlsplit(clean)
    path = (parts.path or "").rstrip("/")
    if path.lower().endswith("/panel"):
        fallback_path = path[:-len("/panel")].rstrip("/")
        fallback = urlunsplit((parts.scheme, parts.netloc, fallback_path, "", "")).rstrip("/")
        if fallback and fallback not in candidates:
            candidates.append(fallback)
    elif not path:
        # Some installations expose the whole panel below a real /panel proxy
        # prefix. Trying that alias after the root 404 stays on the same origin
        # and therefore does not weaken download-origin pinning.
        fallback = urlunsplit((parts.scheme, parts.netloc, "/panel", "", "")).rstrip("/")
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"]).strip()
    return str(getattr(response, "text", "") or "").strip()[:500]


def fetch_remote_metadata(master_url: str) -> dict[str, Any]:
    """Fetch metadata and recover automatically from an obsolete /panel suffix."""
    candidates = master_url_candidates(master_url)
    if not candidates:
        raise UpdateError("Не задан URL главной панели обновлений")
    verify = bool(getattr(config, "UPDATE_VERIFY_TLS", True))
    attempted: list[str] = []
    connection_errors: list[str] = []
    for index, candidate in enumerate(candidates):
        endpoint = f"{candidate}/api/updates/latest"
        attempted.append(endpoint)
        try:
            response = httpx.get(
                endpoint, headers=_auth_headers(), verify=verify, timeout=8.0,
                follow_redirects=False,
            )
        except httpx.RequestError as error:
            connection_errors.append(f"{endpoint}: {error}")
            continue
        detail = _response_detail(response)
        unpublished = (
            "ещё не загружено" in detail.lower()
            or "еще не загружено" in detail.lower()
        )
        if response.status_code == 404 and index + 1 < len(candidates) and not unpublished:
            continue
        if 300 <= response.status_code < 400:
            location = str(response.headers.get("location") or "").strip()
            suffix = f" на {location}" if location else ""
            raise UpdateError(
                "Главная панель перенаправила запрос API" + suffix
                + ". Проверьте базовый URL и общий API-токен"
            )
        if response.status_code == 401:
            raise UpdateError(
                "Главная панель отклонила запрос: общий API-токен не совпадает "
                f"({endpoint})"
            )
        if response.status_code == 503:
            raise UpdateError(
                "API обновлений на главной панели не настроен. Задайте общий "
                f"API-токен длиной не менее 32 символов ({endpoint})"
            )
        if response.status_code == 404:
            if unpublished:
                raise UpdateError("На главной панели пока не опубликован архив обновления: обновление ещё не опубликовано")
            raise UpdateError(
                "API обновлений не найден на главной панели. Проверьте, что "
                "WEB_USERNAME главной панели совпадает с UPDATE_PUBLISHER_USERNAME и что на ней "
                f"установлена совместимая версия или новее. Проверенный адрес: {endpoint}"
            )
        if response.status_code >= 400:
            suffix = f": {detail}" if detail else ""
            raise UpdateError(
                f"Главная панель вернула HTTP {response.status_code} для {endpoint}{suffix}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise UpdateError("Главная панель вернула некорректный JSON") from error
        remote = _validated_remote_metadata(candidate, payload)
        return {
            **remote,
            "master_url": candidate,
            "configured_master_url": normalize_master_url(master_url),
            "metadata_url": endpoint,
            "attempted_urls": attempted,
            "fallback_used": candidate != candidates[0],
        }
    if connection_errors:
        raise UpdateError("Не удалось подключиться к главной панели: " + "; ".join(connection_errors))
    raise UpdateError("API обновлений не найден. Проверенные адреса: " + ", ".join(attempted))


# Compatibility name retained for update-routing diagnostics and older local
# extensions written against the legacy API.
def fetch_remote_update(master_url: str) -> dict[str, Any]:
    return fetch_remote_metadata(master_url)


def api_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": metadata.get("version"),
        "filename": metadata.get("filename"),
        "size": int(metadata.get("size", 0) or 0),
        "sha256": metadata.get("sha256"),
        "published_at": metadata.get("published_at"),
        "changelog": str(metadata.get("changelog") or ""),
        "download_url": f"/api/updates/download/{quote(str(metadata.get('filename', '')), safe='')}",
    }


def _validated_remote_metadata(master_url: str, payload: Any) -> dict[str, Any]:
    """Проверяет ответ главной панели и разрешает скачивание только с того же источника.

    Ведомая панель не должна считать произвольный ``download_url`` из JSON
    доверенным абсолютным адресом. Контракт главной панели использует один
    фиксированный относительный путь API, а общий bearer-токен всегда передаётся
    в заголовке запроса.
    """
    if not isinstance(payload, dict):
        raise UpdateError("Главная панель вернула некорректные метаданные")

    version = str(payload.get("version") or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", version):
        raise UpdateError("Главная панель вернула некорректную версию")

    filename = str(payload.get("filename") or "").strip()
    if (
        not filename
        or filename != Path(filename).name
        or len(filename) > 255
        or not filename.endswith(".tar.gz")
    ):
        raise UpdateError("Главная панель вернула некорректное имя архива")

    checksum = str(payload.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise UpdateError("Главная панель не передала корректную SHA-256")

    try:
        size = int(payload.get("size") or 0)
    except (TypeError, ValueError) as error:
        raise UpdateError("Главная панель вернула некорректный размер архива") from error
    max_size = max(1, int(getattr(config, "UPDATE_MAX_ARCHIVE_MB", 1024))) * 1024 * 1024
    if size <= 0 or size > max_size:
        raise UpdateError("Размер удалённого архива вне допустимого диапазона")

    advertised_url = str(payload.get("download_url") or "").strip()
    parsed = urlsplit(advertised_url)
    expected_path = f"/api/updates/download/{quote(filename, safe='')}"
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
    ):
        raise UpdateError("Адрес скачивания не принадлежит API главной панели")

    master = urlsplit(master_url)
    if master.scheme not in {"http", "https"} or not master.netloc:
        raise UpdateError("Некорректный URL главной панели")

    changelog = str(payload.get("changelog") or "")[:30_000]
    return {
        **payload,
        "version": version,
        "changelog": changelog,
        "filename": filename,
        "size": size,
        "sha256": checksum,
        "download_url": expected_path,
        "download_absolute_url": master_url.rstrip("/") + expected_path,
    }


GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"


def github_owner() -> str:
    value = str(getattr(config, "GITHUB_REPOSITORY_OWNER", "") or "").strip()
    if not value:
        raise UpdateError("Не указан владелец GitHub-репозитория обновлений")
    return value


def github_repo() -> str:
    return str(getattr(config, "GITHUB_REPOSITORY_NAME", "FargoVPN") or "FargoVPN").strip()


def github_token() -> str:
    return str(getattr(config, "GITHUB_API_TOKEN", "") or "").strip()


def github_api_base() -> str:
    value = str(getattr(config, "GITHUB_API_BASE_URL", GITHUB_API) or GITHUB_API).strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc:
        raise UpdateError("GitHub API URL должен начинаться с https://")
    return value


def github_headers(*, binary: bool = False) -> dict[str, str]:
    # GitHub JSON endpoints and the release-asset binary download endpoint
    # intentionally use different media negotiation. Uploads must still
    # advertise the normal JSON media type in Accept, while GET /releases/assets/{id}
    # must request application/octet-stream to receive the actual asset bytes.
    headers = {
        "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    base = github_api_base()
    clean_path = "/" + str(path).lstrip("/")
    headers = dict(kwargs.pop("headers", {}) or {})
    headers = {**github_headers(), **headers}
    try:
        response = httpx.request(method.upper(), base + clean_path, headers=headers, timeout=20.0, follow_redirects=False, **kwargs)
    except httpx.RequestError as error:
        raise UpdateError(f"Не удалось подключиться к GitHub: {error}") from error
    return response


def github_json_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("detail") or "").strip()
        if message:
            return message
    return str(response.text or "").strip()[:500]


def github_repo_url() -> str:
    return f"https://github.com/{github_owner()}/{github_repo()}"


def github_release_tag(version: str) -> str:
    prefix = str(getattr(config, "GITHUB_RELEASE_TAG_PREFIX", "FargoVPN-") or "FargoVPN-").strip()
    if not prefix:
        prefix = "FargoVPN-"
    return f"{prefix}{version}"


def github_release_name(version: str) -> str:
    template = str(getattr(config, "GITHUB_RELEASE_NAME_TEMPLATE", "FargoVPN {version}") or "FargoVPN {version}")
    try:
        return template.format(version=version)
    except Exception:
        return f"FargoVPN {version}"


def github_asset_name(version: str, original_name: str = "") -> str:
    configured = str(getattr(config, "GITHUB_RELEASE_ASSET_NAME", "") or "").strip()
    if configured:
        try:
            configured = configured.format(version=version)
        except Exception:
            pass
    if configured:
        return Path(configured).name
    candidate = Path(original_name).name
    if candidate.endswith(".tar.gz") and candidate != ".tar.gz":
        return candidate
    return f"VPN_Service_Platform_{version}_FULL.tar.gz"


def sanitize_public_release_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"https?://[^\s)\]}>]+", "[ссылка скрыта]", value, flags=re.I)
    value = re.sub(r"\b(?:Bearer|token|pat|api[-_ ]?key|bot[_-]?token)\s*[:=]\s*[^\s]+", "[секрет скрыт]", value, flags=re.I)
    value = re.sub(r"@[A-Za-z0-9_]{5,}", "[telegram_username]", value)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[ip скрыт]", value)
    return value[:30000]


def github_notes(changelog: str, version: str, checksum: str, size: int) -> str:
    prefix = sanitize_public_release_text(str(changelog or "").strip()) or f"Релиз FargoVPN {version}."
    block = [prefix, "", "---", "### Метаданные", f"Версия: `{version}`", f"SHA-256: `{checksum}`", f"Размер архива: `{size}` байт"]
    return "\n".join(block)[:100000]


def github_validate_configuration() -> dict[str, Any]:
    token = github_token()
    if not token:
        raise UpdateError("GitHub Personal Access Token не настроен")
    user = github_request("GET", "/user")
    if user.status_code >= 400:
        raise UpdateError(f"GitHub не принял токен: HTTP {user.status_code}: {github_json_error(user)}")
    repo = github_request("GET", f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}")
    if repo.status_code >= 400:
        raise UpdateError(f"Репозиторий GitHub недоступен: HTTP {repo.status_code}: {github_json_error(repo)}")
    payload = repo.json()
    permissions = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else {}
    if permissions.get("push") is False:
        raise UpdateError("GitHub токен не имеет права записи в выбранный репозиторий")
    return {
        "login": str((user.json() or {}).get("login") or ""),
        "repository": str(payload.get("full_name") or f"{github_owner()}/{github_repo()}"),
        "private": bool(payload.get("private")),
        "default_branch": str(payload.get("default_branch") or "main"),
        "html_url": str(payload.get("html_url") or github_repo_url()),
    }


def github_latest_release() -> dict[str, Any]:
    response = github_request("GET", f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases/latest")
    if response.status_code == 404:
        raise UpdateError("В репозитории GitHub ещё нет опубликованных релизов")
    if response.status_code >= 400:
        raise UpdateError(f"GitHub вернул HTTP {response.status_code}: {github_json_error(response)}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise UpdateError("GitHub вернул некорректные данные последнего релиза")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        assets = []
    configured_name = str(getattr(config, "GITHUB_RELEASE_ASSET_NAME", "") or "").strip()
    asset = None
    if configured_name:
        for item in assets:
            if str(item.get("name") or "") == Path(configured_name.format(version=str(payload.get("tag_name") or ""))).name:
                asset = item
                break
    if asset is None:
        candidates = [item for item in assets if str(item.get("name") or "").endswith(".tar.gz")]
        if candidates:
            asset = sorted(candidates, key=lambda item: int(item.get("size") or 0), reverse=True)[0]
    if not isinstance(asset, dict):
        raise UpdateError("Последний GitHub Release не содержит .tar.gz архива")
    tag = str(payload.get("tag_name") or "").strip()
    prefix = str(getattr(config, "GITHUB_RELEASE_TAG_PREFIX", "FargoVPN-") or "FargoVPN-").strip()
    version = tag[len(prefix):] if prefix and tag.startswith(prefix) else ""
    if not version:
        candidate = str(payload.get("name") or "").replace("FargoVPN", "", 1).strip(" -v")
        version = candidate
    if not re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", version):
        match = re.search(r"(?<![0-9])\d+(?:\.\d+){1,5}(?![0-9])", tag)
        if match:
            version = match.group(0)
    if not re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", version):
        raise UpdateError(f"Не удалось определить версию из GitHub Release: {tag or payload.get('name')}")
    size = int(asset.get("size") or 0)
    max_size = max(1, int(getattr(config, "UPDATE_MAX_ARCHIVE_MB", 1024))) * 1024 * 1024
    if size <= 0 or size > max_size:
        raise UpdateError("Размер архива последнего GitHub Release вне допустимого диапазона")
    digest = str(asset.get("digest") or "").strip().lower()
    checksum = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
    body = str(payload.get("body") or "")
    if not checksum:
        match = re.search(r"SHA-256:\s*`?([0-9a-f]{64})", body, flags=re.I)
        checksum = match.group(1).lower() if match else ""
    return {
        "version": version,
        "filename": str(asset.get("name") or ""),
        "size": size,
        "sha256": checksum,
        "published_at": str(payload.get("published_at") or payload.get("created_at") or ""),
        "changelog": body[:30000],
        "source": "github",
        "github_release_id": int(payload.get("id") or 0),
        "github_tag": tag,
        "github_release_url": str(payload.get("html_url") or ""),
        "github_asset_url": str(asset.get("browser_download_url") or ""),
        "github_asset_api_url": str(asset.get("url") or ""),
        "github_upload_url": str(payload.get("upload_url") or ""),
    }


def _github_release_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert one trusted GitHub release object into update metadata."""
    if not isinstance(payload, dict):
        raise UpdateError("GitHub вернул некорректные данные релиза")
    tag = str(payload.get("tag_name") or "").strip()
    prefix = str(getattr(config, "GITHUB_RELEASE_TAG_PREFIX", "FargoVPN-") or "FargoVPN-").strip()
    version = tag[len(prefix):] if prefix and tag.startswith(prefix) else ""
    if not version:
        version = str(payload.get("name") or "").replace("FargoVPN", "", 1).strip(" -v")
    if not re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", version):
        match = re.search(r"(?<![0-9])\d+(?:\.\d+){1,5}(?![0-9])", tag)
        version = match.group(0) if match else ""
    if not re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", version):
        raise UpdateError(f"Не удалось определить версию релиза: {tag or payload.get('name')}")
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    expected_name = github_asset_name(version)
    asset = next((item for item in assets if str(item.get("name") or "") == expected_name), None)
    if asset is None:
        candidates = [item for item in assets if str(item.get("name") or "").endswith(".tar.gz")]
        asset = max(candidates, key=lambda item: int(item.get("size") or 0)) if candidates else None
    if not isinstance(asset, dict):
        raise UpdateError(f"Релиз {version} не содержит .tar.gz архива")
    size = int(asset.get("size") or 0)
    max_size = max(1, int(getattr(config, "UPDATE_MAX_ARCHIVE_MB", 1024))) * 1024 * 1024
    if size <= 0 or size > max_size:
        raise UpdateError(f"Размер архива релиза {version} вне допустимого диапазона")
    digest = str(asset.get("digest") or "").strip().lower()
    checksum = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
    body = str(payload.get("body") or "")
    if not checksum:
        match = re.search(r"SHA-256:\s*`?([0-9a-f]{64})", body, flags=re.I)
        checksum = match.group(1).lower() if match else ""
    return {
        "version": version,
        "filename": str(asset.get("name") or ""),
        "size": size,
        "sha256": checksum,
        "published_at": str(payload.get("published_at") or payload.get("created_at") or ""),
        "changelog": body[:30000],
        "source": "github",
        "github_release_id": int(payload.get("id") or 0),
        "github_tag": tag,
        "github_release_url": str(payload.get("html_url") or ""),
        "github_asset_url": str(asset.get("browser_download_url") or ""),
        "github_asset_api_url": str(asset.get("url") or ""),
        "github_upload_url": str(payload.get("upload_url") or ""),
    }


def github_release_history(limit: int = 20) -> list[dict[str, Any]]:
    """Return installable published releases, newest first across API pages."""
    safe_limit = max(1, min(int(limit), 100))
    result: list[dict[str, Any]] = []
    page = 1
    per_page = 100
    while len(result) < safe_limit and page <= 10:
        response = github_request(
            "GET",
            f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases?per_page={per_page}&page={page}",
        )
        if response.status_code >= 400:
            raise UpdateError(f"GitHub вернул HTTP {response.status_code}: {github_json_error(response)}")
        payload = response.json()
        if not isinstance(payload, list):
            raise UpdateError("GitHub вернул некорректный список релизов")
        if not payload:
            break
        for release in payload:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            try:
                result.append(_github_release_metadata(release))
            except UpdateError:
                continue
            if len(result) >= safe_limit:
                break
        if len(payload) < per_page:
            break
        page += 1
    return result[:safe_limit]


def github_release_by_version(version: str) -> dict[str, Any]:
    requested = str(version or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", requested):
        raise UpdateError("Некорректно указана версия")
    for release in github_release_history(50):
        if str(release.get("version") or "") == requested:
            return release
    raise UpdateError(f"Опубликованный релиз {requested} не найден")


def _validate_github_metadata(info: dict[str, Any]) -> dict[str, Any]:
    version = str(info.get("version") or "").strip()
    filename = Path(str(info.get("filename") or "")).name
    if not re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", version):
        raise UpdateError("GitHub вернул некорректную версию")
    if not filename.endswith(".tar.gz") or filename in {"", ".tar.gz"}:
        raise UpdateError("GitHub вернул некорректное имя архива")
    size = int(info.get("size") or 0)
    max_size = max(1, int(getattr(config, "UPDATE_MAX_ARCHIVE_MB", 1024))) * 1024 * 1024
    if size <= 0 or size > max_size:
        raise UpdateError("Размер GitHub-архива вне допустимого диапазона")
    checksum = str(info.get("sha256") or "").lower()
    if checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise UpdateError("GitHub передал некорректную SHA-256")
    return {**info, "version": version, "filename": filename, "size": size, "sha256": checksum}


def check_available_update(force: bool = False) -> dict[str, Any]:
    interval = max(10, int(getattr(config, "UPDATE_CHECK_INTERVAL", 300)))
    now = time.time()
    with _CHECK_LOCK:
        if not force and now - float(_CHECK_CACHE.get("ts", 0)) < interval and _CHECK_CACHE.get("info") is not None:
            return dict(_CHECK_CACHE["info"])
    installed = current_version()
    owner = str(getattr(config, "GITHUB_REPOSITORY_OWNER", "") or "").strip()
    repo = github_repo()
    configured_repository = f"{owner}/{repo}" if owner else "не настроен"
    if not owner:
        info = {
            "installed_version": installed,
            "available": False,
            "source": "github",
            "configured_repository": configured_repository,
            "error": "GitHub обновления пока не настроен. Укажите владельца репозитория в Настройки → Обновления.",
        }
    else:
        try:
            remote = github_latest_release()
            info = {
                **remote,
                "installed_version": installed,
                "available": is_newer(str(remote.get("version")), installed),
                "source": "github",
                "error": "",
                "configured_repository": configured_repository,
            }
        except Exception as error:
            info = {
                "installed_version": installed,
                "available": False,
                "source": "github",
                "configured_repository": configured_repository,
                "error": str(error),
            }
    with _CHECK_LOCK:
        _CHECK_CACHE["ts"] = now
        _CHECK_CACHE["info"] = dict(info)
    return info


def invalidate_update_cache() -> None:
    with _CHECK_LOCK:
        _CHECK_CACHE["ts"] = 0.0
        _CHECK_CACHE["info"] = None


def publish_update(source: Path, original_name: str = "update.tar.gz") -> dict[str, Any]:
    info = inspect_archive(source)
    version = str(info["version"])
    root = update_dir()
    published = root / "published"
    published.mkdir(parents=True, exist_ok=True)
    safe_version = re.sub(r"[^0-9A-Za-z._+-]", "_", version)
    target = published / f"vpn_service_update_{safe_version}.tar.gz"
    temp = target.with_suffix(".tmp")
    shutil.copy2(source, temp)
    os.chmod(temp, 0o600)
    temp.replace(target)
    checksum = sha256_file(target)
    changelog = _read_changelog_from_archive(target, version)
    asset_name = github_asset_name(version, original_name)
    branch = str(getattr(config, "GITHUB_TARGET_BRANCH", "main") or "main").strip() or "main"
    tag = github_release_tag(version)
    release_name = github_release_name(version)
    body = github_notes(changelog, version, checksum, target.stat().st_size)
    endpoint = f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases/tags/{quote(tag, safe='')}"
    existing = github_request("GET", endpoint)
    draft = bool(getattr(config, "GITHUB_RELEASE_DRAFT", False))
    prerelease = bool(getattr(config, "GITHUB_RELEASE_PRERELEASE", False))
    make_latest = bool(getattr(config, "GITHUB_RELEASE_MAKE_LATEST", True)) and not draft and not prerelease
    payload = {
        "tag_name": tag,
        "target_commitish": branch,
        "name": release_name,
        "body": body,
        "draft": draft,
        "prerelease": prerelease,
        "make_latest": "true" if make_latest else "false",
    }
    if existing.status_code == 200:
        release = existing.json()
        release_id = int(release.get("id") or 0)
        if release.get("draft") and not payload["draft"]:
            payload["draft"] = False
        response = github_request("PATCH", f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases/{release_id}", json=payload)
        if response.status_code >= 400:
            raise UpdateError(f"Не удалось обновить GitHub Release: HTTP {response.status_code}: {github_json_error(response)}")
        release = response.json()
        for old_asset in (release.get("assets") or []):
            if str(old_asset.get("name") or "") == asset_name:
                delete_response = github_request("DELETE", f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases/assets/{int(old_asset.get('id') or 0)}")
                if delete_response.status_code not in {204, 404}:
                    raise UpdateError(f"Не удалось заменить старый asset GitHub: HTTP {delete_response.status_code}: {github_json_error(delete_response)}")
    elif existing.status_code == 404:
        response = github_request("POST", f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases", json=payload)
        if response.status_code >= 400:
            raise UpdateError(f"Не удалось создать GitHub Release: HTTP {response.status_code}: {github_json_error(response)}")
        release = response.json()
        release_id = int(release.get("id") or 0)
    else:
        raise UpdateError(f"GitHub вернул HTTP {existing.status_code}: {github_json_error(existing)}")
    upload_url = str(release.get("upload_url") or "").replace("{?name,label}", "")
    if not upload_url:
        raise UpdateError("GitHub не вернул upload_url для Release")
    # GitHub requires raw binary bytes sent to the release-specific upload_url.
    # Do not route this through the normal api.github.com JSON helper, and make
    # Content-Length explicit because some HTTP clients otherwise use chunked
    # transfer which has caused intermediary 405 responses.
    upload_url = upload_url.split("{", 1)[0].rstrip("?")
    headers = {**github_headers(), "Content-Type": "application/gzip", "Content-Length": str(target.stat().st_size)}
    try:
        payload_bytes = target.read_bytes()
        upload = httpx.post(upload_url, params={"name": asset_name}, headers=headers, content=payload_bytes, timeout=600.0, follow_redirects=False)
    except httpx.RequestError as error:
        raise UpdateError(f"Не удалось подключиться к GitHub при загрузке asset: {error}") from error
    if upload.status_code == 405:
        # Some environments return 405 when the hypermedia URL is rewritten or
        # passed through a proxy. Reconstruct the canonical upload host from the
        # release id and retry once, still using POST + raw binary.
        canonical = f"https://uploads.github.com/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases/{release_id}/assets"
        if upload_url.rstrip("/") != canonical.rstrip("/"):
            upload = httpx.post(canonical, params={"name": asset_name}, headers=headers, content=payload_bytes, timeout=600.0, follow_redirects=False)
    if upload.status_code >= 400:
        raise UpdateError(f"Не удалось загрузить архив в GitHub Release: HTTP {upload.status_code}: {github_json_error(upload)}")
    asset = upload.json()
    metadata = {
        "version": version,
        "filename": asset_name,
        "original_filename": Path(original_name).name,
        "size": target.stat().st_size,
        "sha256": checksum,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "path": str(target),
        "source": "github",
        "changelog": changelog,
        "github_release_id": int(release.get("id") or 0),
        "github_tag": tag,
        "github_release_url": str(release.get("html_url") or ""),
        "github_asset_url": str(asset.get("browser_download_url") or ""),
    }
    root.mkdir(parents=True, exist_ok=True)
    temp_meta = _latest_path().with_suffix(".tmp")
    temp_meta.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp_meta, 0o600)
    temp_meta.replace(_latest_path())
    invalidate_update_cache()
    return metadata


def _path_inside_update_dir(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
        root = update_dir().resolve(strict=False)
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def obtain_update_archive(info: dict[str, Any], progress: Callable[[int, str], None] | None = None) -> Path:
    if info.get("source") == "manual":
        archive = Path(str(info.get("path") or ""))
        return _verify_local_update_archive(archive, info, progress)
    if info.get("source") == "local":
        archive = Path(str(info.get("path") or ""))
        return _verify_local_update_archive(archive, info, progress)
    remote = _validate_github_metadata(info)
    api_url = str(remote.get("github_asset_api_url") or "").strip()
    browser_url = str(remote.get("github_asset_url") or "").strip()
    url = api_url or browser_url
    if not url:
        raise UpdateError("GitHub не передал URL скачивания архива")
    downloads = update_dir() / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    target = downloads / remote["filename"]
    temp = target.with_suffix(target.suffix + ".part")
    max_size = max(1, int(getattr(config, "UPDATE_MAX_ARCHIVE_MB", 1024))) * 1024 * 1024
    expected_size = int(remote["size"])
    expected_checksum = str(remote.get("sha256") or "").lower()
    temp.unlink(missing_ok=True)
    total = 0
    digest = hashlib.sha256()
    verify = bool(getattr(config, "UPDATE_VERIFY_TLS", True))
    # For the API asset endpoint GitHub returns the binary asset only when
    # Accept: application/octet-stream is requested; otherwise a JSON asset
    # representation may be returned and its byte count then cannot match the
    # release metadata size. browser_download_url is already a direct browser
    # download and does not need the binary media negotiation header.
    headers = github_headers(binary=bool(api_url)) if api_url else {"Accept": "application/octet-stream"}
    try:
        with httpx.stream("GET", url, headers=headers, verify=verify, timeout=300.0, follow_redirects=True) as response:
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "").lower()
            if "application/json" in content_type:
                raise UpdateError("GitHub вернул JSON-описание asset вместо бинарного архива")
            response_size = int(response.headers.get("content-length") or 0)
            if response_size < 0 or response_size > max_size:
                raise UpdateError("GitHub сообщил недопустимый размер архива")
            with temp.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > max_size:
                        raise UpdateError("Скачиваемый GitHub-архив слишком большой")
                    digest.update(chunk)
                    handle.write(chunk)
                    if progress and expected_size > 0:
                        progress(min(99, int(total * 100 / expected_size)), f"Загружено {total} из {expected_size} байт")
        if total != expected_size:
            raise UpdateError(
                f"Размер скачанного GitHub-архива не совпал с метаданными: получено {total} байт, ожидалось {expected_size} байт"
            )
        if expected_checksum and digest.hexdigest().lower() != expected_checksum:
            raise UpdateError("SHA-256 GitHub-архива не совпала")
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    inspected = inspect_archive(target)
    if inspected["version"] != remote["version"]:
        target.unlink(missing_ok=True)
        raise UpdateError("Версия внутри GitHub-архива не совпадает с релизом")
    if progress:
        progress(100, "Архив GitHub загружен и проверен")
    return target


def _verify_local_update_archive(archive: Path, info: dict[str, Any], progress: Callable[[int, str], None] | None = None) -> Path:
    if not archive.is_file() or not _path_inside_update_dir(archive):
        raise UpdateError("Локальный архив обновления не найден")
    inspected = inspect_archive(archive)
    expected_version = str(info.get("version") or "")
    expected_checksum = str(info.get("sha256") or "").lower()
    expected_size = int(info.get("size") or 0)
    if expected_version and inspected["version"] != expected_version:
        raise UpdateError("Версия локального архива изменилась после постановки задачи")
    if expected_checksum and inspected["sha256"] != expected_checksum:
        raise UpdateError("Контрольная сумма локального архива изменилась")
    if expected_size and inspected["size"] != expected_size:
        raise UpdateError("Размер локального архива изменился")
    if progress:
        progress(100, "Локальный архив готов")
    return archive

def stage_update(
    archive: Path,
    progress: Callable[[int, str], None] | None = None,
) -> tuple[Path, dict[str, Any]]:
    info = inspect_archive(archive)
    staging = update_dir() / "staging" / f"{info['version']}-{int(time.time())}-{secrets.token_hex(3)}"
    root = safe_extract(archive, staging, progress=progress)
    return root, info


def status_path() -> Path:
    return update_dir() / "status.json"


def write_status(state: str, **details: Any) -> None:
    """Atomically update persistent installation state across web and workers."""
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _STATUS_LOCK, lock_path.open("a+") as lock_handle:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        previous: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except Exception:
            pass

        previous_job = str(previous.get("job_id") or "")
        incoming_job = str(details.get("job_id") or previous_job)
        previous_state = str(previous.get("state") or "")
        try:
            previous_progress = max(0, min(100, int(previous.get("progress") or 0)))
        except (TypeError, ValueError):
            previous_progress = 0
        requested_progress: int | None = None
        if "progress" in details:
            try:
                requested_progress = max(0, min(100, int(details["progress"])))
            except (TypeError, ValueError):
                requested_progress = 0

        same_job = bool(incoming_job and incoming_job == previous_job)
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
            # A newly launched worker may acknowledge or even finish before the
            # HTTP process stores launcher metadata. Keep the newer phase while
            # still accepting harmless fields such as unit, pid and launcher.
            state = previous_state
            for key in ("phase", "message", "detail", "error", "finished_at"):
                details.pop(key, None)
            details["progress"] = previous_progress
        elif requested_progress is not None:
            incoming_progress = requested_progress
            if same_job and state in BUSY_STATES:
                incoming_progress = max(incoming_progress, previous_progress)
            details["progress"] = incoming_progress

        try:
            revision = int(previous.get("revision") or 0) + 1
        except (TypeError, ValueError):
            revision = 1
        data = {
            **previous,
            "state": state,
            "revision": revision,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **details,
        }
        if "progress" in data:
            try:
                data["progress"] = max(0, min(100, int(data["progress"])))
            except (TypeError, ValueError):
                data["progress"] = 0
        temp = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
        )
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(temp, 0o600)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def read_status() -> dict[str, Any]:
    try:
        return json.loads(status_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


BUSY_STATES = {
    "queued", "checking", "downloading", "verifying", "extracting",
    "scheduled", "installing", "migrating", "rolling-back", "restarting", "health-check",
}


def _status_age_seconds(status: dict[str, Any]) -> float | None:
    value = str(status.get("updated_at") or "").strip()
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def update_job_busy(status: dict[str, Any] | None = None) -> bool:
    current = status if isinstance(status, dict) else read_status()
    if str(current.get("state") or "") not in BUSY_STATES:
        return False
    stale_after = max(900, int(getattr(config, "UPDATE_STALE_JOB_SECONDS", 7200)))
    age = _status_age_seconds(current)
    return age is None or age <= stale_after


def update_log_path() -> Path:
    return update_dir() / "update.log"


def start_update_job(
    actor: str = "web",
    update_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Запустить обновление независимо от жизненного цикла HTTP-запроса.

    В установленной системе используется постоянный шаблон systemd. Запуск
    выполняется с ``--no-block``: для Type=oneshot это принципиально, иначе
    веб-служба может остановиться раньше ответа браузеру. Если шаблон ещё не
    установлен, worker передаётся отдельной transient/runtime systemd-службе.
    Обычный дочерний процесс намеренно не используется: он остался бы в cgroup
    веб-панели и мог бы погибнуть при её перезапуске.
    """
    root = update_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "start.lock"
    with _START_LOCK, lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        current = read_status()
        if update_job_busy(current):
            raise UpdateError("Другая установка обновления уже выполняется")
        if str(current.get("state") or "") in BUSY_STATES:
            write_status(
                "failed",
                progress=int(current.get("progress") or 1),
                phase="stale",
                message="Предыдущая задача обновления перестала отвечать",
                error="Задача признана зависшей; разрешён повторный запуск",
                finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

        job_id = f"upd-{int(time.time())}-{secrets.token_hex(4)}"
        transient_unit = f"vpn-service-update-worker-{int(time.time())}-{secrets.token_hex(2)}"
        unit = ""
        manifest = write_job_manifest(job_id, actor, update_info)
        requested_version = str((update_info or {}).get("version") or "")
        write_status(
            "queued",
            job_id=job_id,
            unit="",
            actor=str(actor)[:100],
            requested_version=requested_version,
            progress=1,
            phase="queue",
            message="Задача обновления поставлена в очередь",
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            finished_at="",
            log=str(update_log_path()),
            manifest=str(manifest),
            error="",
        )
        python = APP_DIR / ".venv" / "bin" / "python"
        if not python.is_file():
            python = Path(os.sys.executable)
        worker = APP_DIR / "update_worker.py"
        command = [
            str(python),
            str(worker),
            "--job-id",
            job_id,
            "--startup-delay",
            "4.0",
        ]
        launcher = ""
        launcher_error = ""
        process_id = 0
        try:
            template = Path("/etc/systemd/system/vpn-service-update@.service")
            if (
                shutil.which("systemctl")
                and Path("/run/systemd/system").exists()
                and template.is_file()
            ):
                unit = f"vpn-service-update@{job_id}.service"
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
                        description=f"Фоновое обновление VPN Service ({job_id})",
                        working_directory=APP_DIR,
                    )
                    unit = transient_unit
                except DetachedJobError as error:
                    details = "; ".join(
                        item for item in (launcher_error, str(error)) if item
                    )
                    raise UpdateError(
                        details or "Не удалось запустить отдельную systemd-службу обновления"
                    ) from error

            write_status(
                "queued",
                job_id=job_id,
                unit=unit,
                pid=process_id,
                launcher=launcher,
                progress=2,
                phase="queue",
                message="Фоновый процесс обновления запущен; начинается проверка пакета",
                error="",
            )
        except Exception as error:
            write_status(
                "failed",
                job_id=job_id,
                progress=1,
                phase="launch",
                message="Не удалось запустить фоновый процесс обновления",
                error=str(error),
                finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            if isinstance(error, UpdateError):
                raise
            raise UpdateError(str(error)) from error
        return {
            "job_id": job_id,
            "unit": unit,
            "pid": process_id,
            "launcher": launcher,
            "state": "queued",
        }


def installer_command(package_root: Path) -> tuple[list[str], dict[str, str]]:
    installer = package_root / "install.sh"
    if not installer.is_file():
        raise UpdateError("Установщик обновления не найден")
    profile = str(getattr(config, "INSTALL_PROFILE", "full")).strip().lower()
    if profile not in {"full", "lite"}:
        profile = "full"
    environment = dict(os.environ)
    environment["VPN_UPDATE_STATUS_FILE"] = str(status_path())
    version_path = package_root / "VERSION"
    if not version_path.is_file():
        version_path = package_root / "app" / "VERSION"
    environment["VPN_UPDATE_TARGET_VERSION"] = version_path.read_text(encoding="utf-8").strip()
    return ["/bin/bash", str(installer), "--profile", profile, "--update-existing", str(APP_DIR)], environment


def launch_update(package_root: Path, version: str) -> str:
    installer = package_root / "install.sh"
    if not installer.is_file():
        raise UpdateError("Установщик обновления не найден")
    profile = str(getattr(config, "INSTALL_PROFILE", "full")).strip().lower()
    if profile not in {"full", "lite"}:
        profile = "full"
    root = update_dir()
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "update.log"
    wrapper = root / f"apply-{int(time.time())}.sh"
    status = status_path()
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "sleep 3\n"
        f"export VPN_UPDATE_STATUS_FILE={shlex.quote(str(status))}\n"
        f"export VPN_UPDATE_TARGET_VERSION={shlex.quote(version)}\n"
        f"exec /bin/bash {shlex.quote(str(installer))} --profile {shlex.quote(profile)} --update-existing {shlex.quote(str(APP_DIR))}\n",
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o700)
    unit = f"vpn-service-update-{int(time.time())}"
    write_status("scheduled", version=version, unit=unit, log=str(log_path))

    try:
        launch_detached(
            unit,
            ["/bin/bash", str(wrapper)],
            description=f"Фоновое обновление VPN Service ({version})",
            working_directory=APP_DIR,
        )
    except DetachedJobError as error:
        raise UpdateError(str(error)) from error
    return unit


def _main() -> int:
    parser = RussianArgumentParser(description="Инструменты проверки пакетов обновления VPN Service")
    parser.add_argument("--verify", metavar="АРХИВ", help="проверить архив релиза без установки")
    parser.add_argument("--json", action="store_true", help="вывести результат в формате JSON")
    args = parser.parse_args()
    if args.verify:
        result = inspect_archive(Path(args.verify))
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"OK {result['version']} {result['sha256']}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
