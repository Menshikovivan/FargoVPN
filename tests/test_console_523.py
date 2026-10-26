"""Console integration: prefix-safe stylesheet URLs and PWA asset delivery."""
import ast
from pathlib import Path
import hashlib
import html
import re
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

def _namespace(prefix):
    source = ast.parse((ROOT / "app/webapp.py").read_text(encoding="utf-8"))
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in {"page", "_panel_asset_version"}]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    env = dict(html=html, re=re, hashlib=hashlib, APP_DIR=ROOT / "app",
               update_manager=SimpleNamespace(current_version=lambda: (ROOT / "app/VERSION").read_text().strip()),
               config=SimpleNamespace(SERVICE_NAME="FargoVPN"),
               public_path=lambda path: prefix + path, update_banner=lambda: "", pop_flash=lambda request: "",
               navigation=lambda active: "", session_csrf_token=lambda request: "qa-csrf")
    exec(compile(ast.fix_missing_locations(module), "actual-panel-shell", "exec"), env)
    return env


def test_console_mobile_asset_is_linked_for_prefixed_and_root_panels():
    for prefix in ("", "/panel"):
        env = _namespace(prefix)
        for active in ("", "messages"):
            document = env["page"](None, "Тест", '<form><input id="q" name="q"></form>', active)
            links = re.findall(r'<link rel="stylesheet" href="([^"?]+)', document)
            assert links == [prefix + "/static/panel.css", prefix + "/static/console-mobile.css"]
            assert document.index("panel.css") < document.index("console-mobile.css")
            assert '<input id="q" name="q">' in document
            assert 'viewport-fit=cover' in document


def test_console_mobile_asset_participates_in_cache_fingerprint(tmp_path):
    env = _namespace("")
    static = tmp_path / "static"
    static.mkdir()
    css = static / "console-mobile.css"
    css.write_text("a{}")
    env["APP_DIR"] = tmp_path
    before = env["_panel_asset_version"]()
    css.write_text("a{min-height:44px}")
    assert env["_panel_asset_version"]() != before


def test_service_worker_precaches_the_console_layout_asset():
    worker = (ROOT / "app/service-worker.js").read_text(encoding="utf-8")
    assert '`${BASE}static/console-mobile.css?v=${VERSION}`' in worker
    assert (ROOT / "app/static/console-mobile.css").is_file()
