from __future__ import annotations

import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import tempfile
import threading
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_update_module():
    root = ROOT / "app"
    cfg = types.ModuleType("config")
    cfg.GITHUB_REPOSITORY_OWNER = "Menshikovivan"
    cfg.GITHUB_REPOSITORY_NAME = "FargoVPN"
    cfg.GITHUB_RELEASE_ASSET_NAME = "VPN_Service_Platform_{version}_FULL.tar.gz"
    cfg.GITHUB_RELEASE_TAG_PREFIX = "v"
    cfg.GITHUB_MAIN_SYNC_ENABLED = True
    dj = types.ModuleType("detached_jobs")
    dj.DetachedJobError = RuntimeError
    dj.launch_detached = lambda *a, **k: None
    sys.modules["config"] = cfg
    sys.modules["detached_jobs"] = dj
    spec = importlib.util.spec_from_file_location("um519", root / "update_manager.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_console_installer_contains_no_github_mutation_path():
    installer = (ROOT / "app" / "install.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "install.sh").read_text(encoding="utf-8")
    forbidden = (
        "--sync-main-directory", "git push", "git commit", "git tag",
        'github_request("POST"', 'github_request("PATCH"',
        'github_request("PUT"', 'github_request("DELETE"',
    )
    shell_sources = [ROOT / "install.sh", *((ROOT.parent / "scripts").glob("*.sh"))]
    for source in shell_sources:
        text = source.read_text(encoding="utf-8")
        assert "git push" not in text and "git commit" not in text and "git tag" not in text
        assert "gh release create" not in text and "gh release upload" not in text
    assert not any(token in installer for token in forbidden)
    assert "releases/latest/download" in bootstrap
    assert "--progress-bar" in bootstrap
    assert "GET" in bootstrap


def test_installer_has_numbered_timed_steps_and_xui_deadline():
    installer = (ROOT / "app" / "install.sh").read_text(encoding="utf-8")
    assert "INSTALL_STEP=0" in installer
    assert "run_timed()" in installer
    assert "run_timed 45" in installer
    assert "run_with_timeout 25 target_python" in installer
    assert "Ожидается: Unix socket" in installer
    assert "3x-ui API" in installer


def test_console_installer_embedded_config_python_compiles():
    import ast
    import re

    installer = (ROOT / "app" / "install.sh").read_text(encoding="utf-8")
    pattern = r"\"\$TARGET/\.venv/bin/python\" <<'PY'\n(?P<body>.*?)\nPY\n"
    matches = list(re.finditer(pattern, installer, re.S))
    assert len(matches) >= 2, "installer must contain the database/config Python block"
    body = matches[1].group("body")
    ast.parse(body, filename="app/install.sh embedded config block")
    assert 'ensure("PUBLISH_STALE_JOB_SECONDS", 1800)' in body


def test_publish_remote_validation_is_worker_only():
    manager = (ROOT / "app" / "update_manager.py").read_text(encoding="utf-8")
    worker = (ROOT / "app" / "publish_worker.py").read_text(encoding="utf-8")
    start = manager.index("def start_publish_job")
    end = manager.index("def publish_update")
    assert "github_validate_configuration()" not in manager[start:end]
    assert "github_validate_configuration()" in worker
    assert "progress=5" in worker and "phase=\"github-auth\"" in worker


def test_logs_sources_and_download_api_contract():
    web = (ROOT / "app" / "webapp.py").read_text(encoding="utf-8")
    assert '"github-publish"' in web
    assert '"github-launcher"' in web
    assert '"/var/log/vpn_bot.log"' in web
    assert '@app.get("/api/logs/download")' in web
    assert "logs-level" in web and "logs-query" in web and "logs-auto" in web
    logs=(ROOT / "app" / "static" / "logs.js").read_text(encoding="utf-8")
    assert "AbortSignal.timeout(16000)" in logs
    assert "history.replaceState" in logs


def test_diagnose_github_is_get_only_and_secrets_redacted():
    diag = (ROOT / "app" / "diagnose.py").read_text(encoding="utf-8")
    assert "method='GET'" in diag
    assert "/user" in diag and "/repos/" in diag
    assert "Authorization" in diag
    assert "[REDACTED]" in diag
    assert "method='POST'" not in diag and "method='PATCH'" not in diag and "method='DELETE'" not in diag


def test_console_bootstrap_network_is_only_get(tmp_path: Path):
    root = tmp_path / "FargoVPN-5.1.11"
    (root / "app").mkdir(parents=True)
    (root / "app" / "VERSION").write_text("5.1.11\n", encoding="utf-8")
    (root / "app" / "install.sh").write_text(
        "#!/usr/bin/env bash\necho LOCAL_INSTALL_5_1_10\n",
        encoding="utf-8",
    )
    os.chmod(root / "app" / "install.sh", 0o755)
    archive = tmp_path / "fixture.tar.gz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(tmp_path), root.name], check=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = tmp_path / "fixture.sha256"
    checksum.write_text(f"{digest}  FargoVPN_FULL.tar.gz\n", encoding="utf-8")

    calls = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.command, self.path))
            if self.path.endswith(".sha256"):
                body = checksum.read_bytes()
                self.send_response(200)
            else:
                body = archive.read_bytes()
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            calls.append((self.command, self.path))
            self.send_response(405); self.end_headers()
        def log_message(self, *_):
            return

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        root_script = ROOT / "install.sh"
        env = os.environ.copy()
        env["FARGOVPN_USE_REMOTE_LATEST"] = "1"
        env["FARGOVPN_ARCHIVE_URL"] = f"http://127.0.0.1:{server.server_address[1]}/FargoVPN_FULL.tar.gz"
        env["FARGOVPN_CHECKSUM_URL"] = f"http://127.0.0.1:{server.server_address[1]}/FargoVPN_FULL.tar.gz.sha256"
        result = subprocess.run(["bash", str(root_script), "--help"], env=env, capture_output=True, text=True, timeout=20)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.returncode == 0
    assert "LOCAL_INSTALL_5_1_10" in result.stdout
    assert calls and all(method == "GET" for method, _ in calls)


def test_github_api_error_contracts_cover_auth_permission_not_found_and_conflict():
    m = _load_update_module()
    class Resp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload or {}
            self.text = json.dumps(self._payload)
        def json(self):
            return self._payload

    m.github_token = lambda: "x" * 40
    m.github_owner = lambda: "Menshikovivan"
    m.github_repo = lambda: "FargoVPN"

    responses = iter([Resp(401, {"message": "mock"})])
    m.github_request = lambda *a, **k: next(responses)
    try:
        m.github_validate_configuration()
    except m.UpdateError as error:
        assert "GitHub не принял токен: HTTP 401" in str(error)
    else:
        raise AssertionError("expected validation failure for HTTP 401")

    responses = iter([Resp(200, {"login": "tester"}), Resp(404, {"message": "not found"})])
    m.github_request = lambda *a, **k: next(responses)
    try:
        m.github_validate_configuration()
    except m.UpdateError as error:
        assert "Репозиторий GitHub недоступен: HTTP 404" in str(error)
    else:
        raise AssertionError("expected repository failure for HTTP 404")

    m.github_request = lambda *a, **k: Resp(200, {
        "login": "tester",
    }) if a[0] == "GET" and a[1] == "/user" else Resp(200, {
        "full_name": "Menshikovivan/FargoVPN",
        "permissions": {"push": False},
    })
    try:
        m.github_validate_configuration()
    except m.UpdateError as error:
        assert "права записи" in str(error)
    else:
        raise AssertionError("expected push-permission failure")

    calls = []
    m.github_request = lambda method, path, **kwargs: (calls.append((method, path)) or Resp(422, {"message": "conflict"}))
    m._release_tag_sha = lambda tag: None
    try:
        m._prepare_release_tag("v5.1.11", "a" * 40)
    except m.UpdateError as error:
        assert "HTTP 422" in str(error)
    else:
        raise AssertionError("expected release tag conflict")
    assert calls and calls[-1][0] == "POST"


def test_github_request_timeout_is_converted_to_safe_error(monkeypatch):
    import httpx

    m = _load_update_module()
    monkeypatch.setattr(m, "github_api_base", lambda: "https://mock.invalid")
    def boom(*_args, **_kwargs):
        raise httpx.ReadTimeout("mock timeout")
    monkeypatch.setattr(m.httpx, "request", boom)
    try:
        m.github_request("GET", "/user")
    except m.UpdateError as error:
        assert "Не удалось подключиться к GitHub" in str(error)
    else:
        raise AssertionError("expected timeout to be converted to UpdateError")


def test_publish_busy_reconciles_dead_systemd_worker(monkeypatch, tmp_path: Path):
    m = _load_update_module()
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(m, "publish_status_path", lambda: status_path)
    status_path.write_text(__import__("json").dumps({
        "state": "queued", "job_id": "pub-stale-1", "version": "5.1.14",
        "progress": 2, "updated_at": "2020-01-01T00:00:00+00:00",
        "unit": "vpn-service-publish-worker-dead",
    }), encoding="utf-8")
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 3, "stdout": "inactive\n", "stderr": ""})())
    assert m.publish_job_busy() is False
    status = m.read_publish_status()
    assert status["state"] == "failed"
    assert status["phase"] in {"reconcile", "stale"}


def test_publish_busy_reconciles_legacy_status_without_unit(monkeypatch, tmp_path: Path):
    m = _load_update_module()
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(m, "publish_status_path", lambda: status_path)
    status_path.write_text(__import__("json").dumps({
        "state": "validating", "job_id": "pub-legacy-dead", "version": "5.1.14",
        "progress": 8, "updated_at": "2020-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setattr(m, "_publish_worker_process_alive", lambda _job_id: False)
    assert m.publish_job_busy() is False
    status = m.read_publish_status()
    assert status["state"] == "failed"
    assert status["phase"] == "reconcile"


def test_publish_busy_keeps_live_legacy_worker_without_unit(monkeypatch, tmp_path: Path):
    m = _load_update_module()
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(m, "publish_status_path", lambda: status_path)
    from datetime import datetime, timezone
    status_path.write_text(__import__("json").dumps({
        "state": "validating", "job_id": "pub-legacy-live", "version": "5.1.17",
        "progress": 8, "updated_at": "2020-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setattr(m, "_publish_worker_process_alive", lambda _job_id: True)
    assert m.publish_job_busy() is True


def test_publish_busy_keeps_live_systemd_worker(monkeypatch, tmp_path: Path):
    m = _load_update_module()
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(m, "publish_status_path", lambda: status_path)
    from datetime import datetime, timezone
    status_path.write_text(__import__("json").dumps({
        "state": "validating", "job_id": "pub-live-1", "version": "5.1.17",
        "progress": 8, "updated_at": datetime.now(timezone.utc).isoformat(),
        "unit": "vpn-service-publish-worker-live",
    }), encoding="utf-8")
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "active\n", "stderr": ""})())
    assert m.publish_job_busy() is True


def test_publish_busy_guard_is_strict(monkeypatch, tmp_path: Path):
    m = _load_update_module()
    monkeypatch.setattr(m, "publish_job_busy", lambda: True)
    source = tmp_path / "release.tar.gz"
    # A structurally valid archive is not needed because the busy guard must
    # still prevent duplicate work before any background launch.
    source.write_bytes(b"not-used")
    try:
        m.start_publish_job(source, source.name, "tester")
    except m.UpdateError as error:
        assert "Другая публикация GitHub уже выполняется" in str(error)
    else:
        raise AssertionError("expected duplicate publish guard")


def test_postgres_summary_reads_environment_dsn():
    installer = (ROOT / "app" / "install.sh").read_text(encoding="utf-8")
    assert 'os.getenv("FARGOVPN_DATABASE_URL", "")' in installer
    assert 'os.getenv("DATABASE_URL", "")' in installer
