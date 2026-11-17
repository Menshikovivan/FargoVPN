from __future__ import annotations

import ast
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def test_file_rollback_keeps_sqlite_and_units(tmp_path, monkeypatch):
    tree = ast.parse((APP / "restore_manager.py").read_text(encoding="utf-8"))
    names = {"_snapshot_current_state", "_rollback_files", "_sqlite_xui_rollback", "_sqlite_remove_sidecars"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    conf = app_dir / "config.py"
    conf.write_text("PAYMENT_PRICE = 200\n", encoding="utf-8")
    xui = tmp_path / "etc/x-ui/x-ui.db"
    xui.parent.mkdir(parents=True)
    unit_path = tmp_path / "etc/systemd/system"
    unit_path.mkdir(parents=True)
    units = ("vpn-service-update@.service", "vpn-service-broadcast@.service")
    for unit in units:
        (unit_path / unit).write_text(unit, encoding="utf-8")

    def route_path(filename):
        path = Path(filename)
        if str(filename).startswith("/etc/"):
            return tmp_path / str(filename).lstrip("/")
        return path

    with closing(sqlite3.connect(xui)) as connection, connection:
        connection.execute("CREATE TABLE marker(value)")
        connection.execute("INSERT INTO marker VALUES ('pre-restore')")
    scopes = {
        "APP_DIR": app_dir, "Path": route_path, "os": os, "shutil": shutil,
        "tempfile": tempfile, "_external_units": list, "_fargovpn_units": list,
        "_capture_service_states": lambda _units: {},
        "_cfg": lambda _key, default: default,
        "subprocess": SimpleNamespace(run=lambda *_args, **_kwargs: pytest.fail("unexpected host systemctl")),
    }
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    exec(compile(ast.Module(body=functions, type_ignores=[]), "restore_manager.py", "exec"), scopes)
    snapshot, _ = scopes["_snapshot_current_state"]()
    assert [path.name for path in snapshot.iterdir()] == ["config.py"]

    # A commit made after the initial configuration snapshot must survive a
    # failure after the database swap.
    rollback = tmp_path / "rollback.db"
    with closing(sqlite3.connect(xui)) as live, closing(sqlite3.connect(rollback)) as safe:
        with live:
            live.execute("UPDATE marker SET value='latest commit'")
        live.backup(safe)
        with live:
            live.execute("UPDATE marker SET value='archive data'")
    conf.write_text("PAYMENT_PRICE = 500\n", encoding="utf-8")
    scopes["_sqlite_xui_rollback"](xui, rollback)
    scopes["_rollback_files"](snapshot)
    assert conf.read_text(encoding="utf-8") == "PAYMENT_PRICE = 200\n"
    with closing(sqlite3.connect(xui)) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("latest commit",)
    for unit in units:
        assert (unit_path / unit).read_text(encoding="utf-8") == unit


def test_failed_config_snapshot_does_not_stop_services(tmp_path, monkeypatch):
    tree = ast.parse((APP / "restore_manager.py").read_text(encoding="utf-8"))
    restore = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_restore_full")
    deleted = []
    states = []
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(DATABASE_URL="postgresql://test-local"))

    def fail_snapshot():
        raise OSError("test snapshot failure")

    scopes = {
        "Path": Path, "Any": object, "time": SimpleNamespace(strftime=lambda _format: "test"),
        "logger": logging.getLogger(__name__), "_current_xui_kind": lambda: "SQLite",
        "make_staging_db": lambda *_args: ("postgresql://test-staging", "test-staging"),
        "_snapshot_current_state": fail_snapshot, "drop_database": deleted.append,
        "write_restore_state": lambda **state: states.append(state),
        "stop_services": lambda **_kwargs: pytest.fail("preflight failure stopped services"),
        "_restore_service_states": lambda _state: pytest.fail("preflight failure touched services"),
    }
    exec(compile(ast.Module(body=[restore], type_ignores=[]), "restore_manager.py", "exec"), scopes)
    with pytest.raises(OSError, match="test snapshot failure"):
        scopes["_restore_full"](tmp_path, {"_xui_kind": "SQLite"})
    assert deleted and set(deleted) == {"test-staging"}
    assert states[-1]["phase"] == "rollback_done"


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux flock and Bash")
def test_installer_and_workers_share_lock_without_nested_deadlock(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(APP))
    import maintenance_lock as maintenance

    file = tmp_path / "maintenance.lock"
    monkeypatch.setattr(maintenance, "LOCK_PATH", file)
    lock_block = (APP / "install.sh").read_text(encoding="utf-8").split(
        "MAINTENANCE_LOCK=/run/vpn-service-maintenance.lock", 1,
    )[1].split('INSTALL_LOG="/var/log/vpn-service-install.log"', 1)[0]
    lock_block = 'MAINTENANCE_LOCK="$1"\n' + lock_block
    child_source = (
        "from pathlib import Path; import maintenance_lock as m, sys; "
        "m.LOCK_PATH = Path(sys.argv[1]); "
        "scope = m.maintenance_lock(inherit=True); "
        "scope.__enter__(); scope.__exit__(None, None, None)"
    )
    run_child = 'exec "$2" -c "$3" "$1"'
    shell = ["bash", "-e", "-c", lock_block + run_child, "test-installer", str(file), sys.executable, child_source]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(APP)
    environment.pop(maintenance.INHERITED_FD_ENV, None)
    with maintenance.maintenance_lock() as fd:
        rejected = subprocess.run(shell, env=environment, capture_output=True, timeout=5)
        assert rejected.returncode == 1
        environment[maintenance.INHERITED_FD_ENV] = str(fd)
        nested = subprocess.run(shell, env=environment, pass_fds=(fd,), capture_output=True, timeout=5)
        assert nested.returncode == 0, nested.stderr.decode()
        with pytest.raises(maintenance.MaintenanceBusyError):
            maintenance.ensure_maintenance_idle()
    environment.pop(maintenance.INHERITED_FD_ENV, None)
    assert subprocess.run(shell, env=environment, timeout=5).returncode == 0
    maintenance.ensure_maintenance_idle()
