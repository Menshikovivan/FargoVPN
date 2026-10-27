"""Real FastAPI requests in an isolated process; no external calls or service restarts."""
import ast
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_public_launchers_admin_render_save_and_access_boundaries(tmp_path):
    # Reuse the existing HTTP suite's isolated configuration/upstream mocks.
    tree = ast.parse((ROOT / "tests/test_panel_http.py").read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "test_panel_routes_and_publisher_boundary")
    script = next(n.value.value for n in function.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "script" for t in n.targets))
    setup = script.split("client = TestClient(web.app)", 1)[0]
    windows_lock_stub = r'''
import os, sys, types
if os.name == "nt":
    # Windows has no flock. Only the OS locking primitive is synthetic;
    # this test does not write config.py or claim to verify Linux locking.
    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX, fcntl.LOCK_UN, fcntl.LOCK_SH, fcntl.LOCK_NB = 2, 8, 1, 4
    fcntl.flock = lambda *args: None
    sys.modules["fcntl"] = fcntl
'''
    checks = r'''
import html
import re
from html.parser import HTMLParser
import whitelist_help

client = TestClient(web.app, base_url="https://testserver", follow_redirects=False)
recorded = []
restarts = []
web.save_config_values = lambda values: recorded.append(dict(values))
web.audit = lambda *args, **kwargs: None
web.auth_security.prune = lambda *args, **kwargs: None
web.user_events.prune_events = lambda *args, **kwargs: None
web.schedule_restart = lambda units, delay=2: restarts.append((units, delay))

# Public collection import requires neither a personal subscription nor login.
for platform, app in (("android", "happ"), ("ios", "happ"), ("windows", "happ"), ("ios", "incy")):
    for backup in (0, 1):
        response = client.get(f"/open-whitelist/{platform}/{app}?backup={backup}")
        assert response.status_code == 200, response.text
        sub = whitelist_help.subscription_url(config, platform, app, backup=bool(backup))
        assert html.escape(f"{app}://add/{sub}", quote=True) in response.text
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in response.headers["cache-control"]
        assert "бесплатные сторонние подборки" in response.text
        assert "access_token" not in response.text and "tg_id" not in response.text
for route in ("/open-whitelist/android/incy", "/open-whitelist/windows/incy",
              "/open-whitelist/unknown/happ", "/open-whitelist/ios/unknown",
              "/open-whitelist/ios/happ?backup=2", "/open-whitelist/ios/happ?backup=-1"):
    assert client.get(route).status_code == 404, route
assert client.get("/open-whitelist/ios/happ?backup=invalid").status_code == 422
assert client.get("/settings").status_code in (401, 303)
assert client.post("/settings", data={"whitelist_android_url": "https://bad.example"}).status_code in (401, 303)
assert not recorded

config.WEB_USERNAME = "admin"
cookie = TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({
    "auth": True, "auth_version": web.auth_credentials_version(),
    "user": "admin", "csrf_token": "test-csrf",
}).encode())).decode()
client.cookies.set("session", cookie)
page = client.get("/settings")
assert page.status_code == 200, page.text
assert page.headers["referrer-policy"] == "same-origin"

class SettingsInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = []
    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))
parser = SettingsInputs()
parser.feed(page.text)
names = {i.get("name") for i in parser.inputs}
assert {field[1] for field in whitelist_help.FIELDS}.issubset(names)
assert "Бесплатные сторонние подборки" in page.text
assert "Happ" in page.text and "Incy" in page.text

# Save all new links and the existing editable bot texts through the actual
# authenticated, CSRF-protected HTTP form handler.
form = {
    "service_name": "FargoVPN QA", "web_username": "admin",
    "reminder_days": "7,3,1,0", "settings_tab": "bot",
    "bot_welcome_text": "QA приветствие {service}",
    "bot_support_prompt": "QA напишите вопрос",
    "faq_incy_url": "https://example.org/incy-app",
}
for key, name, _, _ in whitelist_help.FIELDS:
    form[name] = "https://edited.example.org/" + name
response = client.post("/settings", data=form)
assert response.status_code == 403 and not recorded
response = client.post("/settings", data={**form, "csrf_token": "test-csrf"})
assert response.status_code == 303, response.text
assert len(recorded) == 1, client.get("/settings").text
values = recorded[-1]
assert values["BOT_WELCOME_TEXT"] == form["bot_welcome_text"]
assert values["BOT_SUPPORT_PROMPT"] == form["bot_support_prompt"]
assert values["FAQ_INCY_URL"] == form["faq_incy_url"]
for key, name, _, _ in whitelist_help.FIELDS:
    assert values[key] == form[name]
assert restarts == [(["vpn-service-bot.service", "vpn-service-web.service"], 2)]
edited_page = client.get("/settings")
assert edited_page.status_code == 200
for key, name, _, _ in whitelist_help.FIELDS:
    assert html.escape(form[name], quote=True) in edited_page.text
updated_launcher = client.get("/open-whitelist/ios/incy")
assert "https://edited.example.org/whitelist_incy_sub_url" in updated_launcher.text

# Invalid link must never reach persistence. Existing saved links remain.
bad = {**form, "csrf_token": "test-csrf", "whitelist_android_url": "javascript:alert(1)"}
assert client.post("/settings", data=bad).status_code == 303
assert len(recorded) == 1
failure_page = client.get("/settings")
assert "Не удалось сохранить настройки" in failure_page.text
assert config.WHITELIST_ANDROID_URL == form["whitelist_android_url"]

# Explicitly cleared optional collection is hidden and its old public launcher
# cannot serve a stale URL from cache.
cleared = {**form, "csrf_token": "test-csrf", "whitelist_incy_sub_url": ""}
assert client.post("/settings", data=cleared).status_code == 303
assert len(recorded) == 2
assert client.get("/open-whitelist/ios/incy").status_code == 404

# Server-generated JS string and fallback HTML cannot be broken out of by an
# administrator-supplied URL containing HTML delimiters.
config.WHITELIST_IOS_URL = 'https://example.org/</script><script>alert(1)</script>?x="&y=1'
malicious = client.get("/open-whitelist/ios/happ")
assert malicious.status_code == 200
assert malicious.text.count("<script>") == malicious.text.count("</script>") == 1
native_json = re.search(r"<script>const u=(.*?);setTimeout", malicious.text).group(1)
assert "<" not in native_json and ">" not in native_json
assert json.loads(native_json) == "happ://add/" + config.WHITELIST_IOS_URL
print("Public launchers, actual admin form render/save, CSRF, access control and edited links passed")
'''
    result = subprocess.run(
        [sys.executable, "-c", windows_lock_stub + setup + checks, str(ROOT / "app")],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT / "app"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
