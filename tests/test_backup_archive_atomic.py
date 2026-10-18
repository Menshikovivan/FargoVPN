import hashlib
import json
import os
import tarfile
from pathlib import Path

import pytest

from backup_archive import write_verified_archive


def _write_metadata(source):
    payload = [path for path in source.rglob("*") if path.is_file() and path.name not in {"manifest.json", "checksums.json"}]
    inventory = sorted(path.relative_to(source).as_posix() for path in payload)
    checksums = {
        path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in payload
    }
    (source / "manifest.json").write_text(json.dumps({"inventory": inventory}), encoding="utf-8")
    (source / "checksums.json").write_text(json.dumps(checksums), encoding="utf-8")


def test_verified_backup_archive_is_published_atomically(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.sql").write_text("backup data", encoding="utf-8")
    _write_metadata(source)
    destination = tmp_path / "backup.tar.gz"

    assert write_verified_archive(source, destination) == destination
    with tarfile.open(destination, "r:gz") as archive:
        assert archive.extractfile("vpn_service_backup/data.sql").read() == b"backup data"
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".backup.tar.gz.*.tmp")) == []


def test_invalid_backup_does_not_replace_archive_or_leave_partial_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    destination = tmp_path / "backup.tar.gz"
    destination.write_bytes(b"previous verified backup")

    with pytest.raises(RuntimeError, match="checksums.json"):
        write_verified_archive(source, destination)

    assert destination.read_bytes() == b"previous verified backup"
    assert list(tmp_path.glob(".backup.tar.gz.*.tmp")) == []


def test_checksum_mismatch_does_not_publish_archive(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.sql").write_text("backup data", encoding="utf-8")
    _write_metadata(source)
    (source / "data.sql").write_text("changed after checksums", encoding="utf-8")
    destination = tmp_path / "backup.tar.gz"
    destination.write_bytes(b"previous verified backup")

    with pytest.raises(RuntimeError, match="Контрольная сумма"):
        write_verified_archive(source, destination)

    assert destination.read_bytes() == b"previous verified backup"
    assert list(tmp_path.glob(".backup.tar.gz.*.tmp")) == []


def test_uninventoried_payload_does_not_publish_archive(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.sql").write_text("backup data", encoding="utf-8")
    _write_metadata(source)
    (source / "unexpected.bin").write_bytes(b"not checksummed")
    destination = tmp_path / "backup.tar.gz"

    with pytest.raises(RuntimeError, match="Инвентарь"):
        write_verified_archive(source, destination)

    assert not destination.exists()
    assert list(tmp_path.glob(".backup.tar.gz.*.tmp")) == []
