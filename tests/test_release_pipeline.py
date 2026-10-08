from pathlib import Path
import hashlib
import importlib.util
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parents[1] / "app"

def load_update_module():
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
    spec = importlib.util.spec_from_file_location("um512", ROOT / "update_manager.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

def test_versions():
    assert [(ROOT / name).read_text(encoding="utf-8").strip() for name in ("VERSION", "static/VERSION")] == ["5.1.19"] * 2
    assert "## 5.1.19" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")[:1000]

def test_public_surface_filters_runtime_material():
    m=load_update_module()
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        (root/"app").mkdir()
        (root/"app/VERSION").write_text("5.1.11\n")
        (root/"install.sh").write_text("#!/bin/bash\n")
        (root/"README.md").write_text("# FargoVPN\n")
        (root/"LICENSE").write_text("license\n")
        (root/"old.zip").write_bytes(b"zip")
        (root/"secret.pem").write_text("PRIVATE")
        (root/".env").write_text("TOKEN=x\n")
        files=m._github_main_public_files(root, root/"x.tar.gz", "5.1.11", hashlib.sha256(b"x").hexdigest())
        assert set(files)=={"install.sh","README.md","LICENSE","app/VERSION"}

def test_pruning_contract():
    source=(ROOT/"update_manager.py").read_text(encoding="utf-8")
    assert "stale_paths = sorted(current_paths - expected_paths)" in source
    assert "_verify_github_main_tree" in source
    assert "github_main_stale_paths_removed" in source
    assert '"tree": entries' in source

def test_progress_contract():
    source=(ROOT/"webapp.py").read_text(encoding="utf-8")
    css=(ROOT/"static/panel.css").read_text(encoding="utf-8")
    assert "update-phase" in source and "update-elapsed" in source
    assert "shownProgress=Math.max(shownProgress,nextProgress)" in source
    assert "shownProgress=Math.min(serverProgress" in source
    assert ".progress span.active" in css and "update-progress-flow" in css


def test_main_sync_builds_exact_tree_without_base_tree(monkeypatch):
    m = load_update_module()
    class Resp:
        def __init__(self, status, payload): self.status_code=status; self._payload=payload
        def json(self): return self._payload
    calls = []
    base_sha = 'a' * 40
    base_tree = 'b' * 40
    new_tree = 'c' * 40
    commit_sha = 'd' * 40
    blob_sha = 'e' * 40
    backup_ref = 'backup/before-v5.1.11-test'
    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == 'GET' and path.endswith('/git/ref/heads/main'):
            return Resp(200, {'object': {'sha': base_sha}})
        if method == 'GET' and f'/git/commits/{base_sha}' in path:
            return Resp(200, {'tree': {'sha': base_tree}})
        if method == 'GET' and f'/git/commits/{commit_sha}' in path:
            return Resp(200, {'tree': {'sha': new_tree}})
        if method == 'GET' and f'/git/trees/{new_tree}' in path:
            return Resp(200, {'truncated': False, 'tree': [
                {'path': 'keep.txt', 'type': 'blob', 'sha': blob_sha},
            ]})
        if method == 'GET' and f'/git/trees/{base_tree}' in path:
            return Resp(200, {'truncated': False, 'tree': [
                {'path': 'keep.txt', 'type': 'blob', 'sha': blob_sha},
                {'path': 'obsolete.md', 'type': 'blob', 'sha': blob_sha},
            ]})
        if method == 'POST' and path.endswith('/git/refs'):
            return Resp(201, {'ref': f'refs/heads/{backup_ref}'})
        if method == 'POST' and path.endswith('/git/blobs'):
            return Resp(201, {'sha': blob_sha})
        if method == 'POST' and path.endswith('/git/trees'):
            body = kwargs.get('json', {})
            assert 'base_tree' not in body
            tree_entries = body.get('tree', [])
            calls.append(('TREE_ENTRIES', '', {'json': {'tree': tree_entries}}))
            assert [e.get('path') for e in tree_entries] == ['keep.txt']
            return Resp(201, {'sha': new_tree})
        if method == 'POST' and path.endswith('/git/commits'):
            return Resp(201, {'sha': commit_sha, 'html_url': 'https://github.com/Menshikovivan/FargoVPN/commit/' + commit_sha})
        if method == 'PATCH' and path.endswith('/git/refs/heads/main'):
            return Resp(200, {'object': {'sha': commit_sha}})
        if method == 'DELETE' and path.endswith('/git/refs/heads/' + backup_ref):
            return Resp(204, {})
        raise AssertionError((method, path, kwargs))

    monkeypatch.setattr(m, 'github_request', fake_request)
    monkeypatch.setattr(m, '_github_create_backup_ref', lambda base, version: backup_ref)
    monkeypatch.setattr(m, '_github_delete_ref', lambda ref: None)
    monkeypatch.setattr(m, 'github_main_sync_enabled', lambda: True)
    monkeypatch.setattr(m, 'github_owner', lambda: 'Menshikovivan')
    monkeypatch.setattr(m, 'github_repo', lambda: 'FargoVPN')
    monkeypatch.setattr(m, 'safe_extract', lambda archive, target: target)

    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / 'release.tar.gz'
        archive.write_bytes(b'archive')
        monkeypatch.setattr(m, '_github_main_public_files', lambda *_args: {'keep.txt': b'new'})
        result = m._github_main_sync(archive, '5.1.11', hashlib.sha256(b'archive').hexdigest())
        assert result['stale_count'] == 1
        assert result['stale_paths_removed'] == ['obsolete.md']


def test_repository_root_is_minimal():
    repo = ROOT.parent
    files = sorted(p.name for p in repo.iterdir() if p.is_file())
    assert files == ["LICENSE", "README.md", "diagnose.sh", "install.sh"]


def test_publish_progress_contract():
    manager=(ROOT/"update_manager.py").read_text(encoding="utf-8")
    web=(ROOT/"webapp.py").read_text(encoding="utf-8")
    worker=(ROOT/"publish_worker.py").read_text(encoding="utf-8")
    assert "def start_publish_job" in manager and "def read_publish_status" in manager
    assert "/api/updates/publish-status" in web
    assert "startPublishPolling" in web
    assert "publish_update(archive, original_name, progress=progress)" in worker
    assert '<button id="publish-button" type="submit">' in web
    assert 'data-no-navigation="1"' in web
    assert "xhr.upload.onprogress" in web
    assert "X-Requested-With','XMLHttpRequest'" in web
    assert "/api/updates/publish-log" in web
    start=manager.index('def start_publish_job'); end=manager.index('def publish_update')
    assert "github_validate_configuration()" not in manager[start:end]
    assert "publish_log_tail" in manager
    assert 'publish-cancel-button' in web
    installer=(ROOT/'install.sh').read_text(encoding='utf-8')
    assert 'nginx_panel_guard.py" --once' in installer
    assert 'proxy_hide_header Content-Security-Policy' in (ROOT/'nginx_panel_guard.py').read_text(encoding='utf-8')

def test_update_completion_refresh_contract():
    web=(ROOT/"webapp.py").read_text(encoding="utf-8")
    assert "watchedJob=currentJob" in web
    assert "window.location.replace(purl('/updates')" in web




def test_update_worker_launch_contract_and_changelog_history():
    manager=(ROOT/"update_manager.py").read_text(encoding="utf-8")
    worker=(ROOT/"update_worker.py").read_text(encoding="utf-8")
    publish_worker=(ROOT/"publish_worker.py").read_text(encoding="utf-8")
    web=(ROOT/"webapp.py").read_text(encoding="utf-8")
    assert '"--startup-delay"' not in manager[manager.index("def start_update_job"):manager.index("def _package_installer")]
    assert "time.monotonic() + 6.0" in manager
    assert "update-launcher.log" in manager
    assert 'def run(job_id: str, startup_delay: float = 0.0)' in worker
    assert 'progress=3' in publish_worker and 'phase="startup"' in publish_worker
    assert 'time.monotonic() + 6.0' in manager[manager.index('def start_publish_job'):manager.index('def publish_update')]
    assert "def changelog_history(" in manager
    assert "История изменений предыдущих версий" in web



def test_logs_page_uses_external_prefix_aware_controller():
    web=(ROOT/"webapp.py").read_text(encoding="utf-8")
    logs=(ROOT/"static/logs.js").read_text(encoding="utf-8")
    block=web.split('@app.get("/logs", response_class=HTMLResponse)',1)[1]
    block=block.split('def logs_api',1)[0]
    assert '/static/logs.js' in block
    assert 'public_path("/static/logs.js")' in block
    assert 'logs-service' in block and 'logs-output' in block
    assert 'Файл не найден' in logs and 'Нет прав' in logs and 'Журнал пуст' in logs


def test_updates_inline_javascript_is_syntactically_valid():
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        return
    source = (ROOT / "webapp.py").read_text(encoding="utf-8")
    start = source.index("    script = r'''<script>", source.index('@app.get("/updates"'))
    end = source.index("</script>'''.replace", start)
    script = source[start:end].split("<script>\n", 1)[1]
    replacements = {
        "__INITIAL__": "{}",
        "__PAGE_VERSION__": '"5.1.11"',
        "__PUBLISHER__": "true",
        "__BASE_PATH__": '""',
    }
    for needle, replacement in replacements.items():
        script = script.replace(needle, replacement)
    path = Path(tempfile.mkdtemp()) / "updates.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "'health-check':'Проверка запуска'" in script
    assert "health-check: 'Проверка запуска'" not in script


def test_github_publish_bootstrap_is_panel_driven():
    manager = (ROOT / "update_manager.py").read_text(encoding="utf-8")
    web = (ROOT / "webapp.py").read_text(encoding="utf-8")
    assert "def github_configuration_ready" in manager
    assert "не настроен: откройте «Настройки GitHub»" in manager
    assert "def can_configure_github" in web
    assert "/settings" in web and "Настройки GitHub" in web
    assert "Настройки GitHub" in web


def test_public_bootstrap_downloads_verified_release_package():
    bootstrap = (ROOT.parent / "install.sh").read_text(encoding="utf-8")
    # The publisher may keep main/install.sh release-independent while FULL
    # archives can also be installed directly from their embedded app tree.
    assert "releases/latest/download" in bootstrap
    assert "FargoVPN_FULL.tar.gz" in bootstrap
    assert "sha256sum -c" in bootstrap
    assert "app/install.sh" in bootstrap
    if 'LOCAL_INSTALLER="$SCRIPT_DIR/app/install.sh"' in bootstrap:
        assert 'LOCAL_VERSION_FILE="$SCRIPT_DIR/app/VERSION"' in bootstrap
        assert 'exec /bin/bash "$LOCAL_INSTALLER" "$@"' in bootstrap
        assert 'FARGOVPN_USE_REMOTE_LATEST' in bootstrap
    else:
        assert 'INSTALLER="$PACKAGE_ROOT/app/install.sh"' in bootstrap


def test_publish_worker_ack_is_persistent():
    worker=(ROOT/"publish_worker.py").read_text(encoding="utf-8")
    assert 'phase="startup"' in worker
    assert 'progress=3' in worker
    manager=(ROOT/"update_manager.py").read_text(encoding="utf-8")
    assert 'launcher_log=str(launcher_log)' in manager
    assert 'GitHub publisher worker не подтвердил запуск' in manager


def test_console_installer_timeout_is_self_contained_and_github_write_free():
    installer=(ROOT / "install.sh").read_text(encoding="utf-8")
    assert "def run_with_timeout()" in installer or "run_with_timeout() {" in installer
    assert "apt_install coreutils" in installer
    assert "timeout --kill-after" not in installer
    assert "--sync-main-directory" not in installer
    assert "git push" not in installer
    assert "gh release create" not in installer
    assert "gh release upload" not in installer
    # The critical XUI smoke must use the internal timeout helper rather than
    # assuming a standalone GNU timeout binary exists.
    assert 'run_timed 45 "проверка clients/list, inbounds/list и server/status через 3x-ui API"' in installer


def test_installer_release_version_matches_package():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "5.1.19"
    assert (ROOT / "static" / "VERSION").read_text(encoding="utf-8").strip() == "5.1.19"
    installer=(ROOT / "install.sh").read_text(encoding="utf-8")
    assert "5.1.19 всегда используется полный профиль" in installer


def test_messages_generated_js_has_safe_path_placeholders_and_no_nested_public_path_511():
    web=(ROOT/"webapp.py").read_text(encoding="utf-8")
    block=web[web.index('def messages_page('):web.index('@app.get("/users/new"', web.index('def messages_page('))]
    assert 'messages_path_js = json.dumps(public_path("/messages"))' in block
    assert 'messages_feed_path_js = json.dumps(public_path("/api/panel/messages/feed"))' in block
    assert "__MESSAGES_PATH__" in block
    assert "__MESSAGES_FEED_PATH__" in block
    assert "new URL('{public_path('/api/panel/messages/feed')}'" not in block


def test_logs_page_loads_data_asynchronously_and_updates_url_without_reload_511():
    web=(ROOT/"webapp.py").read_text(encoding="utf-8")
    logs=(ROOT/"static/logs.js").read_text(encoding="utf-8")
    marker='@app.get("/logs", response_class=HTMLResponse)'
    block=web[web.index(marker):web.index('def replace_assignment', web.index(marker))]
    assert 'initial_text = ""' in block
    assert 'public_path("/static/logs.js")' in block
    assert "history.replaceState" in logs
    assert "select.addEventListener('change',load)" in logs
    assert 'apiFetch' in logs and 'AbortSignal.timeout' in logs


def test_github_auth_check_has_ajax_json_contract_511():
    web=(ROOT/"webapp.py").read_text(encoding="utf-8")
    marker='@app.post("/updates/github/test")'
    block=web[web.index(marker):web.index('@app.post("/updates/github/config")', web.index(marker))]
    assert 'wants_json =' in block
    assert 'return JSONResponse(payload, status_code=200)' in block
    assert 'return JSONResponse({"ok": False' in block
    assert 'github_validate_configuration()' in block
