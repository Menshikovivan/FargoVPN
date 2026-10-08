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
import base64
import shlex
import shutil
import subprocess
import tempfile
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
_PUBLISH_LOCK = threading.Lock()
_CHECK_CACHE: dict[str, Any] = {"ts": 0.0, "info": None}
PUBLISHER_USERNAME_DIGEST = "1f05e73fbff2ac214ce58f14f1124be37e28b9e72d8a1240dc7d8cb6bf8b8705"


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
        exact_candidates = [
            APP_DIR / f"RELEASE_NOTES_{version}.md",
            APP_DIR.parent / f"RELEASE_NOTES_{version}.md",
        ]
        for exact in exact_candidates:
            if not exact.is_file():
                continue
            try:
                text = exact.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                break
        # Current production packages carry the canonical CHANGELOG.md.
        # Prefer its section for the installed VERSION before falling back to
        # historical release-note files from older installations.
        changelog_path = APP_DIR / "CHANGELOG.md"
        if not text and changelog_path.is_file():
            try:
                changelog_text = changelog_path.read_text(encoding="utf-8").strip()
            except OSError:
                changelog_text = ""
            if changelog_text:
                wanted = str(version or current_version()).strip()
                match = re.search(
                    rf"(?ms)^##\s+{re.escape(wanted)}\s*$\n?(.*?)(?=^##\s+|\Z)",
                    changelog_text,
                )
                if match:
                    text = f"## {wanted}\n\n{match.group(1).strip()}"
                else:
                    text = changelog_text

        if not text:
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


def changelog_history(limit: int = 8, *, exclude_version: str = "") -> list[dict[str, str]]:
    """Return recent local CHANGELOG sections for the updates page."""
    path = APP_DIR / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    matches = list(re.finditer(r"(?ms)^##\s+\[?([0-9A-Za-z._+-]{1,64})\]?[^\n]*\n(.*?)(?=^##\s+|\Z)", text))
    result: list[dict[str, str]] = []
    excluded = str(exclude_version or "").strip()
    for match in matches:
        version = str(match.group(1) or "").strip()
        if not re.fullmatch(r"[0-9A-Za-z._+-]{1,64}", version):
            continue
        if excluded and version_key(version) == version_key(excluded):
            continue
        body = str(match.group(2) or "").strip()
        if not body:
            continue
        result.append({"version": version, "text": f"## {version}\n\n{body}"[:30000]})
        if len(result) >= max(1, min(int(limit), 20)):
            break
    return result


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
                #   app/VERSION + root install.sh + app/install.sh (current format)
                #   VERSION + install.sh + application files at the archive root (legacy format)
                #   <prefix>/app/VERSION + <prefix>/install.sh (packaged legacy format)
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
            legacy_installer = install_name in install_names
            modern_installer = f"{root_prefix + '/' if root_prefix else ''}app/install.sh" in install_names
            if not (legacy_installer or modern_installer):
                raise UpdateError("В архиве не найден install.sh или app/install.sh")
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


def current_release_changelog(text: str, version: str) -> str:
    """Select exactly this release, accepting headings with dates/descriptions."""
    version = str(version or "").strip()
    match = re.search(
        rf"(?ms)^##\s+\[?{re.escape(version)}\]?(?=\s|$)[^\n]*\n?(.*?)(?=^##\s+|\Z)",
        str(text or ""),
    )
    return f"## {version}\n\n{match.group(1).strip()}" if match else ""


def _read_changelog_from_archive(path: Path, version: str) -> str:
    """Read release notes embedded in the release archive.

    The exact release-note convention is retained for legacy packages.
    CHANGELOG.md is the compatibility fallback for packages that only carry
    the cumulative changelog.
    """
    version = str(version or "").strip()
    candidates = [f"RELEASE_NOTES_{version}.md", "RELEASE_NOTES.md"]
    try:
        with tarfile.open(path, "r:gz") as archive:
            files = [member for member in archive.getmembers() if member.isfile()]
            by_name = {member.name.replace("\\", "/").strip("/"): member for member in files}
            selected = None
            for candidate in candidates:
                for name, member in by_name.items():
                    if name == candidate or name.endswith("/" + candidate):
                        selected = member
                        break
                if selected is not None:
                    break
            if selected is not None:
                source = archive.extractfile(selected)
                text = source.read().decode("utf-8", errors="replace").strip() if source else ""
                if text:
                    section = current_release_changelog(text, version)
                    if section:
                        return section[:30_000]
                    is_versioned_file = selected.name.replace("\\", "/").rsplit("/", 1)[-1] == f"RELEASE_NOTES_{version}.md"
                    matching_heading = re.search(rf"(?mi)^#\s+FargoVPN\s+{re.escape(version)}\s*$", text)
                    if (is_versioned_file or matching_heading) and not re.search(r"(?m)^##\s+\[?\d", text):
                        return text[:30_000]

            # Compatibility fallback: extract the matching section from CHANGELOG.md.
            changelog_member = None
            for name, member in by_name.items():
                if name == "CHANGELOG.md" or name.endswith("/CHANGELOG.md"):
                    changelog_member = member
                    break
            if changelog_member is None:
                return ""
            source = archive.extractfile(changelog_member)
            changelog = source.read().decode("utf-8", errors="replace") if source else ""
            if not changelog:
                return ""
            return current_release_changelog(changelog, version)[:30_000]
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
        "github_main_synced",
        "github_main_branch",
        "github_main_commit_sha",
        "github_main_commit_url",
        "github_main_files",
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


def github_configuration_ready() -> bool:
    """Return whether this installation has enough GitHub settings to publish."""
    return bool(
        github_token()
        and str(getattr(config, "GITHUB_REPOSITORY_OWNER", "") or "").strip()
        and github_repo()
        and bool(getattr(config, "GITHUB_MAIN_SYNC_ENABLED", True))
    )


def publisher_enabled() -> bool:
    """Проверяет возможность публикации GitHub с текущей панели.

    Первичная настройка может выполняться из веб-панели без заранее заданной
    publisher identity. После сохранения токена публикация разрешается самой
    панели через её локальную GitHub-конфигурацию. Защищённый publisher digest
    остаётся совместимым дополнительным способом назначения главной панели.
    """
    return is_publisher_username(getattr(config, "WEB_USERNAME", "")) or github_configuration_ready()


def is_publisher_username(username: str) -> bool:
    """Match the protected publisher identity for config and authenticated sessions."""
    username = str(username or "").strip()
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest() if username else ""
    return bool(digest and hmac_compare(digest, PUBLISHER_USERNAME_DIGEST))


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
                "API обновлений не найден на главной панели. Проверенный адрес: "
                f"{endpoint}"
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
        "github_main_synced": bool(metadata.get("github_main_synced", False)),
        "github_main_commit_url": str(metadata.get("github_main_commit_url") or ""),
        "github_main_commit_sha": str(metadata.get("github_main_commit_sha") or ""),
        "github_tag": str(metadata.get("github_tag") or ""),
        "github_release_url": str(metadata.get("github_release_url") or ""),
        "github_release_verified": bool(metadata.get("github_release_verified", False)),
        "github_release_assets": list(metadata.get("github_release_assets") or []),
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


def github_repository_topics() -> list[str]:
    configured = getattr(config, "GITHUB_REPOSITORY_TOPICS", None)
    if isinstance(configured, (list, tuple)):
        topics = [str(item).strip().lower() for item in configured if str(item).strip()]
    else:
        topics = ["fargovpn", "telegram-bot", "vpn", "3x-ui", "xray", "python", "fastapi"]
    result: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        topic = re.sub(r"[^a-z0-9._-]+", "-", topic).strip("-._")
        if not topic or topic in seen:
            continue
        seen.add(topic)
        result.append(topic[:50])
    return result[:20]


def github_repository_description() -> str:
    return str(getattr(
        config,
        "GITHUB_REPOSITORY_DESCRIPTION",
        "Telegram-бот и веб-панель для управления продажей VPN-подписок с интеграцией 3x-ui.",
    ) or "Telegram-бот и веб-панель для управления продажей VPN-подписок с интеграцией 3x-ui.").strip()[:500]


def _github_configure_repository() -> dict[str, Any]:
    """Apply safe repository metadata without making Contents-only tokens unusable."""
    owner = quote(github_owner(), safe="")
    repo = quote(github_repo(), safe="")
    base = f"/repos/{owner}/{repo}"
    warnings: list[str] = []
    response = github_request("PATCH", base, json={
        "description": github_repository_description(),
        "default_branch": "main",
        "has_issues": True,
        "has_discussions": True,
        "delete_branch_on_merge": True,
    })
    if response.status_code >= 400:
        warnings.append(f"repository settings HTTP {response.status_code}: {github_json_error(response)}")
        payload: dict[str, Any] = {}
    else:
        payload = response.json() if isinstance(response.json(), dict) else {}
    topics = github_repository_topics()
    topics_response = github_request("PUT", base + "/topics", json={"names": topics})
    if topics_response.status_code >= 400:
        warnings.append(f"topics HTTP {topics_response.status_code}: {github_json_error(topics_response)}")
    return {
        "repository": f"{github_owner()}/{github_repo()}",
        "default_branch": str(payload.get("default_branch") or ""),
        "topics": topics,
        "issues": bool(payload.get("has_issues")),
        "discussions": bool(payload.get("has_discussions")),
        "configured": not warnings,
        "warnings": warnings,
    }


def _github_create_backup_ref(base_sha: str, version: str) -> str:
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_version = re.sub(r"[^0-9A-Za-z._-]", "-", str(version or "unknown"))
    ref = f"refs/heads/backup/before-v{safe_version}-{suffix}"
    owner = quote(github_owner(), safe="")
    repo = quote(github_repo(), safe="")
    response = github_request("POST", f"/repos/{owner}/{repo}/git/refs", json={"ref": ref, "sha": base_sha})
    if response.status_code >= 400:
        raise UpdateError(f"Не удалось создать backup-ветку GitHub перед публикацией: HTTP {response.status_code}: {github_json_error(response)}")
    return ref.removeprefix("refs/heads/")


def _github_delete_ref(branch: str) -> None:
    owner = quote(github_owner(), safe="")
    repo = quote(github_repo(), safe="")
    response = github_request("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{quote(branch, safe='')}")
    if response.status_code not in {204, 404}:
        raise UpdateError(f"Не удалось удалить временную backup-ветку GitHub {branch}: HTTP {response.status_code}: {github_json_error(response)}")


def _github_restore_main(base_sha: str, failed_sha: str) -> str:
    """Rollback main content without a force-push by creating a reverse commit."""
    owner = quote(github_owner(), safe="")
    repo = quote(github_repo(), safe="")
    base_commit = github_request("GET", f"/repos/{owner}/{repo}/git/commits/{quote(base_sha, safe='')}")
    if base_commit.status_code >= 400:
        raise UpdateError(f"Не удалось получить исходное дерево main для rollback: HTTP {base_commit.status_code}: {github_json_error(base_commit)}")
    base_tree = str(((base_commit.json().get("tree") or {}).get("sha")) or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", base_tree):
        raise UpdateError("GitHub не вернул корректное дерево исходного main для rollback")
    rollback_commit = github_request(
        "POST",
        f"/repos/{owner}/{repo}/git/commits",
        json={"message": "Rollback failed FargoVPN release", "tree": base_tree, "parents": [failed_sha]},
    )
    if rollback_commit.status_code >= 400:
        raise UpdateError(f"Не удалось создать rollback commit: HTTP {rollback_commit.status_code}: {github_json_error(rollback_commit)}")
    rollback_sha = str(rollback_commit.json().get("sha") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", rollback_sha):
        raise UpdateError("GitHub вернул некорректный rollback commit SHA")
    current = github_request("GET", f"/repos/{owner}/{repo}/git/ref/heads/main")
    current_sha = str(((current.json().get("object") or {}).get("sha")) or "") if current.status_code < 400 else ""
    if current_sha != failed_sha:
        raise UpdateError("Rollback остановлен: ветка main уже изменилась после неудачной публикации")
    update_ref = github_request("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/main", json={"sha": rollback_sha, "force": False})
    if update_ref.status_code >= 400:
        raise UpdateError(f"Не удалось вернуть main к исходному содержимому: HTTP {update_ref.status_code}: {github_json_error(update_ref)}")
    return rollback_sha


def github_release_tag(version: str) -> str:
    # New public releases always use standard SemVer tags. Legacy releases such
    # as FargoVPN-5.1.2 remain readable through the fallback version parser.
    return f"v{version}"


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
    return f"VPN_Service_Platform_{version}_FULL.tar.gz"


def sanitize_public_release_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"https?://[^\s)\]}>]+", "[ссылка скрыта]", value, flags=re.I)
    value = re.sub(r"\b(?:Bearer|token|pat|api[-_ ]?key|bot[_-]?token)\s*[:=]\s*[^\s]+", "[секрет скрыт]", value, flags=re.I)
    value = re.sub(r"@[A-Za-z0-9_]{5,}", "[telegram_username]", value)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[ip скрыт]", value)
    return value[:30000]


def github_notes(changelog: str, version: str, checksum: str, size: int) -> str:
    safe_version = str(version or "").strip()
    raw = current_release_changelog(str(changelog or ""), safe_version)
    if not raw and str(changelog or "").strip():
        raw = "Изменения для текущей версии не найдены в CHANGELOG.md."
    heading = f"# FargoVPN {safe_version}" if safe_version else "# FargoVPN"
    sanitized = sanitize_public_release_text(raw)
    # Release notes must never contain the cumulative history.
    if sanitized and re.search(r"(?m)^##\s+\[?\d", sanitized):
        sanitized = current_release_changelog(sanitized, safe_version)
    install_cmd = github_install_command()
    block = [
        heading,
        sanitized,
        "",
        "### Установка",
        "```bash",
        install_cmd,
        "```",
        "",
        "### Обновление",
        "Проверьте доступную версию во вкладке «Обновления» веб-панели и запустите установку новой версии после проверки архива и SHA-256.",
        "",
        "### Метаданные",
        f"Версия: `{safe_version}`",
        f"SHA-256: `{checksum}`",
        f"Размер архива: `{size}` байт",
    ]
    return "\n".join(item for item in block if item != "")[:100000]


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
        tag_value = str(payload.get("tag_name") or "")
        prefix_value = str(getattr(config, "GITHUB_RELEASE_TAG_PREFIX", "v") or "v")
        release_version = tag_value[len(prefix_value):] if prefix_value and tag_value.startswith(prefix_value) else tag_value
        expected_asset = Path(configured_name.format(version=release_version)).name
        for item in assets:
            if str(item.get("name") or "") == expected_asset:
                asset = item
                break
    if asset is None:
        candidates = [item for item in assets if str(item.get("name") or "").endswith(".tar.gz")]
        if candidates:
            asset = sorted(candidates, key=lambda item: int(item.get("size") or 0), reverse=True)[0]
    if not isinstance(asset, dict):
        raise UpdateError("Последний GitHub Release не содержит .tar.gz архива")
    tag = str(payload.get("tag_name") or "").strip()
    prefix = str(getattr(config, "GITHUB_RELEASE_TAG_PREFIX", "v") or "v").strip()
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
    prefix = str(getattr(config, "GITHUB_RELEASE_TAG_PREFIX", "v") or "v").strip()
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


def github_main_sync_enabled() -> bool:
    return bool(getattr(config, "GITHUB_MAIN_SYNC_ENABLED", True))


def github_install_command() -> str:
    repo_raw = f"https://raw.githubusercontent.com/{github_owner()}/{github_repo()}/main"
    return f"curl -fsSL {repo_raw}/install.sh | sudo bash"


def _github_main_bootstrap() -> str:
    """Generate the public one-command bootstrap installer committed to main."""
    repo_raw = f"https://raw.githubusercontent.com/{github_owner()}/{github_repo()}/main"
    release_base = f"https://github.com/{github_owner()}/{github_repo()}/releases/latest/download"
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail

REPO_RAW="{repo_raw}"
RELEASE_BASE="{release_base}"
ARCHIVE_NAME="FargoVPN_FULL.tar.gz"
ARCHIVE_URL="${{FARGOVPN_ARCHIVE_URL:-$RELEASE_BASE/$ARCHIVE_NAME}}"
CHECKSUM_URL="${{FARGOVPN_CHECKSUM_URL:-$RELEASE_BASE/$ARCHIVE_NAME.sha256}}"
TMP_BASE="${{FARGOVPN_BOOTSTRAP_TMPDIR:-/var/tmp}}"

if [[ ${{EUID:-$(id -u)}} -ne 0 ]]; then
  echo "Запустите установщик через sudo или от пользователя root." >&2
  exit 1
fi

mkdir -p "$TMP_BASE"

download() {{
  local url="$1" destination="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 20 --max-time 600 -o "$destination" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --https-only --tries=3 --timeout=30 -O "$destination" "$url"
  else
    echo "Не найдены curl и wget. Устанавливаю curl и ca-certificates..." >&2
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends curl ca-certificates
    curl -fsSL --retry 3 --connect-timeout 20 --max-time 600 -o "$destination" "$url"
  fi
}}

missing_packages=()
if ! command -v tar >/dev/null 2>&1; then missing_packages+=(tar); fi
if ! command -v sha256sum >/dev/null 2>&1; then missing_packages+=(coreutils); fi
if (( ${{#missing_packages[@]}} )); then
  echo "[FargoVPN bootstrap] Устанавливаю недостающие системные зависимости: ${{missing_packages[*]}}..." >&2
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends "${{missing_packages[@]}}"
fi
command -v tar >/dev/null 2>&1 || {{ echo "Не удалось установить tar." >&2; exit 1; }}
command -v sha256sum >/dev/null 2>&1 || {{ echo "Не удалось установить sha256sum." >&2; exit 1; }}

choose_tmp_base() {{
  local candidate free
  local candidates=("$TMP_BASE" "${{TMPDIR:-}}" "/var/tmp" "/tmp" "/root/.cache")
  for candidate in "${{candidates[@]}}"; do
    [[ -n "$candidate" ]] || continue
    mkdir -p "$candidate" 2>/dev/null || continue
    free=$(df -Pk -- "$candidate" 2>/dev/null | awk 'NR==2 {{print $4}}')
    if [[ "$free" =~ ^[0-9]+$ ]] && (( free >= 262144 )); then
      TMP_BASE="$candidate"
      echo "[FargoVPN bootstrap] Временные файлы: $TMP_BASE (свободно около $((free / 1024)) МБ)"
      return 0
    fi
  done
  echo "Не найден доступный временный каталог минимум с 256 МБ свободного места." >&2
  echo "Освободите место или задайте FARGOVPN_BOOTSTRAP_TMPDIR на подходящем разделе." >&2
  return 1
}}
choose_tmp_base

TMP="$(mktemp -d "$TMP_BASE/fargovpn-bootstrap.XXXXXX")" || {{
  echo "Не удалось создать временный каталог для FargoVPN в $TMP_BASE." >&2
  exit 1
}}
cleanup() {{ rm -rf -- "$TMP"; }}
trap cleanup EXIT

ARCHIVE="$TMP/$ARCHIVE_NAME"
SUMFILE="$TMP/$ARCHIVE_NAME.sha256"

echo "[FargoVPN bootstrap] Получение актуального полного пакета..."
download "$ARCHIVE_URL" "$ARCHIVE" || {{ echo "Не удалось скачать полный пакет FargoVPN." >&2; exit 1; }}
download "$CHECKSUM_URL" "$SUMFILE" || {{ echo "Не удалось скачать SHA-256 полного пакета FargoVPN." >&2; exit 1; }}

echo "[FargoVPN bootstrap] Проверка SHA-256..."
( cd "$TMP" && sha256sum -c "$(basename "$SUMFILE")" ) || {{ echo "Проверка SHA-256 не пройдена; установка остановлена." >&2; exit 1; }}

echo "[FargoVPN bootstrap] Проверка структуры архива..."
tar -tzf "$ARCHIVE" >/dev/null || {{ echo "Архив FargoVPN повреждён или имеет неверный формат." >&2; exit 1; }}
TOP_DIRS=( $(tar -tzf "$ARCHIVE" | awk -F/ 'NF {{print $1}}' | sort -u) )
if [[ ${{#TOP_DIRS[@]}} -ne 1 ]]; then
  echo "Не удалось определить единственный корневой каталог полного пакета." >&2
  exit 1
fi
PACKAGE_ROOT="$TMP/${{TOP_DIRS[0]}}"
tar -xzf "$ARCHIVE" -C "$TMP" || {{ echo "Не удалось распаковать полный пакет FargoVPN во временный каталог." >&2; exit 1; }}

INSTALLER="$PACKAGE_ROOT/app/install.sh"
VERSION_FILE="$PACKAGE_ROOT/app/VERSION"
[[ -f "$INSTALLER" && -f "$VERSION_FILE" ]] || {{ echo "В полном пакете не найден app/install.sh или app/VERSION." >&2; exit 1; }}
VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
echo "[FargoVPN bootstrap] Версия полного пакета: $VERSION"
echo "[FargoVPN bootstrap] Запуск штатного установщика..."

set +e
/bin/bash "$INSTALLER" "$@"
STATUS=$?
set -e
exit "$STATUS"
"""


def _github_main_public_files(archive_root: Path, archive_path: Path, version: str, checksum: str) -> dict[str, bytes]:
    """Build the public main tree from the uploaded release archive.

    The publisher is intentionally the single source of truth: uploading a
    release from the Updates tab must immediately synchronize the complete safe
    project tree, while never copying runtime secrets, databases, logs, caches
    or release archives into the public branch.
    """
    files: dict[str, bytes] = {}
    forbidden_names = {"config.py", ".env", ".env.local", ".env.production"}
    forbidden_dirs = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
    forbidden_suffixes = (".db", ".sqlite", ".sqlite3", ".log", ".pyc", ".pyo", ".tmp", ".bak", ".pem", ".key", ".dump")
    for source in archive_root.rglob("*"):
        if not source.is_file():
            continue
        rel = source.relative_to(archive_root)
        parts = rel.parts
        if any(part in forbidden_dirs for part in parts):
            continue
        name = source.name
        lower = name.lower()
        if lower in forbidden_names or lower.endswith(forbidden_suffixes):
            continue
        if lower.startswith("config.py.") or (lower.startswith(".env.") and lower != ".env.example"):
            continue
        if lower.endswith((".tar.gz", ".tgz", ".zip")):
            continue
        if name.startswith("vpn_service_update_"):
            continue
        # Do not publish local test output generated by a developer outside the
        # source tree unless it is explicitly a markdown report shipped in the release.
        files[str(rel).replace(os.sep, "/")] = source.read_bytes()

    version_file = archive_root / "VERSION"
    if not version_file.is_file():
        version_file = archive_root / "app" / "VERSION"
    if not version_file.is_file():
        raise UpdateError("Для синхронизации main не найден VERSION/app/VERSION")
    # Keep the release archive structure in main, except that the public root
    # install.sh is always the generated bootstrap rather than the full installer.
    files["install.sh"] = _github_main_bootstrap().encode("utf-8")
    # A root VERSION is intentionally not created: version metadata lives in app/VERSION.
    files.pop("VERSION", None)
    return files


def _github_main_tree_state(owner: str, repo: str, commit_sha: str) -> tuple[str, set[str]]:
    """Return current commit tree SHA and all non-directory paths in it."""
    commit = github_request("GET", f"/repos/{owner}/{repo}/git/commits/{quote(commit_sha, safe='')}")
    if commit.status_code >= 400:
        raise UpdateError(f"Не удалось получить дерево текущего main: HTTP {commit.status_code}: {github_json_error(commit)}")
    tree_sha = str(((commit.json().get("tree") or {}).get("sha")) or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
        raise UpdateError("GitHub вернул некорректный SHA дерева текущего main")
    tree = github_request("GET", f"/repos/{owner}/{repo}/git/trees/{quote(tree_sha, safe='')}", params={"recursive": "1"})
    if tree.status_code >= 400:
        raise UpdateError(f"Не удалось прочитать дерево текущего main: HTTP {tree.status_code}: {github_json_error(tree)}")
    payload = tree.json() if isinstance(tree.json(), dict) else {}
    if payload.get("truncated"):
        raise UpdateError("GitHub вернул усечённое дерево main; публикация остановлена, чтобы не удалить файлы неверно")
    paths = {
        str(item.get("path") or "").strip("/")
        for item in (payload.get("tree") or [])
        if isinstance(item, dict)
        and str(item.get("type") or "") in {"blob", "commit"}
        and str(item.get("path") or "").strip("/")
    }
    return tree_sha, paths


def _verify_github_main_tree(owner: str, repo: str, commit_sha: str, expected_paths: set[str]) -> None:
    """Fail closed unless the published main contains exactly the expected file set."""
    _tree_sha, actual_paths = _github_main_tree_state(owner, repo, commit_sha)
    missing = sorted(expected_paths - actual_paths)
    unexpected = sorted(actual_paths - expected_paths)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("отсутствуют: " + ", ".join(missing[:20]))
        if unexpected:
            details.append("лишние: " + ", ".join(unexpected[:20]))
        raise UpdateError("GitHub main после публикации не совпадает с ожидаемым деревом: " + "; ".join(details))


def _github_main_sync(source_archive: Path, version: str, checksum: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Replace public main with one commit built from an uploaded release archive."""
    with tempfile.TemporaryDirectory(prefix="fargovpn-main-sync-") as temp_dir:
        root = safe_extract(source_archive, Path(temp_dir))
        public_files = _github_main_public_files(root, source_archive, version, checksum)
    return _github_main_sync_public_files(public_files, version, progress)


def _github_main_sync_directory(source_root: Path, version: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Repair/synchronize main directly from a package/repository root directory."""
    root = source_root.resolve()
    version_file = root / "VERSION"
    if not version_file.is_file():
        version_file = root / "app" / "VERSION"
    if not root.is_dir() or not version_file.is_file():
        raise UpdateError(f"Не найдено исходное дерево для синхронизации main: {root}")
    public_files = _github_main_public_files(root, root / "release-source-placeholder.tar.gz", version, "")
    return _github_main_sync_public_files(public_files, version, progress)


def _github_main_sync_public_files(public_files: dict[str, bytes], version: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Publish an exact public file map to main with a safety ref and rollback."""
    branch = str(getattr(config, "GITHUB_TARGET_BRANCH", "main") or "main").strip() or "main"
    if branch != "main":
        raise UpdateError("Автосинхронизация публичного main требует GITHUB_TARGET_BRANCH=main")
    if not github_main_sync_enabled():
        raise UpdateError("Публикация требует синхронизации main: включите GITHUB_MAIN_SYNC_ENABLED")
    owner = quote(github_owner(), safe="")
    repo = quote(github_repo(), safe="")
    ref_path = f"/repos/{owner}/{repo}/git/ref/heads/{quote(branch, safe='')}"
    update_ref_path = f"/repos/{owner}/{repo}/git/refs/heads/{quote(branch, safe='')}"
    ref = github_request("GET", ref_path)
    if ref.status_code >= 400:
        raise UpdateError(f"Не удалось получить ветку {branch}: HTTP {ref.status_code}: {github_json_error(ref)}")
    base_sha = str(((ref.json().get("object") or {}).get("sha")) or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise UpdateError(f"GitHub вернул некорректный SHA ветки {branch}")

    base_tree_sha, current_paths = _github_main_tree_state(owner, repo, base_sha)
    expected_paths = set(public_files)
    stale_paths = sorted(current_paths - expected_paths)

    backup_ref = _github_create_backup_ref(base_sha, version)
    applied = False
    commit_sha = ""
    try:
        entries: list[dict[str, Any]] = []
        total_files = max(1, len(public_files))
        for index, (path, data) in enumerate(sorted(public_files.items()), start=1):
            blob = github_request(
                "POST", f"/repos/{owner}/{repo}/git/blobs",
                json={"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
            )
            if blob.status_code >= 400:
                raise UpdateError(f"Не удалось создать Git blob для {path}: HTTP {blob.status_code}: {github_json_error(blob)}")
            blob_sha = str((blob.json() or {}).get("sha") or "").strip()
            if not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
                raise UpdateError(f"GitHub вернул некорректный blob SHA для {path}")
            entries.append({"path": path, "mode": "100755" if path.endswith(".sh") else "100644", "type": "blob", "sha": blob_sha})
            if progress:
                progress(f"{index}/{total_files}::{path}")

        # IMPORTANT: build the new tree from scratch. Do not inherit base_tree.
        # The expected file set is the complete public repository surface, so
        # omitted paths are physically absent from the new main tree. This is
        # the strongest possible guarantee against stale files surviving in main.
        tree = github_request("POST", f"/repos/{owner}/{repo}/git/trees", json={"tree": entries})
        if tree.status_code >= 400:
            raise UpdateError(f"Не удалось собрать дерево main: HTTP {tree.status_code}: {github_json_error(tree)}")
        tree_sha = str((tree.json() or {}).get("sha") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
            raise UpdateError("GitHub вернул некорректный SHA дерева main")

        commit = github_request(
            "POST", f"/repos/{owner}/{repo}/git/commits",
            json={"message": f"Release {version}", "tree": tree_sha, "parents": [base_sha]},
        )
        if commit.status_code >= 400:
            raise UpdateError(f"Не удалось создать commit main: HTTP {commit.status_code}: {github_json_error(commit)}")
        commit_payload = commit.json()
        commit_sha = str(commit_payload.get("sha") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise UpdateError("GitHub вернул некорректный SHA commit main")

        latest_ref = github_request("GET", ref_path)
        if latest_ref.status_code >= 400:
            raise UpdateError(f"Не удалось повторно проверить ветку {branch}: HTTP {latest_ref.status_code}: {github_json_error(latest_ref)}")
        latest_sha = str(((latest_ref.json().get("object") or {}).get("sha")) or "").strip()
        if latest_sha != base_sha:
            raise UpdateError("Ветка main изменилась во время публикации; новый commit не применён")

        update_ref = github_request("PATCH", update_ref_path, json={"sha": commit_sha, "force": False})
        if update_ref.status_code >= 400:
            raise UpdateError(f"Не удалось обновить ветку {branch}: HTTP {update_ref.status_code}: {github_json_error(update_ref)}")
        applied = True
        if str((update_ref.json().get("object") or {}).get("sha") or "") != commit_sha:
            raise UpdateError("GitHub не подтвердил SHA опубликованного commit main")

        _verify_github_main_tree(owner, repo, commit_sha, expected_paths)
        return {
            "enabled": True,
            "synced": True,
            "branch": branch,
            "commit_sha": commit_sha,
            "base_sha": base_sha,
            "backup_ref": backup_ref,
            "stale_paths_removed": stale_paths,
            "stale_count": len(stale_paths),
            "commit_url": str(commit_payload.get("html_url") or f"https://github.com/{github_owner()}/{github_repo()}/commit/{commit_sha}"),
            "files": sorted(public_files),
        }
    except Exception as error:
        if applied and commit_sha:
            try:
                _github_restore_main(base_sha, commit_sha)
            except Exception as rollback_error:
                raise UpdateError(f"Ошибка публикации main: {error}; автоматический rollback main не завершился: {rollback_error}") from rollback_error
        try:
            _github_delete_ref(backup_ref)
        except Exception:
            LOGGER.warning("Не удалось удалить backup-ветку GitHub %s после сбоя синхронизации main", backup_ref, exc_info=True)
        raise
    branch = str(getattr(config, "GITHUB_TARGET_BRANCH", "main") or "main").strip() or "main"
    if branch != "main":
        raise UpdateError("Автосинхронизация публичного main требует GITHUB_TARGET_BRANCH=main")
    if not github_main_sync_enabled():
        raise UpdateError("Публикация требует синхронизации main: включите GITHUB_MAIN_SYNC_ENABLED")


def _release_tag_sha(tag: str) -> str | None:
    base = f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}"
    response = github_request("GET", base + "/git/ref/tags/" + quote(tag, safe=''))
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise UpdateError(f"Не удалось проверить тег: HTTP {response.status_code}: {github_json_error(response)}")
    obj = response.json().get("object") or {}
    for _ in range(5):
        if obj.get("type") == "commit" and obj.get("sha"):
            return str(obj["sha"])
        if obj.get("type") != "tag" or not obj.get("sha"):
            break
        nested = github_request("GET", base + "/git/tags/" + quote(str(obj['sha']), safe=''))
        if nested.status_code >= 400:
            raise UpdateError(f"Не удалось прочитать annotated tag: HTTP {nested.status_code}")
        obj = nested.json().get("object") or {}
    raise UpdateError("GitHub не вернул коммит для тега релиза")


def _verify_release_tag(tag: str, commit_sha: str) -> None:
    actual = _release_tag_sha(tag)
    if actual != commit_sha:
        raise UpdateError(f"Тег {tag} не совпадает с коммитом выпуска: {actual or 'отсутствует'} != {commit_sha}")


def _prepare_release_tag(tag: str, commit_sha: str) -> None:
    actual = _release_tag_sha(tag)
    if actual and actual != commit_sha:
        raise UpdateError(f"Тег {tag} уже указывает на другой коммит. Выберите новую версию; существующий тег не перезаписывается. main уже обновлён до {commit_sha}.")
    if actual is None:
        base = f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}"
        response = github_request("POST", base + "/git/refs", json={"ref": "refs/tags/" + tag, "sha": commit_sha})
        if response.status_code >= 400:
            raise UpdateError(f"main обновлён, но тег не создан: HTTP {response.status_code}: {github_json_error(response)}")
    _verify_release_tag(tag, commit_sha)


def _verify_published_release(release_id: int, tag: str, commit_sha: str, expected_assets: list[str], expected_archive_size: int, expected_checksum: str) -> dict[str, Any]:
    """Verify the final GitHub Release and all uploaded assets through the API."""
    owner = quote(github_owner(), safe="")
    repo = quote(github_repo(), safe="")
    response = github_request("GET", f"/repos/{owner}/{repo}/releases/{release_id}")
    if response.status_code >= 400:
        raise UpdateError(f"Не удалось подтвердить GitHub Release: HTTP {response.status_code}: {github_json_error(response)}")
    payload = response.json()
    if str(payload.get("tag_name") or "") != tag:
        raise UpdateError("GitHub Release вернул другой tag")
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    by_name = {str(item.get("name") or ""): item for item in assets if isinstance(item, dict)}
    missing = [name for name in expected_assets if name not in by_name]
    if missing:
        raise UpdateError("GitHub Release не содержит ожидаемые assets: " + ", ".join(missing))
    archive_item = by_name.get(expected_assets[0], {})
    if int(archive_item.get("size") or 0) != int(expected_archive_size):
        raise UpdateError("GitHub Release подтвердил неверный размер versioned archive")
    digest = str(archive_item.get("digest") or "").lower()
    if digest and digest != "sha256:" + expected_checksum.lower():
        raise UpdateError("GitHub Release вернул несовпадающий SHA-256 versioned archive")
    return {
        "release_id": int(payload.get("id") or release_id),
        "release_url": str(payload.get("html_url") or ""),
        "tag": tag,
        "commit_sha": commit_sha,
        "assets": sorted(by_name),
    }


PUBLISH_BUSY_STATES = {"queued", "validating", "syncing-main", "creating-release", "uploading-assets", "verifying", "completed", "failed"}

def publish_status_path() -> Path:
    return update_dir() / "publish" / "status.json"

def _publish_status_is_fresh(status: dict[str, Any] | None = None) -> bool:
    current = status if isinstance(status, dict) else read_publish_status()
    updated = str(current.get("updated_at") or "").strip()
    if not updated:
        return False
    try:
        stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() <= max(900, int(getattr(config, "UPDATE_STALE_JOB_SECONDS", 7200)))
    except (TypeError, ValueError):
        return False

def write_publish_status(state: str, **details: Any) -> None:
    path = publish_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _STATUS_LOCK, lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        previous: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except Exception:
            pass
        progress = details.get("progress", previous.get("progress", 0))
        try:
            progress = max(0, min(100, int(progress)))
        except (TypeError, ValueError):
            progress = 0
        data = {
            **previous,
            "state": str(state),
            "progress": progress,
            "revision": int(previous.get("revision") or 0) + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **details,
        }
        temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(temp, 0o600)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

def read_publish_status() -> dict[str, Any]:
    try:
        return json.loads(publish_status_path().read_text(encoding="utf-8"))
    except Exception:
        return {}

def publish_job_busy(status: dict[str, Any] | None = None) -> bool:
    current = status if isinstance(status, dict) else read_publish_status()
    return str(current.get("state") or "") in {"queued", "validating", "syncing-main", "creating-release", "uploading-assets", "verifying"} and _publish_status_is_fresh(current)

def publish_job_manifest_path(job_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", str(job_id))[:120]
    return update_dir() / "publish" / "jobs" / f"{safe}.json"

def write_publish_job_manifest(job_id: str, actor: str, archive: Path, original_name: str, version: str) -> Path:
    path = publish_job_manifest_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_id, "actor": str(actor)[:100], "archive": str(archive.resolve()),
        "original_name": Path(original_name).name, "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)
    return path

def read_publish_job_manifest(job_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(publish_job_manifest_path(job_id).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}

def start_publish_job(source: Path, original_name: str, actor: str = "web") -> dict[str, Any]:
    info = inspect_archive(source)
    if not github_token():
        raise UpdateError("GitHub не настроен: откройте «Настройки GitHub» и сохраните Personal Access Token")
    if not str(getattr(config, "GITHUB_REPOSITORY_OWNER", "") or "").strip():
        raise UpdateError("GitHub не настроен: укажите владельца репозитория в «Настройках GitHub»")
    if str(getattr(config, "GITHUB_REPOSITORY_NAME", "FargoVPN") or "FargoVPN").strip() == "":
        raise UpdateError("GitHub не настроен: укажите имя репозитория")
    if str(getattr(config, "GITHUB_TARGET_BRANCH", "main") or "main").strip() != "main":
        raise UpdateError("Публикация FargoVPN выполняется только в ветку main")
    if not github_main_sync_enabled():
        raise UpdateError("GitHub: включите синхронизацию main в «Настройках GitHub»")
    if publish_job_busy():
        raise UpdateError("Другая публикация GitHub уже выполняется")
    # Fail before queueing if the token cannot read the repository or lacks push access.
    # This makes configuration problems visible in the upload response instead of
    # producing a background job that can only fail later.
    github_validate_configuration()
    root = update_dir() / "publish" / "pending"
    root.mkdir(parents=True, exist_ok=True)
    job_id = f"pub-{int(time.time())}-{secrets.token_hex(4)}"
    target = root / f"{job_id}.tar.gz"
    shutil.copy2(source, target)
    os.chmod(target, 0o600)
    write_publish_job_manifest(job_id, actor, target, original_name, str(info["version"]))
    write_publish_status(
        "queued", job_id=job_id, version=str(info["version"]), actor=str(actor)[:100], progress=1,
        phase="queue", message="Архив принят; публикация GitHub поставлена в очередь", error="",
        finished_at="", github_tag="", github_release_url="", github_main_commit_sha="",
    )
    python = APP_DIR / ".venv" / "bin" / "python"
    if not python.is_file():
        python = Path(os.sys.executable)
    worker = APP_DIR / "publish_worker.py"
    unit = f"vpn-service-publish-worker-{int(time.time())}-{secrets.token_hex(2)}"
    try:
        from detached_jobs import DetachedJobError, launch_detached
        launcher_log = update_dir() / "publish" / "launcher.log"
        launcher = launch_detached(
            unit,
            [str(python), str(worker), "--job-id", job_id],
            description=f"Публикация FargoVPN {info['version']} в GitHub",
            working_directory=APP_DIR,
            output_path=launcher_log,
        )
    except DetachedJobError as error:
        target.unlink(missing_ok=True)
        write_publish_status("failed", job_id=job_id, version=str(info["version"]), progress=1, phase="launch", message="Не удалось запустить публикацию", error=str(error), finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        raise UpdateError(str(error)) from error
    write_publish_status("queued", job_id=job_id, version=str(info["version"]), actor=str(actor)[:100], progress=2, phase="queue", message="Фоновая публикация GitHub запущена; ожидается подтверждение worker", launcher=launcher, launcher_log=str(launcher_log), unit=unit)
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        current = read_publish_status()
        if str(current.get("job_id") or "") == job_id:
            if str(current.get("state") or "") == "failed":
                raise UpdateError(str(current.get("error") or "GitHub publisher worker завершился с ошибкой"))
            if int(current.get("progress") or 0) >= 3:
                break
        time.sleep(0.1)
    else:
        raise UpdateError(f"GitHub publisher worker не подтвердил запуск в течение 6 с; журнал запуска: {launcher_log}")
    return {"job_id": job_id, "version": str(info["version"]), "state": "queued", "launcher": launcher, "launcher_log": str(launcher_log), "unit": unit}


def publish_update(source: Path, original_name: str = "update.tar.gz", progress: Callable[[int, str, str], None] | None = None) -> dict[str, Any]:
    if not _PUBLISH_LOCK.acquire(blocking=False):
        raise UpdateError("Другая публикация GitHub уже выполняется")
    try:
        root = update_dir()
        root.mkdir(parents=True, exist_ok=True)
        with (root / "publish.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise UpdateError("Другая публикация GitHub уже выполняется") from error
            return _publish_update_locked(source, original_name, progress)
    finally:
        _PUBLISH_LOCK.release()


def _publish_update_locked(source: Path, original_name: str = "update.tar.gz", progress: Callable[[int, str, str], None] | None = None) -> dict[str, Any]:
    def report(percent: int, phase: str, message: str) -> None:
        if progress:
            progress(max(0, min(100, int(percent))), phase, message)
    report(4, "validating", "Проверяется архив обновления")
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
    tag = github_release_tag(version)
    release_name = github_release_name(version)
    body = github_notes(changelog, version, checksum, target.stat().st_size)
    endpoint = f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases/tags/{quote(tag, safe='')}"

    if _release_tag_sha(tag):
        raise UpdateError(f"Тег {tag} уже существует. Публикация остановлена; версия релиза должна быть новой.")

    report(10, "validating", "Архив проверен; подготавливаются данные GitHub")
    repo_settings = _github_configure_repository()
    report(15, "syncing-main", "Синхронизируется публичный main")
    def main_progress(detail: str) -> None:
        # The sync callback carries a stable file counter, so the UI can show real progress.
        head, _, path = str(detail).partition("::")
        try:
            current, total = [int(item) for item in head.split("/", 1)]
        except (TypeError, ValueError):
            current, total = 1, 1
        report(15 + int(min(33, current / max(1, total) * 33)), "syncing-main", f"GitHub main: {current}/{max(1,total)} файлов · {path}")
    setattr(_publish_update_locked, "_main_seen", 0)
    main_sync = _github_main_sync(target, version, checksum, progress=main_progress)
    if not main_sync.get("synced") or not main_sync.get("commit_sha"):
        raise UpdateError("GitHub не подтвердил коммит main; релиз не создавался")

    release_id = 0
    release: dict[str, Any] = {}
    try:
        report(50, "creating-release", f"Создаётся GitHub Release {tag}")
        _prepare_release_tag(tag, str(main_sync["commit_sha"]))
        existing = github_request("GET", endpoint)
        draft = bool(getattr(config, "GITHUB_RELEASE_DRAFT", False))
        prerelease = bool(getattr(config, "GITHUB_RELEASE_PRERELEASE", False))
        make_latest = bool(getattr(config, "GITHUB_RELEASE_MAKE_LATEST", True)) and not draft and not prerelease
        payload = {
            "tag_name": tag,
            "target_commitish": str(main_sync["commit_sha"]),
            "name": release_name,
            "body": body,
            "draft": draft,
            "prerelease": prerelease,
            "make_latest": "true" if make_latest else "false",
        }
        if existing.status_code == 404:
            response = github_request("POST", f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases", json=payload)
            if response.status_code >= 400:
                raise UpdateError(f"Не удалось создать GitHub Release: HTTP {response.status_code}: {github_json_error(response)}")
            release = response.json()
            release_id = int(release.get("id") or 0)
        elif existing.status_code == 200:
            raise UpdateError(f"GitHub Release для {tag} уже существует; версия должна публиковаться один раз")
        else:
            raise UpdateError(f"GitHub вернул HTTP {existing.status_code}: {github_json_error(existing)}")

        upload_url = str(release.get("upload_url") or "").replace("{?name,label}", "")
        if not upload_url:
            raise UpdateError("GitHub не вернул upload_url для Release")
        upload_url = upload_url.split("{", 1)[0].rstrip("?")
        payload_bytes = target.read_bytes()
        generic_asset = "FargoVPN_FULL.tar.gz"
        checksum_line = f"{checksum}  {asset_name}\n"
        generic_checksum_line = f"{checksum}  {generic_asset}\n"
        assets_payloads = [
            (asset_name, payload_bytes, "application/gzip"),
            (generic_asset, payload_bytes, "application/gzip"),
            (f"{asset_name}.sha256", checksum_line.encode("utf-8"), "text/plain; charset=utf-8"),
            (f"{generic_asset}.sha256", generic_checksum_line.encode("utf-8"), "text/plain; charset=utf-8"),
        ]
        uploaded_assets: list[dict[str, Any]] = []
        report(58, "uploading-assets", f"GitHub: подготовлено {len(assets_payloads)} assets")
        for asset_index, (name, data, content_type) in enumerate(assets_payloads, start=1):
            report(58 + int((asset_index - 1) * 8), "uploading-assets", f"GitHub: загружается asset {asset_index}/{len(assets_payloads)} — {name}")
            headers = {**github_headers(), "Content-Type": content_type, "Content-Length": str(len(data))}
            try:
                upload = httpx.post(upload_url, params={"name": name}, headers=headers, content=data, timeout=600.0, follow_redirects=False)
            except httpx.RequestError as error:
                raise UpdateError(f"Не удалось подключиться к GitHub при загрузке asset {name}: {error}") from error
            if upload.status_code >= 400:
                raise UpdateError(f"Не удалось загрузить asset {name} в GitHub Release: HTTP {upload.status_code}: {github_json_error(upload)}")
            uploaded = upload.json()
            if (str(uploaded.get("name") or "") != name or int(uploaded.get("size") or 0) != len(data) or str(uploaded.get("state") or "") != "uploaded"):
                raise UpdateError(f"GitHub не подтвердил размер/состояние asset {name}")
            digest = str(uploaded.get("digest") or "")
            if digest and digest != "sha256:" + hashlib.sha256(data).hexdigest():
                raise UpdateError(f"GitHub вернул несовпадающий digest asset {name}")
            uploaded_assets.append(uploaded)
            report(58 + int(asset_index * 8), "uploading-assets", f"GitHub: asset {asset_index}/{len(assets_payloads)} загружен")

        asset = next(item for item in uploaded_assets if str(item.get("name") or "") == asset_name)
        report(92, "verifying", "Проверяются tag, Release и все assets")
        _verify_release_tag(tag, str(main_sync["commit_sha"]))
        verified_release = _verify_published_release(release_id, tag, str(main_sync["commit_sha"]), [name for name, _, _ in assets_payloads], target.stat().st_size, checksum)

        if main_sync.get("backup_ref"):
            try:
                _github_delete_ref(str(main_sync["backup_ref"]))
            except Exception:
                LOGGER.warning("Не удалось удалить временную backup-ветку GitHub %s после успешного релиза", main_sync.get("backup_ref"), exc_info=True)

        report(98, "verifying", "GitHub подтвердил Release; завершается публикация")
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
            "github_release_id": release_id,
            "github_tag": tag,
            "github_release_url": str(release.get("html_url") or ""),
            "github_asset_url": str(asset.get("browser_download_url") or ""),
            "github_main_synced": True,
            "github_main_branch": str(main_sync.get("branch") or "main"),
            "github_main_commit_sha": str(main_sync.get("commit_sha") or ""),
            "github_main_commit_url": str(main_sync.get("commit_url") or ""),
            "github_main_files": list(main_sync.get("files") or []),
            "github_main_stale_paths_removed": int(main_sync.get("stale_count") or 0),
            "github_repository": repo_settings.get("repository", ""),
            "github_repository_topics": list(repo_settings.get("topics") or []),
            "github_repository_configured": bool(repo_settings.get("configured")),
            "github_repository_warnings": list(repo_settings.get("warnings") or []),
            "github_release_assets": list(verified_release.get("assets") or []),
            "github_release_verified": True,
        }
        temp_meta = _latest_path().with_suffix(".tmp")
        temp_meta.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temp_meta, 0o600)
        temp_meta.replace(_latest_path())
        invalidate_update_cache()
        return metadata
    except Exception as error:
        cleanup_errors: list[str] = []
        if release_id:
            try:
                delete_release = github_request("DELETE", f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/releases/{release_id}")
                if delete_release.status_code not in {204, 404}:
                    cleanup_errors.append(f"release delete HTTP {delete_release.status_code}: {github_json_error(delete_release)}")
            except Exception as cleanup_error:
                cleanup_errors.append(f"release delete: {cleanup_error}")
        try:
            if _release_tag_sha(tag):
                delete_tag = github_request("DELETE", f"/repos/{quote(github_owner(), safe='')}/{quote(github_repo(), safe='')}/git/refs/tags/{quote(tag, safe='')}")
                if delete_tag.status_code not in {204, 404}:
                    cleanup_errors.append(f"tag delete HTTP {delete_tag.status_code}: {github_json_error(delete_tag)}")
        except Exception as cleanup_error:
            cleanup_errors.append(f"tag delete: {cleanup_error}")
        try:
            if main_sync.get("commit_sha") and main_sync.get("base_sha"):
                _github_restore_main(str(main_sync["base_sha"]), str(main_sync["commit_sha"]))
        except Exception as rollback_error:
            cleanup_errors.append(f"main rollback: {rollback_error}")
        try:
            if main_sync.get("backup_ref"):
                _github_delete_ref(str(main_sync["backup_ref"]))
        except Exception as cleanup_error:
            cleanup_errors.append(f"backup ref delete: {cleanup_error}")
        if cleanup_errors:
            raise UpdateError(f"Публикация версии {version} не удалась: {error}; cleanup/rollback: {'; '.join(cleanup_errors)}") from error
        raise


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

def update_launcher_log_path() -> Path:
    return update_dir() / "update-launcher.log"

def _make_update_launcher(job_id: str, command: list[str]) -> Path:
    root = update_dir() / "launchers"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{re.sub(r'[^A-Za-z0-9._-]', '_', job_id)}.sh"
    log_path = update_launcher_log_path()
    quoted = " ".join(shlex.quote(str(item)) for item in command)
    text = (
        "#!/usr/bin/env bash\nset -Eeuo pipefail\n"
        f"mkdir -p {shlex.quote(str(log_path.parent))}\n"
        f"exec >> {shlex.quote(str(log_path))} 2>&1\n"
        f"echo '[FargoVPN update launcher] $(date -Is) job={shlex.quote(job_id)} starting'\n"
        f"exec {quoted}\n"
    )
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o700)
    return path

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
        ]
        launcher = ""
        launcher_error = ""
        process_id = 0
        try:
            launcher_script = _make_update_launcher(job_id, command)
            launcher = launch_detached(
                transient_unit,
                ["/bin/bash", str(launcher_script)],
                description=f"Фоновое обновление VPN Service ({job_id})",
                working_directory=APP_DIR,
            )
            unit = transient_unit
            write_status(
                "queued",
                job_id=job_id,
                unit=unit,
                pid=process_id,
                launcher=launcher,
                progress=2,
                phase="queue",
                message="Фоновый процесс обновления запущен; ожидается подтверждение worker",
                error="",
            )
            # systemd --no-block confirms the unit was queued, not that Python
            # actually started. Wait briefly for the worker's durable ack.
            # The worker writes progress=3 before network/file work begins.
            deadline = time.monotonic() + 6.0
            acknowledged = False
            while time.monotonic() < deadline:
                current_status = read_status()
                if str(current_status.get("job_id") or "") == job_id and int(current_status.get("progress") or 0) >= 3:
                    acknowledged = True
                    break
                if str(current_status.get("state") or "") == "failed" and str(current_status.get("job_id") or "") == job_id:
                    raise UpdateError(str(current_status.get("error") or "Фоновый worker завершился с ошибкой"))
                time.sleep(0.1)
            if not acknowledged:
                launcher_log = update_launcher_log_path()
                raise UpdateError(f"Фоновый worker не подтвердил запуск в течение 6 с; журнал запуска: {launcher_log}")
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


def _package_installer(package_root: Path) -> Path:
    modern = package_root / "app" / "install.sh"
    legacy = package_root / "install.sh"
    if modern.is_file():
        return modern
    if legacy.is_file():
        return legacy
    raise UpdateError("Установщик обновления не найден")


def installer_command(package_root: Path) -> tuple[list[str], dict[str, str]]:
    installer = _package_installer(package_root)
    environment = dict(os.environ)
    environment["VPN_UPDATE_STATUS_FILE"] = str(status_path())
    version_path = package_root / "VERSION"
    if not version_path.is_file():
        version_path = package_root / "app" / "VERSION"
    environment["VPN_UPDATE_TARGET_VERSION"] = version_path.read_text(encoding="utf-8").strip()
    return ["/bin/bash", str(installer), "--update-existing", str(APP_DIR)], environment


def launch_update(package_root: Path, version: str) -> str:
    installer = _package_installer(package_root)
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
        f"exec /bin/bash {shlex.quote(str(installer))} --update-existing {shlex.quote(str(APP_DIR))}\n",
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
    parser.add_argument("--sync-main-directory", metavar="DIR", help="синхронизировать public main из установленного runtime-каталога")
    parser.add_argument("--version", metavar="VERSION", help="версия для --sync-main-directory")
    parser.add_argument("--json", action="store_true", help="вывести результат в формате JSON")
    args = parser.parse_args()
    if args.verify:
        result = inspect_archive(Path(args.verify))
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"OK {result['version']} {result['sha256']}")
        return 0
    if args.sync_main_directory:
        version = str(args.version or current_version()).strip()
        if not github_main_sync_enabled() or not github_token():
            result = {"synced": False, "skipped": True, "reason": "GitHub publisher disabled or token missing"}
        else:
            result = _github_main_sync_directory(Path(args.sync_main_directory), version)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else str(result))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
