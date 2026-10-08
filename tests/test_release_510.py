import hashlib
import importlib.util
import sys
import tarfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load_update_module():
    cfg = types.ModuleType("config")
    cfg.GITHUB_REPOSITORY_OWNER = "Menshikovivan"
    cfg.GITHUB_REPOSITORY_NAME = "FargoVPN"
    cfg.GITHUB_RELEASE_ASSET_NAME = "VPN_Service_Platform_{version}_FULL.tar.gz"
    cfg.GITHUB_RELEASE_TAG_PREFIX = "v"
    dj = types.ModuleType("detached_jobs")
    dj.DetachedJobError = RuntimeError
    dj.launch_detached = lambda *a, **k: None
    sys.modules["config"] = cfg
    sys.modules["detached_jobs"] = dj
    spec = importlib.util.spec_from_file_location("um510", ROOT / "update_manager.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_single_version_contract():
    values = [(ROOT / name).read_text(encoding="utf-8").strip() for name in ("VERSION", "app/VERSION", "static/VERSION")]
    assert values == ["5.1"] * 3
    assert "Текущая версия: `5.1`" in (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "\n## 5.1" in changelog[:500]
## 5.1" in changelog[:500]
    for heading in ("### Added", "### Changed", "### Fixed", "### Removed"):
        assert heading in changelog


def test_main_public_surface_excludes_release_archives():
    m = load_update_module()
    class Root:
        def __init__(self, path): self.path = Path(path)
        def rglob(self, pattern): return self.path.rglob(pattern)
    # Use a real temporary tree via pytest's tmp_path in a nested helper.
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp)
        (source / "VERSION").write_text("5.1\n")
        (source / "install.sh").write_text("#!/bin/bash\n")
        (source / "README.md").write_text("# FargoVPN\n")
        (source / "FargoVPN_FULL.tar.gz").write_bytes(b"archive")
        (source / "private.pem").write_text("DUMMY-PRIVATE-KEY-PLACEHOLDER\n")
        files = m._github_main_public_files(source, source / "FargoVPN_FULL.tar.gz", "5.1", hashlib.sha256(b"archive").hexdigest())
        assert "install.sh" in files
        assert "FargoVPN_FULL.tar.gz" not in files
        assert "private.pem" not in files
        assert "VERSION" in files
        bootstrap = files["install.sh"].decode()
        assert "releases/latest/download" in bootstrap
        assert "FargoVPN_FULL.tar.gz" in bootstrap


def test_release_tag_defaults_to_v():
    m = load_update_module()
    assert m.github_release_tag("5.1") == "v5.1"


def test_install_uses_real_version_and_safe_sync():
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'releases/latest/download' in installer and 'PACKAGE_ROOT' in installer
    assert 'sha256sum -c' in installer
    assert 'VERSION="4.0"' not in installer
    assert 'FARGOVPN_ARCHIVE_URL' in installer
    assert 'FARGOVPN_CHECKSUM_URL' in installer
    assert 'set +e' in installer and 'STATUS=$?' in installer


def test_config_example_contains_no_demo_credentials():
    config = (ROOT / "config.example.py").read_text(encoding="utf-8")
    assert 'BOT_TOKEN = ""' in config
    assert 'MASTER_API_TOKEN = ""' in config
    assert 'WEB_PASSWORD_HASH = ""' in config
    assert 'GITHUB_RELEASE_TAG_PREFIX = "v"' in config
    assert 'GITHUB_REPOSITORY_TOPICS' in config
    assert "123456:telegram-bot-token" not in config


def test_public_gitignore_blocks_runtime_material():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in ("*.db", "*.sqlite", "*.sqlite3", "*.tar.gz", ".env", "*.pem", "*.key", ".idea/", ".vscode/", "__pycache__/"):
        assert entry in ignore
    assert "!FargoVPN_FULL.tar.gz" not in ignore


def test_github_publisher_has_transactional_backup_and_repo_setup():
    source = (ROOT / "update_manager.py").read_text(encoding="utf-8")
    assert "_github_create_backup_ref" in source
    assert "_github_restore_main" in source
    assert "_github_configure_repository" in source
    assert '"tree": tree_sha' in source and '"parents": [base_sha]' in source
    assert '"force": False' in source
    assert '"has_discussions": True' in source
    assert '"delete_branch_on_merge": True' in source
    assert 'topics_response = github_request("PUT", base + "/topics"' in source
    assert 'Тег {tag} уже существует' in source


def test_public_tree_is_rebuilt_without_release_archives_or_runtime_state():
    m = load_update_module()
    import tempfile
    import hashlib
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "VERSION").write_text("5.1\n")
        (root / "install.sh").write_text("#!/bin/bash\n")
        (root / "app").mkdir()
        (root / "app" / "main.py").write_text("print('ok')\n")
        (root / ".env").write_text("SECRET=x\n")
        (root / "bot.db").write_bytes(b"db")
        (root / "old.zip").write_bytes(b"zip")
        files = m._github_main_public_files(root, root / "old.tar.gz", "5.1", hashlib.sha256(b'x').hexdigest())
        assert set(files) == {"VERSION", "install.sh", "app/main.py"}
