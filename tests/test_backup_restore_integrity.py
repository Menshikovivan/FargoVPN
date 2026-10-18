import importlib.util
import logging
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
spec = importlib.util.spec_from_file_location("backup_restore_integrity_test", APP_ROOT / "restore_manager.py")
manager = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"fcntl": types.ModuleType("fcntl")}), patch(
    "logging.FileHandler", return_value=logging.NullHandler()
):
    spec.loader.exec_module(manager)


def test_empty_sqlite_xui_database_is_not_a_restorable_backup(tmp_path):
    root = tmp_path / "archive"
    database = root / "databases" / "xui.db"
    database.parent.mkdir(parents=True)
    sqlite3.connect(database).close()

    with pytest.raises(RuntimeError, match="допустимая база 3x-ui"):
        manager.find_xui_artifact(root, {"xui_dump": "databases/xui.db"})


def test_restore_row_counts_compare_staged_data_with_committed_data():
    manager._verify_imported_counts({"users": 2, "payments": 3}, {"users": 2, "payments": 3})

    with pytest.raises(RuntimeError, match="ожидалось 3, получено 2"):
        manager._verify_imported_counts({"users": 2, "payments": 3}, {"users": 2, "payments": 2})


def test_users_only_restore_checks_every_restored_table(monkeypatch):
    counts = {"users": 2, "payments": 3, "referral_rewards": 4}

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            if "to_regclass" in query:
                table = params[0].rsplit(".", 1)[-1]
                return Result((f"public.{table}" if table in counts else None,))
            table = query.rsplit('"', 2)[1]
            return Result((counts[table],))

    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda _dsn: Connection()
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)

    restored_counts = manager.app_counts(
        "postgresql://example.invalid/fargovpn",
        manager.USERS_ONLY_TABLES,
    )

    manager._verify_imported_counts(counts, restored_counts)


def test_full_restore_keeps_runtime_config_and_keys_and_merges_only_allowed_values(tmp_path):
    archive = tmp_path / "archive"
    archived_config = archive / "bot" / "config.py"
    archived_config.parent.mkdir(parents=True)
    archived_config.write_text(
        "PAYMENT_PRICE = 900\nDATABASE_URL = 'archive-db'\nXUI_DB_PATH = '/archive/x-ui.db'\n",
        encoding="utf-8",
    )
    (archive / "system" / "etc-xray").mkdir(parents=True)
    (archive / "system" / "etc-xray" / "config.json").write_text('{"archived": true}', encoding="utf-8")
    (archive / "runtime").mkdir()
    (archive / "runtime" / "vapid_private.pem").write_text("archived-key", encoding="utf-8")

    app = tmp_path / "installed-app"
    app.mkdir()
    active_config = app / "config.py"
    active_config.write_text(
        "PAYMENT_PRICE = 100\nDATABASE_URL = 'live-db'\nXUI_DB_PATH = '/live/x-ui.db'\n",
        encoding="utf-8",
    )
    active_key = tmp_path / "live-vapid.pem"
    active_key.write_text("live-key", encoding="utf-8")

    with patch.object(manager, "APP_DIR", app):
        manager._restore_fargo_config_from_archive(archive)
        manager._restore_system_configs(archive)
        manager._restore_systemd_units(archive)

    result = active_config.read_text(encoding="utf-8")
    assert "PAYMENT_PRICE = 900" in result
    assert "DATABASE_URL = 'live-db'" in result
    assert "XUI_DB_PATH = '/live/x-ui.db'" in result
    assert active_key.read_text(encoding="utf-8") == "live-key"
