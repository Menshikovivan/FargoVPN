from pathlib import Path
import importlib.util
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def read(name: str) -> str:
    return (APP / name).read_text(encoding="utf-8")


def load_update_manager(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.GITHUB_REPOSITORY_OWNER = "Menshikovivan"
    cfg.GITHUB_REPOSITORY_NAME = "FargoVPN"
    cfg.GITHUB_TARGET_BRANCH = "main"
    cfg.GITHUB_API_TOKEN = "x" * 40
    cfg.GITHUB_API_BASE_URL = "https://api.github.com"
    cfg.GITHUB_MAIN_SYNC_ENABLED = True
    dj = types.ModuleType("detached_jobs")
    dj.DetachedJobError = RuntimeError
    dj.launch_detached = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "detached_jobs", dj)
    spec = importlib.util.spec_from_file_location("um_release512", APP / "update_manager.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod, cfg


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def test_panel_csp_is_single_and_allows_page_controllers():
    policy = read("security_policy.py")
    web = read("webapp.py")
    nginx = read("nginx_panel_guard.py")
    assert "script-src 'self' 'unsafe-inline' https://telegram.org" in policy
    assert "PANEL_CONTENT_SECURITY_POLICY" in web
    assert "proxy_hide_header Content-Security-Policy;" in nginx
    assert "add_header Content-Security-Policy \"{csp}\" always;" in nginx


def test_logs_page_has_external_controller_and_retry_states():
    block = read("webapp.py").split('@app.get("/logs", response_class=HTMLResponse)', 1)[1]
    assert 'public_path("/static/logs.js")' in block
    logs = read("static/logs.js")
    assert 'id="logs-retry"' in block
    assert "retry?.addEventListener('click',load)" in logs
    for needle in ('Файл не найден','Нет прав','Журнал пуст','Ошибка чтения'):
        assert needle in logs


def test_messages_page_uses_ajax_and_retry_without_reload():
    block = read("webapp.py").split("def messages_page(", 1)[1]
    block = block.split('@app.get("/users/new"', 1)[0]
    assert "event.preventDefault()" in block
    assert "window.apiFetch(compose.action" in block
    assert "id=\"messages-compose-retry\"" in block
    assert "location.reload" not in block
    assert "window.location.reload" not in block


def test_github_validation_checks_token_repo_permission_and_branch(monkeypatch):
    m, cfg = load_update_manager(monkeypatch)
    calls = []

    def fake(method, path, **kwargs):
        calls.append((method, path))
        if path == "/user":
            return Resp(200, {"login": "Menshikovivan"})
        if path == "/repos/Menshikovivan/FargoVPN":
            return Resp(200, {"full_name": "Menshikovivan/FargoVPN", "permissions": {"push": True}, "default_branch": "main"})
        if path == "/repos/Menshikovivan/FargoVPN/git/ref/heads/main":
            return Resp(200, {"object": {"sha": "a" * 40}})
        raise AssertionError(path)

    monkeypatch.setattr(m, "github_request", fake)
    result = m.github_validate_configuration()
    assert result["login"] == "Menshikovivan"
    assert result["repository"] == "Menshikovivan/FargoVPN"
    assert result["target_branch"] == "main"
    assert result["can_write"] is True
    assert calls[-1][1].endswith("/git/ref/heads/main")


def test_github_validation_maps_common_failures(monkeypatch):
    m, _cfg = load_update_manager(monkeypatch)

    for status, needle in ((401, "недействителен"), (403, "запрещён")):
        def fake_user(method, path, _status=status):
            return Resp(_status, {"message": "denied"})
        monkeypatch.setattr(m, "github_request", fake_user)
        try:
            m.github_validate_configuration()
            assert False, "UpdateError expected"
        except m.UpdateError as exc:
            assert needle in str(exc)

    def repo_404(method, path):
        if path == "/user":
            return Resp(200, {"login": "Menshikovivan"})
        return Resp(404, {"message": "Not Found"})
    monkeypatch.setattr(m, "github_request", repo_404)
    try:
        m.github_validate_configuration()
        assert False, "UpdateError expected"
    except m.UpdateError as exc:
        assert "HTTP 404" in str(exc)

    def branch_404(method, path):
        if path == "/user":
            return Resp(200, {"login": "Menshikovivan"})
        if path == "/repos/Menshikovivan/FargoVPN":
            return Resp(200, {"full_name": "Menshikovivan/FargoVPN", "permissions": {"push": True}, "default_branch": "main"})
        return Resp(404, {"message": "Not Found"})
    monkeypatch.setattr(m, "github_request", branch_404)
    try:
        m.github_validate_configuration()
        assert False, "UpdateError expected"
    except m.UpdateError as exc:
        assert "ветка main не существует" in str(exc)


def test_github_validation_refuses_read_only_repo(monkeypatch):
    m, _cfg = load_update_manager(monkeypatch)

    def read_only(method, path):
        if path == "/user":
            return Resp(200, {"login": "Menshikovivan"})
        return Resp(200, {"full_name": "Menshikovivan/FargoVPN", "permissions": {"push": False, "maintain": False, "admin": False}, "default_branch": "main"})

    monkeypatch.setattr(m, "github_request", read_only)
    try:
        m.github_validate_configuration()
        assert False, "UpdateError expected"
    except m.UpdateError as exc:
        assert "права записи" in str(exc)


def test_publish_verification_rechecks_release_tag_and_main_head():
    source = read("update_manager.py")
    block = source[source.index("def _verify_published_release"):source.index("PUBLISH_BUSY_STATES")]
    assert "/releases/{release_id}" in block
    assert "/git/ref/heads/{quote(target_branch, safe='')}" in block
    assert "actual_head_sha != commit_sha" in block


def test_legacy_dedup_prune_uses_postgres_text_cast():
    source = read("user_events.py")
    assert "first_seen_at::timestamptz" in source
    assert "datetime(first_seen_at)" in source
    assert 'f"-{keep_days} days"' in source
