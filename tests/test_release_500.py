import importlib.util
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(name): return (ROOT / name).read_text(encoding="utf-8")

def load_update_module():
    cfg = types.ModuleType("config"); cfg.GITHUB_REPOSITORY_OWNER="Menshikovivan"; cfg.GITHUB_REPOSITORY_NAME="FargoVPN"; cfg.GITHUB_RELEASE_ASSET_NAME=""; sys.modules["config"]=cfg
    dj = types.ModuleType("detached_jobs"); dj.DetachedJobError=RuntimeError; dj.launch_detached=lambda *a,**k:None; sys.modules["detached_jobs"]=dj
    spec=importlib.util.spec_from_file_location("um500", ROOT/"update_manager.py"); mod=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(mod); return mod

def test_versions():
    assert read("VERSION").strip()=="5.0.5"; assert read("app/VERSION").strip()=="5.0.5"; assert read("static/VERSION").strip()=="5.0.5"; assert read("README.md").startswith("# FargoVPN 5.0.5")

def test_no_legacy_profile():
    files=[p for p in ROOT.rglob("*") if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts and p.suffix not in {".db", ".sqlite", ".sqlite3", ".log", ".pyc", ".tar", ".gz"}]
    payload="\n".join(p.read_text(encoding="utf-8",errors="ignore") for p in files)
    legacy = "li" + "te"
    assert not re.search(rf"(?i)(^|[^a-z]){legacy}([^a-z]|$)", payload)
    assert not (ROOT/("requirements-" + legacy + ".txt")).exists()

def test_install_disk_check():
    src=read("install.sh")
    assert 'check_disk_space "$TARGET" "целевого каталога"' in src
    assert 'check_disk_space "$PACKAGE_ROOT" "временного каталога установщика"' in src
    assert 'df -Pk "$PACKAGE_ROOT"' not in src
    assert 'mkdir -p -- "$path"' in src

def test_bootstrap_and_command():
    m=load_update_module(); b=m._github_main_bootstrap(); assert 'curl -fsSL' in b; assert ("--" + "profile") not in b; assert '/var/tmp' in b; assert 'sha256sum -c' in b; assert 'choose_tmp_base' in b; assert 'apt-get install' in b; assert 'STATUS=$?' in b; assert 'exec /bin/bash' not in b; assert m.github_install_command().endswith('install.sh | sudo bash')

def test_release_notes_and_assets():
    m=load_update_module(); notes=m.github_notes('## 5.0.0\nNEW\n\n## 4.9.3\nOLD','5.0.0','a'*64,1); assert 'NEW' in notes and 'OLD' not in notes; assert '### Установка' in notes; assert '### Обновление' in notes; assert m.github_asset_name('5.0.0','wrong.tar.gz')=='VPN_Service_Platform_5.0.0_FULL.tar.gz'

def test_diagnostics_and_ui_contracts():
    d=read("diagnostics.py"); w=read("webapp.py"); j=read("static/panel.js"); assert 'sqlite3' not in d and 'PRAGMA' not in d and 'pg_database_size(current_database())' in d; assert '"registration"' in w and 'Дата регистрации' in w; assert 'data-broadcast-form' in w and 'initBroadcastForm' in j and 'stopImmediatePropagation' in j and 'Скачать JSON-отчёт' in w
