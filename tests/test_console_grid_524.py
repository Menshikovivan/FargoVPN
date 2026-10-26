"""Release integration: public brand assets and launcher icons keep URL prefixes."""
import ast
import hashlib
import html
from pathlib import Path
import re
import struct
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

class Response:
    def __init__(self, data):
        self.data = data
        self.headers = {}


def load_shell(prefix):
    tree = ast.parse((ROOT / "app/webapp.py").read_text(encoding="utf-8"))
    names = {"manifest_webmanifest", "cabinet_manifest", "_brand_mark", "navigation"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    env = dict(html=html, JSONResponse=Response,
               config=SimpleNamespace(SERVICE_NAME="FargoVPN", DB_PATH="qa-only", XUI_PANEL_URL="https://panel.example.test"),
               update_manager=SimpleNamespace(current_version=lambda: (ROOT / "app/VERSION").read_text().strip()),
               public_path=lambda path: prefix + path,
               user_events=SimpleNamespace(unread_messages_summary=lambda **kw: {"total": 3}),
               payment_attention_count=lambda: 2)
    exec(compile(ast.fix_missing_locations(module), "actual-brand-shell", "exec"), env)
    return env


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_pwa_uses_existing_square_icons_for_both_url_prefixes():
    for prefix in ("", "/panel"):
        env = load_shell(prefix)
        for fn in ("manifest_webmanifest", "cabinet_manifest"):
            manifest = env[fn]().data
            assert manifest["display"] == "standalone"
            assert manifest["id"].startswith(prefix + "/")
            assert {i["purpose"] for i in manifest["icons"]} == {"any", "maskable"}
            for icon in manifest["icons"]:
                assert icon["src"].startswith(prefix + "/static/icons/fargo-")
                relative = icon["src"][len(prefix):].lstrip("/")
                path = ROOT / "app" / relative
                width, height = png_size(path)
                assert f"{width}x{height}" == icon["sizes"] and width == height


def test_brand_image_and_grouped_navigation_keep_destinations_and_badges():
    for prefix in ("", "/panel"):
        env = load_shell(prefix)
        mark = env["_brand_mark"]()
        assert prefix + "/static/fargo-logo.png?v=" in mark
        assert png_size(ROOT / "app/static/fargo-logo.png") == (512, 512)
        nav = env["navigation"]("payments")
        assert nav.count('class="nav-group"') == 3
        for path in ("/", "/users", "/subscription-tools", "/messages", "/broadcast", "/payments", "/monitoring", "/backups", "/logs", "/diagnostics", "/updates", "/audit", "/reminders", "/settings", "/profile", "/logout"):
            assert nav.count('href="' + prefix + path + '"') == 1
        assert 'href="https://panel.example.test"' in nav
        assert "data-unread-total" in nav and "data-payment-attention" in nav
        assert nav.index('href="' + prefix + '/profile"') > nav.index('class="aside-foot"')


def test_apple_launcher_icon_and_notification_icons_contain_new_artwork():
    assert png_size(ROOT / "app/static/icons/apple-touch-icon-v524.png") == (180, 180)
    for size in (192, 512):
        a = (ROOT / f"app/static/icons/icon-{size}.png").read_bytes()
        b = (ROOT / f"app/static/icons/fargo-{size}-v524.png").read_bytes()
        assert hashlib.sha256(a).digest() == hashlib.sha256(b).digest()
    source = (ROOT / "app/webapp.py").read_text(encoding="utf-8")
    assert 'apple-touch-icon-v524.png' in source and 'sizes="180x180"' in source


def test_live_monitoring_svg_fills_equal_plot_tracks():
    import os
    from playwright.sync_api import sync_playwright
    markup = '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="fargovpn-canonical-path" content="/"></head><body class="panel-page"><main><div class="monitoring-grid">'
    for ident, title in (("monitor-chart", "CPU сервера"), ("monitor-xui-cpu-chart", "CPU 3x-ui"), ("monitor-speed-chart", "Сеть 3x-ui")):
        markup += f'<section class="card chart-card"><h2>{title}</h2><div class="chart" id="{ident}"></div><div class="chart-caption-slot"></div></section>'
    markup += '</div></main></body></html>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("FARGOVPN_CHROMIUM") or pw.chromium.executable_path, args=["--no-sandbox"])
        for width in (390, 1440):
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.set_content(markup)
            for name in ("panel.css", "console-grid.css", "console-mobile.css"):
                page.add_style_tag(content=(ROOT / "app/static" / name).read_text(encoding="utf-8"))
            page.evaluate("""() => {window.fetch=async()=>({ok:true,json:async()=>({cpu:25,count:1,status:{cpu:20,xray_state:'running',net_down_speed:1024,net_up_speed:512},nodes:[]})})}""")
            page.add_script_tag(content=(ROOT / "app/static/monitoring.js").read_text(encoding="utf-8"))
            page.wait_for_function("document.querySelectorAll('.chart > svg').length===3")
            metrics = page.locator('.chart').evaluate_all("""els=>els.map(e=>{const r=e.querySelector('svg').getBoundingClientRect();return [e.clientWidth,e.clientHeight,r.width,r.height]})""")
            assert max(x[1] for x in metrics)-min(x[1] for x in metrics)<1
            for client_width, client_height, svg_width, svg_height in metrics:
                assert client_height > 50
                assert abs(client_width-svg_width)<1 and abs(client_height-svg_height)<1, (width, metrics)
            page.close()
        browser.close()
