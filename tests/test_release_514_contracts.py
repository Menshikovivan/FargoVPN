from __future__ import annotations

import ast
import json
import inspect
import subprocess
import sys
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _load_update_manager():
    cfg = types.ModuleType("config")
    cfg.GITHUB_REPOSITORY_OWNER = "Menshikovivan"
    cfg.GITHUB_REPOSITORY_NAME = "FargoVPN"
    cfg.GITHUB_RELEASE_ASSET_NAME = "VPN_Service_Platform_{version}_FULL.tar.gz"
    cfg.GITHUB_RELEASE_TAG_PREFIX = "v"
    cfg.GITHUB_MAIN_SYNC_ENABLED = True
    cfg.GITHUB_API_BASE_URL = "https://api.github.com"
    cfg.GITHUB_API_TOKEN = "test-token"
    dj = types.ModuleType("detached_jobs")
    dj.DetachedJobError = RuntimeError
    dj.launch_detached = lambda *a, **k: None
    import importlib.util
    old_cfg, old_dj = sys.modules.get("config"), sys.modules.get("detached_jobs")
    sys.modules["config"], sys.modules["detached_jobs"] = cfg, dj
    spec = importlib.util.spec_from_file_location("update_manager_514_test", APP / "update_manager.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    if old_cfg is not None:
        sys.modules["config"] = old_cfg
    else:
        sys.modules.pop("config", None)
    if old_dj is not None:
        sys.modules["detached_jobs"] = old_dj
    else:
        sys.modules.pop("detached_jobs", None)
    return mod


def test_all_detached_launch_call_keywords_match_signature():
    signature_source = (APP / "detached_jobs.py").read_text(encoding="utf-8")
    module = ast.parse(signature_source)
    launch_def = next(node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "launch_detached")
    accepted = {arg.arg for arg in (*launch_def.args.posonlyargs, *launch_def.args.args, *launch_def.args.kwonlyargs)}
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            is_target = isinstance(fn, ast.Name) and fn.id == "launch_detached" or isinstance(fn, ast.Attribute) and fn.attr == "launch_detached"
            if not is_target:
                continue
            bad = {kw.arg for kw in node.keywords if kw.arg is not None and kw.arg not in accepted}
            assert not bad, f"{path}:{getattr(node, 'lineno', '?')} passes unknown launch_detached kwargs: {sorted(bad)}"


def test_all_detached_launch_sites_bind_full_signature():
    import ast
    signature_source = (APP / "detached_jobs.py").read_text(encoding="utf-8")
    module = ast.parse(signature_source)
    launch_def = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "launch_detached")
    accepted_sig = inspect.Signature.from_callable(
        __import__("detached_jobs").launch_detached
    )
    sites = 0
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            is_target = isinstance(fn, ast.Name) and fn.id == "launch_detached" or isinstance(fn, ast.Attribute) and fn.attr == "launch_detached"
            if not is_target:
                continue
            if any(isinstance(arg, ast.Starred) for arg in node.args) or any(kw.arg is None for kw in node.keywords):
                continue
            dummy_args = [object() for _ in node.args]
            dummy_kwargs = {kw.arg: object() for kw in node.keywords if kw.arg is not None}
            accepted_sig.bind(*dummy_args, **dummy_kwargs)
            sites += 1
    assert sites >= 7


def test_detached_jobs_contract_accepts_publish_output_path():
    sys.path.insert(0, str(APP))
    try:
        import detached_jobs
        sig = inspect.signature(detached_jobs.launch_detached)
        sig.bind("vpn-service-test", ["/bin/true"], description="test", working_directory=APP, output_path=APP / "x.log")
    finally:
        sys.path.pop(0)


def test_diagnose_json_mode_has_clean_stdout_and_report_file(tmp_path: Path):
    out = tmp_path / "reports"
    proc = subprocess.run(
        [sys.executable, str(APP / "diagnose.py"), "--app-dir", str(APP), "--output-dir", str(out), "--json"],
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["path"].startswith(str(out.resolve()))
    assert Path(payload["path"]).is_file()
    assert isinstance(payload["checks"], list) and payload["checks"]
    assert "EFFECTIVE NGINX CSP CONTRACT" in proc.stderr


def test_diagnostic_job_worker_is_tolerant_to_leading_json_noise(tmp_path: Path):
    src = (APP / "diagnostic_jobs.py").read_text(encoding="utf-8")
    assert "JSONDecoder().raw_decode" in src


def test_logs_api_and_static_controller_expose_explicit_states():
    webapp = (APP / "webapp.py").read_text(encoding="utf-8")
    logs_js = (APP / "static" / "logs.js").read_text(encoding="utf-8")
    assert '"status": status' in webapp
    for value in ("not_found", "permission", "empty", "error"):
        assert value in webapp
    assert "Content-Type" in logs_js or "content-type" in logs_js
    assert "Файл не найден" in logs_js
    assert "Нет прав" in logs_js
    assert "Журнал пуст" in logs_js
    assert "Не удалось загрузить журнал" in logs_js


def test_settings_push_registration_has_no_timestamp_churn_and_reload_guard():
    push = (APP / "static" / "push.js").read_text(encoding="utf-8")
    panel = (APP / "static" / "panel.js").read_text(encoding="utf-8")
    assert "push_reset" not in push
    assert "getRegistration(expected)" in push
    assert "sessionStorage.getItem(reloadKey" in panel
    assert "sessionStorage.setItem(reloadKey" in panel
    assert "if (reloaded)" in panel


def test_static_assets_are_prefix_aware():
    webapp = (APP / "webapp.py").read_text(encoding="utf-8")
    for name in ("users.js", "push.js", "settings.js", "diagnostic.js"):
        assert f'public_path("/static/{name}")' in webapp


def test_api_error_contract_is_json_for_api_paths():
    webapp = (APP / "webapp.py").read_text(encoding="utf-8")
    assert '@app.exception_handler(HTTPException)' in webapp
    assert '@app.exception_handler(Exception)' in webapp
    assert '"internal_error"' in webapp
    assert '"unauthorized"' in webapp


def test_github_validate_status_messages(monkeypatch):
    m = _load_update_manager()

    class Resp:
        def __init__(self, code, payload=None, text=""):
            self.status_code = code
            self._payload = payload or {}
            self.text = text
        def json(self):
            return self._payload

    def req(method, path, **kwargs):
        return Resp(401, {"message": "Bad credentials"})
    monkeypatch.setattr(m, "github_request", req)
    with pytest.raises(m.UpdateError, match="401"):
        m.github_validate_configuration()

    def req403(method, path, **kwargs):
        return Resp(403, {"message": "forbidden"})
    monkeypatch.setattr(m, "github_request", req403)
    with pytest.raises(m.UpdateError, match="403"):
        m.github_validate_configuration()

    calls = iter([
        Resp(200, {"login": "tester"}),
        Resp(404, {"message": "Not Found"}),
    ])
    monkeypatch.setattr(m, "github_request", lambda *a, **k: next(calls))
    with pytest.raises(m.UpdateError, match="404"):
        m.github_validate_configuration()


def test_github_request_timeout_and_connection_are_translated(monkeypatch):
    m = _load_update_manager()
    def timeout(*a, **k):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(m.httpx, "request", timeout)
    with pytest.raises(m.UpdateError, match="подключиться к GitHub"):
        m.github_request("GET", "/user")

    def broken(*a, **k):
        raise httpx.ConnectError("connection reset")
    monkeypatch.setattr(m.httpx, "request", broken)
    with pytest.raises(m.UpdateError, match="подключиться к GitHub"):
        m.github_request("GET", "/user")


def test_github_422_is_not_treated_as_success(monkeypatch):
    m = _load_update_manager()
    class Resp:
        status_code = 422
        text = "Validation Failed"
        def json(self): return {"message": "Validation Failed"}
    monkeypatch.setattr(m, "github_request", lambda *a, **k: Resp())
    with pytest.raises(m.UpdateError, match="422"):
        m._prepare_release_tag("v5.1.14", "a" * 40)


def test_service_worker_reload_marker_is_not_time_limited():
    panel = (APP / "static" / "panel.js").read_text(encoding="utf-8")
    assert "marker.version === version" in panel
    assert "now - Number(marker.at || 0) <" not in panel


def test_logs_download_url_is_prefix_aware():
    webapp = (APP / "webapp.py").read_text(encoding="utf-8")
    assert 'public_path(\'/api/logs/download?service=app&lines=300\')' in webapp


def test_qa_runner_renders_from_project_root():
    script = (ROOT / "tests/qa/run_qa.sh").read_text(encoding="utf-8")
    assert 'render_panel.py "$ROOT" "$QA_RENDER_DIR"' in script


def test_root_diagnose_wrapper_exists_and_delegates():
    wrapper = ROOT / "diagnose.sh"
    assert wrapper.is_file()
    assert 'exec bash "$ROOT_DIR/scripts/diagnose.sh" "$@"' in wrapper.read_text(encoding="utf-8")
