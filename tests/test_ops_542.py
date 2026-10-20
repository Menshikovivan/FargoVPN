"""Portable regressions; all Telegram/systemd interactions are synthetic."""
import ast
import importlib.util
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

APP = Path(os.environ.get("FARGOVPN_REGRESSION_APP", str(Path(__file__).resolve().parents[1] / "app")))


def module():
    spec = importlib.util.spec_from_file_location("detached_test", APP / "detached_jobs.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def extract(filename, names, namespace):
    tree = ast.parse((APP / filename).read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


def test_detached_loaded_error_is_not_success_or_retried(monkeypatch, tmp_path):
    d = module()
    monkeypatch.setattr(d.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(d.shutil, "which", lambda name: "systemd-run")
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "loaded", "") if args[:2] == ["systemctl", "show"] else subprocess.CompletedProcess(args, 1, "", "rejected")
    monkeypatch.setattr(d.subprocess, "run", run)
    with pytest.raises(d.DetachedJobError):
        d.launch_detached("synthetic-regression", ["python", "worker.py"], description="test", working_directory=tmp_path)
    assert len([args for args in calls if args[0] == "systemd-run"]) == 1


def test_detached_timeout_does_not_retry(monkeypatch, tmp_path):
    d = module()
    monkeypatch.setattr(d.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(d.shutil, "which", lambda name: "systemd-run")
    run = Mock(side_effect=subprocess.TimeoutExpired("systemd-run", 20))
    monkeypatch.setattr(d.subprocess, "run", run)
    with pytest.raises(d.DetachedJobError, match="могла запуститься"):
        d.launch_detached("synthetic-regression", ["python"], description="test", working_directory=tmp_path)
    assert run.call_count == 1


@pytest.mark.parametrize("retry_after,expected_calls,expected_sleep", [(120, 2, [120]), (7200, 1, [])])
def test_broadcast_respects_retry_after(retry_after, expected_calls, expected_sleep):
    sleeps = []
    response = SimpleNamespace(status_code=429, json=lambda: {"ok": False, "parameters": {"retry_after": retry_after}})
    success = SimpleNamespace(status_code=200, json=lambda: {"ok": True})
    client = SimpleNamespace(post=Mock(side_effect=[response, success]))
    env = extract("broadcast_worker.py", {"_telegram_call"}, {"httpx": SimpleNamespace(Client=object, RequestError=RuntimeError), "Any": object, "config": SimpleNamespace(BOT_TOKEN="synthetic"), "time": SimpleNamespace(sleep=sleeps.append)})
    ok, _, _ = env["_telegram_call"](client, "sendMessage", data={})
    assert ok == (retry_after <= 3600)
    assert client.post.call_count == expected_calls
    assert sleeps == expected_sleep


def test_configured_github_does_not_grant_publisher():
    env = extract("update_manager.py", {"publisher_enabled"}, {"config": SimpleNamespace(WEB_USERNAME="ordinary-admin"), "is_publisher_username": lambda name: name == "menshikovivan", "github_configuration_ready": lambda: True})
    assert env["publisher_enabled"]() is False
    env["config"].WEB_USERNAME = "menshikovivan"
    assert env["publisher_enabled"]() is True


@pytest.mark.parametrize("timed_out", [False, True])
def test_runtime_start_error_never_loaded_success(monkeypatch, tmp_path, timed_out):
    d = module()
    monkeypatch.setattr(d.Path, "write_text", lambda *a, **k: None)
    monkeypatch.setattr(d.Path, "replace", lambda *a, **k: None)
    monkeypatch.setattr(d.os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(d, "_loaded", lambda *a: True)
    def run(args, **kwargs):
        if args[1] == "daemon-reload":
            return subprocess.CompletedProcess(args, 0, "", "")
        if timed_out:
            raise subprocess.TimeoutExpired(args, 20)
        return subprocess.CompletedProcess(args, 1, "", "start denied")
    monkeypatch.setattr(d.subprocess, "run", run)
    with pytest.raises(d.DetachedJobError):
        d._runtime_unit("synthetic", ["python"], description="test", working_directory=tmp_path)


# Execute actual status writers with real temporary JSON files. On Windows only
# flock is substituted; these cases make no claim about interprocess locking.
def _status_writer(kind, monkeypatch, tmp_path):
    import json
    import re
    import secrets
    import sys
    import threading
    from datetime import datetime, timezone

    try:
        import fcntl
    except ImportError:
        fcntl = SimpleNamespace(LOCK_EX=2, flock=lambda *args: None)
        monkeypatch.setitem(sys.modules, "fcntl", fcntl)
    path = tmp_path / "status.json"
    if kind == "manager":
        env = extract("update_manager.py", {"write_status"}, {
            "Any": object, "status_path": lambda: path,
            "_STATUS_LOCK": threading.Lock(), "os": os, "fcntl": fcntl,
            "json": json, "datetime": datetime, "timezone": timezone,
            "threading": threading, "secrets": secrets,
            "BUSY_STATES": {"queued", "starting", "running", "installing", "checking"},
        })
        write = env["write_status"]
    else:
        source = (APP / "install.sh").read_text(encoding="utf-8")
        block = re.search(r"(?ms)^write_update_status\(\) \{.*?<<'PY'\n(.*?)^PY$", source)
        assert block, "actual installer embedded Python not found"
        code = compile(block.group(1), str(APP / "install.sh"), "exec")
        def write(state, **details):
            monkeypatch.setattr(sys, "argv", ["installer-status", str(path), state,
                "5.1.42", details.get("detail", "synthetic status"), "synthetic",
                str(details.get("progress", "")), details.get("phase", state),
                details.get("job_id", "")])
            exec(code, {})
    def read():
        return json.loads(path.read_text(encoding="utf-8"))
    def seed(data):
        path.write_text(json.dumps(data), encoding="utf-8")
    return write, read, seed


@pytest.mark.parametrize("kind", ["manager", "installer"])
def test_new_update_job_does_not_inherit_failed_job(kind, monkeypatch, tmp_path):
    write, read, seed = _status_writer(kind, monkeypatch, tmp_path)
    seed({"job_id": "old-job", "state": "failed", "progress": 97,
          "exit_code": 1, "error": "old failure", "finished_at": "old finish",
          "detail": "old detail", "phase": "failed"})
    write("queued", job_id="new-job", progress=0, phase="queued", detail="new queue")
    data = read()
    assert data["job_id"] == "new-job"
    assert data["state"] == "queued" and data["progress"] == 0
    assert data.get("exit_code") is None
    assert not data.get("error") and not data.get("finished_at")
    assert data["phase"] == "queued" and data["detail"] == "new queue"


@pytest.mark.parametrize("kind", ["manager", "installer"])
def test_completed_update_clears_stale_failure(kind, monkeypatch, tmp_path):
    write, read, seed = _status_writer(kind, monkeypatch, tmp_path)
    seed({"job_id": "same-job", "state": "running", "progress": 98,
          "exit_code": 1, "error": "old failure"})
    write("completed", job_id="same-job", progress=100)
    data = read()
    assert data["state"] == "completed" and data["progress"] == 100
    assert data["exit_code"] == 0
    assert not data.get("error")


@pytest.mark.parametrize("kind", ["manager", "installer"])
def test_failed_update_never_reports_zero_exit(kind, monkeypatch, tmp_path):
    write, read, seed = _status_writer(kind, monkeypatch, tmp_path)
    seed({"job_id": "same-job", "state": "running", "progress": 70,
          "exit_code": 0})
    write("failed", job_id="same-job", progress=70, detail="new failure")
    data = read()
    assert data["state"] == "failed"
    assert isinstance(data["exit_code"], int) and data["exit_code"] != 0


@pytest.mark.parametrize("kind", ["manager", "installer"])
def test_same_update_job_progress_never_regresses(kind, monkeypatch, tmp_path):
    write, read, seed = _status_writer(kind, monkeypatch, tmp_path)
    seed({"job_id": "same-job", "state": "running", "progress": 80,
          "phase": "install"})
    write("running", job_id="same-job", progress=30, phase="download")
    assert read()["progress"] == 80


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_manager_terminal_state_survives_late_launcher_ack(monkeypatch, tmp_path, terminal):
    write, read, seed = _status_writer("manager", monkeypatch, tmp_path)
    seed({"job_id": "same-job", "state": terminal, "progress": 100,
          "phase": "done", "finished_at": "original finish",
          "exit_code": 0 if terminal == "completed" else 1})
    write("queued", job_id="same-job", progress=1, phase="queued",
          detail="late ack", error="late false failure", unit="synthetic.service")
    data = read()
    assert data["state"] == terminal and data["progress"] == 100
    assert data["phase"] == "done" and data["finished_at"] == "original finish"
    assert not data.get("error")
    assert data["exit_code"] == (0 if terminal == "completed" else 1)
    assert data["unit"] == "synthetic.service"
