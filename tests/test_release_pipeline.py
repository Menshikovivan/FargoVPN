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
    assert [(ROOT / name).read_text(encoding="utf-8").strip() for name in ("VERSION", "static/VERSION")] == ["5.1.6"] * 2
    assert "## 5.1.6" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")[:1000]

def test_public_surface_filters_runtime_material():
    m=load_update_module()
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        (root/"app").mkdir()
        (root/"app/VERSION").write_text("5.1.6\n")
        (root/"install.sh").write_text("#!/bin/bash\n")
        (root/"README.md").write_text("# FargoVPN\n")
        (root/"LICENSE").write_text("license\n")
        (root/"old.zip").write_bytes(b"zip")
        (root/"secret.pem").write_text("PRIVATE")
        (root/".env").write_text("TOKEN=x\n")
        files=m._github_main_public_files(root, root/"x.tar.gz", "5.1.6", hashlib.sha256(b"x").hexdigest())
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
    backup_ref = 'backup/before-v5.1.6-test'
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
        result = m._github_main_sync(archive, '5.1.6', hashlib.sha256(b'archive').hexdigest())
        assert result['stale_count'] == 1
        assert result['stale_paths_removed'] == ['obsolete.md']


def test_repository_root_is_minimal():
    repo = ROOT.parent
    files = sorted(p.name for p in repo.iterdir() if p.is_file())
    assert files == ["LICENSE", "README.md", "install.sh"]


def test_publish_progress_contract():
    manager=(ROOT/"update_manager.py").read_text(encoding="utf-8")
    web=(ROOT/"webapp.py").read_text(encoding="utf-8")
    worker=(ROOT/"publish_worker.py").read_text(encoding="utf-8")
    assert "def start_publish_job" in manager and "def read_publish_status" in manager
    assert "/api/updates/publish-status" in web
    assert "startPublishPolling" in web
    assert "publish_update(archive, original_name, progress=progress)" in worker

def test_update_completion_refresh_contract():
    web=(ROOT/"webapp.py").read_text(encoding="utf-8")
    assert "watchedJob=currentJob" in web
    assert "window.location.replace(purl('/updates')" in web


