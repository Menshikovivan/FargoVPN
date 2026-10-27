"""HTTP-level checks for the bot community URL setting in an isolated app process."""
import ast
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_community_url_render_save_clear_reject_and_legacy_post(tmp_path):
    # Reuse the HTTP suite's isolated config, session signer and upstream mocks.
    tree = ast.parse((ROOT / "tests/test_panel_http.py").read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "test_panel_routes_and_publisher_boundary"
    )
    script = next(
        node.value.value for node in function.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "script" for target in node.targets)
    )
    setup = script.split("client = TestClient(web.app)", 1)[0]
    windows_lock_stub = r'''
import os, sys, types
if os.name == "nt":
    # This test mocks config persistence; only the unavailable OS lock is synthetic.
    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX, fcntl.LOCK_UN, fcntl.LOCK_SH, fcntl.LOCK_NB = 2, 8, 1, 4
    fcntl.flock = lambda *args: None
    sys.modules["fcntl"] = fcntl
'''
    checks = r'''
import base64
import json
from itsdangerous import TimestampSigner
from fastapi.testclient import TestClient
import whitelist_help

client = TestClient(web.app, base_url="https://testserver", follow_redirects=False)
saved = []
restarts = []
web.save_config_values = lambda values: saved.append(dict(values))
web.audit = lambda *args, **kwargs: None
web.auth_security.prune = lambda *args, **kwargs: None
web.user_events.prune_events = lambda *args, **kwargs: None
web.schedule_restart = lambda units, delay=2: restarts.append((units, delay))
config.WEB_USERNAME = "admin"
config.BOT_COMMUNITY_URL = "https://t.me/fargovpn"
cookie = TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({
    "auth": True,
    "auth_version": web.auth_credentials_version(),
    "user": "admin",
    "csrf_token": "community-test-csrf",
}).encode())).decode()
client.cookies.set("session", cookie)

# The configured default is visible as a labelled input in the real settings page.
page = client.get("/settings")
assert page.status_code == 200, page.text
assert 'name="bot_community_url"' in page.text
assert 'value="https://t.me/fargovpn"' in page.text
assert "Пустое поле скрывает кнопку" in page.text

form = {
    "service_name": "FargoVPN HTTP QA",
    "web_username": "admin",
    "reminder_days": "7,3,1,0",
    "settings_tab": "bot",
    "bot_welcome_text": "QA welcome",
    "bot_support_prompt": "QA support",
    "faq_incy_url": "https://apps.apple.com/ru/app/incy/id6756943388",
}
for key, name, _, default in whitelist_help.FIELDS:
    form[name] = getattr(config, key, default)

# A private Telegram group invite is accepted and reaches the existing save path.
group_link = "https://t.me/+AbCdEf_123456"
response = client.post("/settings", data={**form, "csrf_token": "community-test-csrf", "bot_community_url": group_link})
assert response.status_code == 303, response.text
assert len(saved) == 1
assert saved[-1]["BOT_COMMUNITY_URL"] == group_link
assert restarts

# An explicit blank persists as blank, disabling the bot's community button.
response = client.post("/settings", data={**form, "csrf_token": "community-test-csrf", "bot_community_url": ""})
assert response.status_code == 303, response.text
assert len(saved) == 2
assert saved[-1]["BOT_COMMUNITY_URL"] == ""

# A lookalike host is rejected before the persistence callback runs.
before_invalid = len(saved)
response = client.post("/settings", data={
    **form, "csrf_token": "community-test-csrf", "bot_community_url": "https://t.me.evil.example/fakegroup",
})
assert response.status_code == 303, response.text
assert len(saved) == before_invalid
assert "Не удалось сохранить настройки" in client.get("/settings").text

# Old settings forms omit the new control; preserve the current configured URL.
config.BOT_COMMUNITY_URL = "https://t.me/previous_group"
response = client.post("/settings", data={**form, "csrf_token": "community-test-csrf"})
assert response.status_code == 303, response.text
assert saved[-1]["BOT_COMMUNITY_URL"] == "https://t.me/previous_group"
print("Community setting HTTP checks passed: render, group save, blank, invalid host and legacy form")
'''
    result = subprocess.run(
        [sys.executable, "-c", windows_lock_stub + setup + checks, str(ROOT / "app")],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "app"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Community setting HTTP checks passed" in result.stdout
