from __future__ import annotations

import ast
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load_restore_state_functions(path: Path, systemctl_run):
    source = (ROOT / "app/restore_manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_safe_json_write",
        "_read_restore_state_raw",
        "_restore_worker_active",
        "read_restore_state",
        "write_restore_state",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {
        "RESTORE_STATE_PATH": path,
        "datetime": datetime,
        "json": json,
        "os": os,
        "re": re,
        "subprocess": SimpleNamespace(
            run=systemctl_run,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
        "time": time,
        "logger": SimpleNamespace(warning=lambda *_args: None),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "restore_state_functions", "exec"), namespace)
    return namespace


def write_old_state(path: Path, **overrides):
    state = {
        "job_id": "deadbeef",
        "status": "running",
        "phase": "restore",
        "progress": 45,
        "message": "Восстановление выполняется",
        "updated_at": (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds"),
        **overrides,
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    return state


def systemctl_result(stdout: str, returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_inactive_stale_worker_is_marked_interrupted_and_persisted(tmp_path):
    path = tmp_path / "restore-state.json"
    write_old_state(path, manifest_version="5.1.18")
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return systemctl_result("loaded\ninactive\n")

    state = load_restore_state_functions(path, run)["read_restore_state"]()

    assert state["status"] == "interrupted"
    assert state["phase"] == "interrupted"
    assert "активной службы восстановления нет" in state["message"]
    assert state["error"] == "Служба восстановления не активна"
    assert "manifest_version" not in state
    assert json.loads(path.read_text(encoding="utf-8")) == state
    assert calls[0][2] == "vpn-service-restore-deadbeef.service"


def test_active_stale_worker_remains_running():
    path = Path("restore-state.json")
    state_before = {
        "job_id": "deadbeef",
        "status": "running",
        "updated_at": (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds"),
    }

    def run(_args, **_kwargs):
        return systemctl_result("loaded\nactive\n")

    namespace = load_restore_state_functions(path, run)
    namespace["_read_restore_state_raw"] = lambda: dict(state_before)

    assert namespace["read_restore_state"]() == state_before


def test_recent_queued_restore_is_not_mistaken_for_a_dead_worker():
    path = Path("restore-state.json")
    fresh_state = {
        "job_id": "deadbeef",
        "status": "queued",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    def unexpected_systemctl(_args, **_kwargs):
        raise AssertionError("systemd check must not run for a fresh restore")

    namespace = load_restore_state_functions(path, unexpected_systemctl)
    namespace["_read_restore_state_raw"] = lambda: dict(fresh_state)

    assert namespace["read_restore_state"]() == fresh_state


def test_systemctl_failure_keeps_status_to_avoid_false_stale_detection():
    path = Path("restore-state.json")
    state_before = {
        "job_id": "deadbeef",
        "status": "running",
        "updated_at": (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds"),
    }

    def run(_args, **_kwargs):
        return systemctl_result("", returncode=1)

    namespace = load_restore_state_functions(path, run)
    namespace["_read_restore_state_raw"] = lambda: dict(state_before)

    assert namespace["read_restore_state"]() == state_before


def test_legacy_stale_state_without_worker_id_is_marked_interrupted():
    path = Path("restore-state.json")
    state_before = {
        "status": "queued",
        "updated_at": (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds"),
    }

    def unexpected_systemctl(_args, **_kwargs):
        raise AssertionError("legacy state has no worker ID to check")

    namespace = load_restore_state_functions(path, unexpected_systemctl)
    namespace["_read_restore_state_raw"] = lambda: dict(state_before)
    namespace["_safe_json_write"] = lambda *_args: None

    assert namespace["read_restore_state"]()["status"] == "interrupted"


def test_backup_page_and_restore_api_use_stale_aware_state_reader():
    tree = ast.parse((ROOT / "app/webapp.py").read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    for name in ("backups", "restore_status", "restore_backup", "backup_delete"):
        calls = [
            node for node in ast.walk(functions[name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read_restore_state"
        ]
        assert calls, f"{name} must use stale-aware restore state"


def test_new_restore_job_does_not_inherit_stale_error_or_result(tmp_path):
    path = tmp_path / "restore-state.json"
    write_old_state(
        path,
        status="failed",
        error="old error",
        result={"old": True},
    )
    namespace = load_restore_state_functions(path, lambda *_args, **_kwargs: None)

    namespace["write_restore_state"](
        job_id="newjob",
        status="queued",
        phase="queue",
        progress=1,
        message="Восстановление поставлено в очередь",
    )

    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["job_id"] == "newjob"
    assert "error" not in state
    assert "result" not in state
