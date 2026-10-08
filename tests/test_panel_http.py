"""HTTP regression checks in an isolated process; external services are mocked."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1] / "app"


def test_panel_routes_and_publisher_boundary(tmp_path):
    script = r'''
import base64
import hashlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch
from itsdangerous import TimestampSigner

root = Path(sys.argv[1])
ROOT = root
spec = importlib.util.spec_from_file_location("config", root / "config.example.py")
config = importlib.util.module_from_spec(spec)
sys.modules["config"] = config
spec.loader.exec_module(config)
config.BOT_TOKEN = ""
config.MASTER_API_TOKEN = ""
config.WEB_PUBLIC_PREFIX = ""
config.DB_PATH = str(Path.cwd() / "test.db")
config.BACKUP_DIR = str(Path.cwd() / "backup")
config.UPDATE_DIR = str(Path.cwd() / "updates")
import psutil
psutil.net_io_counters = lambda: type("Counters", (), {"bytes_recv": 0, "bytes_sent": 0})()
with patch("logging.FileHandler", lambda *a, **k: logging.NullHandler()):
    import webapp as web
from fastapi.testclient import TestClient
manager = web.update_manager
# The production digest remains fixed. Test a synthetic identity without exposing it.
manager.PUBLISHER_USERNAME_DIGEST = hashlib.sha256(b"test-publisher").hexdigest()
assert manager.is_publisher_username("test-publisher")
assert manager.is_publisher_username(" test-publisher ")
for name in ("", None, "admin", "TEST-PUBLISHER", "test-publisherX"):
    assert not manager.is_publisher_username(name)
config.UPDATE_IS_PUBLISHER = True
config.UPDATE_PUBLISHER_USERNAME = "admin"
manager.read_status = lambda: {"state": "idle"}
manager.cached_update_info = lambda: {}
manager.check_available_update = lambda **kw: {}
manager.list_preupdate_backups = lambda: []
manager.latest_local_update = lambda: None
manager.github_release_history = lambda *a: []
web.user_events.unread_messages_summary = lambda **kw: {"total": 0}
web.service_state = lambda *a, **kw: "active"
web.fetch_and_sync = lambda **kw: {"clients": [], "stale": False}
web.service_audit.audit = lambda: {"healthy": True}
web.platform_diagnostics.database_report = lambda *a, **kw: {"healthy": True}
web.platform_diagnostics.storage_report = lambda *a, **kw: {"healthy": True}
web.platform_diagnostics.config_permissions_report = lambda: {"healthy": True}
web.shutil.which = lambda name: None
web.httpx.get = lambda *a, **kw: type("Reply", (), {"json": lambda self: {"ok": True}})()
client = TestClient(web.app)
for session_user, configured_user, expected in (
    ("test-publisher", "test-publisher", True),
    ("admin", "admin", False),
    ("test-publisher", "admin", False),
    ("admin", "test-publisher", False),
):
    config.WEB_USERNAME = configured_user
    cookie = TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({
        "auth": True, "user": session_user, "csrf_token": "test-csrf"
    }).encode())).decode()
    client.cookies.set("session", cookie)
    assert manager.publisher_enabled() == (configured_user == "test-publisher")
    report = web.platform_diagnostics.update_topology_report()
    assert report["role"] == ("publisher" if configured_user == "test-publisher" else "follower")
    for route, heading in (("/settings", "Настройки"), ("/updates", "Обновления"), ("/diagnostics", "Диагностика")):
        response = client.get(route)
        assert response.status_code == 200, (route, response.status_code, response.text)
        assert heading in response.text
    response = client.get("/api/diagnostics")
    assert response.status_code == 200
    assert response.json()["version"] == (ROOT / "VERSION").read_text().strip()
    response = client.get("/api/updates/releases")
    assert response.status_code == (200 if expected else 403), response.text
    response = client.post("/updates/config", headers={"x-csrf-token": "test-csrf"})
    assert response.status_code == (410 if expected else 403), response.text
# Execute settings validation through the persistence boundary without writing.
from starlette.requests import Request
from fastapi import HTTPException
class PersistenceReached(Exception):
    pass
recorded = []
def capture_settings(values):
    recorded.append(values)
    raise PersistenceReached()
web.save_config_values = capture_settings
for actor, target, denied in (("admin", "admin", False), ("admin", "test-publisher", True), ("test-publisher", "test-publisher", False)):
    request = Request({"type": "http", "method": "POST", "path": "/settings", "headers": [], "session": {"auth": True, "user": actor}})
    form = {"service_name": "TestVPN", "web_username": target, "reminder_days": "7,3,1,0"}
    try:
        web._save_settings(request, form)
    except PersistenceReached:
        assert not denied
        assert recorded[-1]["WEB_USERNAME"] == target
    except HTTPException as error:
        assert denied and error.status_code == 403, error.detail
    else:
        raise AssertionError("Expected persistence boundary or access denial")
client.cookies.clear()
for route in ("/settings", "/updates", "/diagnostics", "/api/diagnostics"):
    assert client.get(route, follow_redirects=False).status_code in (401, 303)
print("HTTP routes, diagnostics and publisher boundary passed")
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT)], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])},
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
