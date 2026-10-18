from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path


def _verify_archive_payload(archive: tarfile.TarFile) -> None:
    members = archive.getmembers()
    names: set[str] = set()
    payload: dict[str, tarfile.TarInfo] = {}
    for member in members:
        name = member.name.replace("\\", "/")
        parts = name.split("/")
        if name != "vpn_service_backup" and not name.startswith("vpn_service_backup/"):
            raise RuntimeError(f"Созданный архив содержит путь вне корня резервной копии: {name!r}")
        if any(part in {"", ".", ".."} for part in parts):
            raise RuntimeError(f"Созданный архив содержит небезопасный путь: {name!r}")
        if name in names:
            raise RuntimeError(f"Созданный архив содержит повторный путь: {name!r}")
        names.add(name)
        if member.issym() or member.islnk() or member.isdev() or not (member.isfile() or member.isdir()):
            raise RuntimeError("Созданный архив содержит небезопасные специальные файлы")
        if member.isfile():
            relative = name.removeprefix("vpn_service_backup/")
            payload[relative] = member

    manifest_member = payload.get("manifest.json")
    checksums_member = payload.get("checksums.json")
    if manifest_member is None:
        raise RuntimeError("Созданный архив не содержит manifest.json")
    if checksums_member is None:
        raise RuntimeError("Созданный архив не содержит checksums.json")

    def read_json(member: tarfile.TarInfo, label: str) -> dict:
        try:
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError(f"Созданный архив не содержит данные {label}")
            with stream:
                value = json.load(stream)
        except (UnicodeError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"{label} в созданном архиве повреждён: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"{label} в созданном архиве имеет неверный формат")
        return value

    manifest = read_json(manifest_member, "manifest.json")
    checksums = read_json(checksums_member, "checksums.json")
    actual_payload = set(payload) - {"manifest.json", "checksums.json"}
    inventory = manifest.get("inventory")
    if (
        not isinstance(inventory, list)
        or any(not isinstance(relative, str) for relative in inventory)
        or set(inventory) != actual_payload
        or len(inventory) != len(actual_payload)
    ):
        raise RuntimeError("Инвентарь созданного архива не совпадает с его содержимым")
    if set(checksums) != actual_payload:
        raise RuntimeError("checksums.json созданного архива не содержит полный контрольный список содержимого")

    for relative in actual_payload:
        expected = str(checksums.get(relative) or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RuntimeError(f"Некорректная контрольная сумма в созданном архиве: {relative}")
        member = payload[relative]
        stream = archive.extractfile(member)
        if stream is None:
            raise RuntimeError(f"Не удалось прочитать файл созданного архива: {relative}")
        digest = hashlib.sha256()
        with stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError(f"Контрольная сумма созданного архива не совпадает: {relative}")


def write_verified_archive(source: Path, destination: Path) -> Path:
    destination = Path(destination)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=6) as archive:
            archive.add(source, arcname="vpn_service_backup", recursive=True)

        try:
            with tarfile.open(temporary, "r:gz") as archive:
                members = archive.getmembers()
                total = sum(int(member.size or 0) for member in members if member.isfile())
                if total <= 0:
                    raise RuntimeError("Созданный архив пуст")
                _verify_archive_payload(archive)
        except (tarfile.TarError, OSError) as exc:
            raise RuntimeError(f"Проверка созданного tar.gz не удалась: {exc}") from exc

        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(f"Создание tar.gz не удалось: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
