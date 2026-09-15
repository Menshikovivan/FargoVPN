import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import httpx

import backup
import update_manager
import webapp


def test_public_prefix_routes_and_service_worker(client):
    prefix = webapp.public_prefix()
    response = client.get(prefix + '/login')
    assert response.status_code == 200
    assert f'action="{prefix}/login"' in response.text
    sw = client.get(prefix + '/service-worker.js')
    assert sw.status_code == 200
    assert f"const BASE = '{prefix}/';" in sw.text
    assert 'Service-Worker-Allowed' in sw.headers


def test_chat_markup_and_bottom_scroll_script(monkeypatch):
    monkeypatch.setattr(webapp, "require_auth", lambda request: None)
    from starlette.requests import Request
    scope={"type":"http","method":"GET","path":"/messages","headers":[],"query_string":b"", "session": {"auth": True, "user": "admin"}}
    response = webapp.messages_page(Request(scope))
    assert 'class="chat-window"' in str(response)
    js = Path('static/panel.js').read_text(encoding='utf-8')
    assert '.chat-window' in js
    assert 'MutationObserver' in js
    assert 'container.scrollTop = container.scrollHeight' in js


def test_yandex_uploader_reports_disabled_state(tmp_path):
    archive = tmp_path / 'a.tar.gz'
    archive.write_bytes(b'archive')
    old = backup.config.YANDEX_DISK_ENABLED
    try:
        backup.config.YANDEX_DISK_ENABLED = False
        ok, detail = backup.upload_to_yandex(archive)
        assert ok is False
        assert 'выключена' in detail.lower()
    finally:
        backup.config.YANDEX_DISK_ENABLED = old


def test_github_release_history_paginates(monkeypatch):
    calls = []
    def fake_request(method, path, **kwargs):
        calls.append(path)
        page = 1 if 'page=1' in path else 2
        base = 100 - page
        payload = []
        for i in range(100 if page == 1 else 3):
            v = f'9.0.{base-i}'
            assets = [] if (page == 1 and i % 2) else [{'name': f'VPN_Service_Platform_{v}_FULL.tar.gz', 'size': 10, 'digest': 'sha256:'+'a'*64}]
            payload.append({'draft': False, 'tag_name': f'FargoVPN-{v}', 'name': f'FargoVPN {v}',
                            'assets': assets, 'published_at': '2026-09-15T00:00:00Z', 'body': ''})
        return httpx.Response(200, json=payload, request=httpx.Request(method, 'https://api.github.test'))
    monkeypatch.setattr(update_manager, "github_request", fake_request)
    monkeypatch.setattr(update_manager, "github_owner", lambda: "Menshikovivan")
    monkeypatch.setattr(update_manager, "github_repo", lambda: "FargoVPN")
    result = update_manager.github_release_history(60)
    assert len(result) == 60
    assert any('page=2' in path for path in calls)


def test_backup_create_is_detached(monkeypatch, tmp_path):
    calls = {}
    def fake_launch(unit, command, **kwargs):
        calls.update(unit=unit, command=list(command), kwargs=kwargs)
        return 'test-detached'
    monkeypatch.setattr(webapp, 'launch_detached', fake_launch)
    with patch.object(webapp, 'require_auth', lambda request: None), patch.object(webapp, 'audit', lambda *a, **k: None), patch.object(webapp, 'public_path', lambda x: '/panel-test/backups'), patch.object(webapp, 'set_flash', lambda request, message, kind='good': request.session.__setitem__('flash', {'message': message, 'kind': kind})):
        request = webapp.Request({'type':'http','method':'POST','path':'/backups/create','headers':[], 'session': {'user':'admin'}})
        request.scope['session'] = {'user':'admin'}
        result = webapp.create_backup(request)
    assert result.status_code == 303
    assert calls['command'][-1] == '--force'
    assert calls['unit'].startswith('fargovpn-manual-backup-')


def test_push_encrypt_packet_shape(tmp_path):
    sub = {'keys': {}}
    from cryptography.hazmat.primitives.asymmetric import ec
    from push_service import _pub, b64u, _encrypt
    receiver = ec.generate_private_key(ec.SECP256R1())
    sub['keys']['p256dh'] = b64u(_pub(receiver))
    sub['keys']['auth'] = b64u(b'0123456789abcdef')
    packet = _encrypt(sub, b'hello')
    assert len(packet) > 16 + 4 + 1 + 65 + 16
    assert len(packet[:16]) == 16
    assert int.from_bytes(packet[16:20], 'big') == 4096
    assert packet[20] == 65
    assert len(packet[21:86]) == 65

def test_yandex_upload_end_to_end_against_local_http_api(monkeypatch, tmp_path):
    """Exercise the real uploader state machine against a local Disk-API-compatible test server."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from urllib.parse import parse_qs, urlsplit

    archive = tmp_path / 'vpn_service_full_backup_test.tar.gz'
    archive.write_bytes(b'FargoVPN-test-archive' * 1024)
    remote = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def _json(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_PUT(self):
            length = int(self.headers.get('Content-Length','0'))
            remote['bytes'] = self.rfile.read(length)
            self.send_response(201); self.end_headers()
        def do_GET(self):
            u = urlsplit(self.path)
            if u.path.endswith('/resources/upload'):
                href = f'http://127.0.0.1:{self.server.server_port}/upload'
                return self._json(200, {'href': href, 'method': 'PUT'})
            if u.path.endswith('/resources'):
                if remote.get('bytes') is None:
                    return self._json(404, {'message':'not found'})
                self._json(200, {'type':'file','size':len(remote['bytes'])})
                return
            self._json(200, {'total_space':10**9,'used_space':0})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    old_enabled = backup.config.YANDEX_DISK_ENABLED
    old_api = backup.YANDEX_API
    old_token = getattr(backup.config, 'YANDEX_DISK_TOKEN', '')
    try:
        backup.config.YANDEX_DISK_ENABLED = True
        backup.config.YANDEX_DISK_TOKEN = 'test-token'
        backup.YANDEX_API = f'http://127.0.0.1:{server.server_port}'
        monkeypatch.setattr(backup, 'ensure_yandex_directory', lambda client, directory: None)
        monkeypatch.setattr(backup.config, 'YANDEX_UPLOAD_VERIFY_DELAY', 0.0, raising=False)
        monkeypatch.setattr(backup.config, 'YANDEX_UPLOAD_RETRIES', 2, raising=False)
        ok, detail = backup.upload_to_yandex(archive)
        assert ok is True
        assert len(remote['bytes']) == archive.stat().st_size
        assert 'проверено' in detail.lower() or 'байт' in detail.lower()
    finally:
        backup.config.YANDEX_DISK_ENABLED = old_enabled
        backup.config.YANDEX_DISK_TOKEN = old_token
        backup.YANDEX_API = old_api
        server.shutdown(); server.server_close(); thread.join(timeout=2)

def test_push_ui_preserves_browser_diagnostics(monkeypatch):
    text = Path('webapp.py').read_text(encoding='utf-8')
    assert 'const clientLogs=[]' in text
    assert 'renderVisibleLogs' in text
    assert "limit=200" in text
    assert "БРАУЗЕРНЫЙ ЖУРНАЛ PUSH" in text


def test_settings_push_script_is_real_script_not_visible_text(client):
    from unittest.mock import patch
    request = None
    with patch.object(webapp, "require_auth", lambda request: None):
        response = client.get(webapp.public_prefix() + "/settings")
    assert response.status_code == 200
    text = response.text
    assert '<script>(function(){' in text
    assert 'const ps=document.getElementById(\'panel-push-status\')' in text
    assert '</script>' in text
    # The raw function body must not appear as a sibling text node outside script tags.
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, 'html.parser')
    visible = soup.body.get_text('\n', strip=True) if soup.body else ''
    assert 'const ps=document.getElementById' not in visible
    assert 'window.__fargovpnPushBooted' not in visible


def test_settings_push_routes_are_prefixed_and_protected(client):
    prefix = webapp.public_prefix()
    for method, path in [('get', '/api/panel/push/status'), ('get', '/api/panel/push/logs?limit=10')]:
        response = getattr(client, method)(prefix + path)
        assert response.status_code == 401
        assert 'detail' in response.json()


def test_login_json_sets_prefix_scoped_session_cookie(client, monkeypatch):
    monkeypatch.setattr(webapp.config, 'WEB_USERNAME', 'admin')
    monkeypatch.setattr(webapp.config, 'WEB_PASSWORD_HASH', 'plain-test-password')
    monkeypatch.setattr(webapp, 'verify_password', lambda password: password == 'secret')
    with patch.object(webapp.auth_security, 'check_login', lambda *a, **k: type('S', (), {'allowed': True, 'retry_after': 0})()), \
         patch.object(webapp.auth_security, 'record_success', lambda *a, **k: None), \
         patch.object(webapp.auth_security, 'prune', lambda *a, **k: None), \
         patch.object(webapp, 'audit', lambda *a, **k: None):
        r = client.post(webapp.public_prefix() + '/login', data={'username':'admin','password':'secret'}, headers={'Accept':'application/json','X-Requested-With':'XMLHttpRequest'})
    assert r.status_code == 200
    assert r.json()['ok'] is True
    set_cookie = r.headers.get('set-cookie','')
    assert f"path={webapp.public_prefix() or '/'}" in set_cookie.lower()
    assert 'secure' in set_cookie.lower()
    assert 'httponly' in set_cookie.lower()
    assert 'samesite=lax' in set_cookie.lower()

def test_settings_push_script_is_not_visible_text(client, monkeypatch):
    monkeypatch.setattr(webapp, "require_auth", lambda request: None)
    response = client.get(webapp.public_prefix() + "/settings")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    text = response.text
    assert "<script>(function(){" in text
    assert "})();</script>" in text
    # Extract only body text outside script/style tags without third-party parsers.
    import re
    visible = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.I | re.S)
    visible = re.sub(r"<style\b[^>]*>.*?</style>", "", visible, flags=re.I | re.S)
    visible = re.sub(r"<[^>]+>", " ", visible)
    assert "const ps=document.getElementById" not in visible
    assert "window.__fargovpnPushBooted" not in visible


def test_settings_push_calls_are_present_in_script(client, monkeypatch):
    monkeypatch.setattr(webapp, "require_auth", lambda request: None)
    text = client.get(webapp.public_prefix() + "/settings").text
    for endpoint in [
        "/api/panel/push/status",
        "/api/panel/push/logs?limit=200",
        "/api/panel/push/config",
        "/api/panel/push/subscribe",
        "/api/panel/push/unsubscribe",
        "/api/panel/push/test",
    ]:
        assert endpoint in text


def test_login_json_sets_prefix_scoped_secure_session_cookie(client, monkeypatch):
    monkeypatch.setattr(webapp.config, "WEB_USERNAME", "admin")
    monkeypatch.setattr(webapp.config, "WEB_PASSWORD_HASH", "not-used")
    monkeypatch.setattr(webapp, "verify_password", lambda password: password == "secret")
    class Allowed:
        allowed = True
        retry_after = 0
    monkeypatch.setattr(webapp.auth_security, "check_login", lambda *a, **k: Allowed())
    monkeypatch.setattr(webapp.auth_security, "record_success", lambda *a, **k: None)
    monkeypatch.setattr(webapp.auth_security, "prune", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "audit", lambda *a, **k: None)
    monkeypatch.setattr(webapp.config, "WEB_COOKIE_HTTPS_ONLY", True)
    r = client.post(
        webapp.public_prefix() + "/login",
        data={"username": "admin", "password": "secret"},
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    sc = r.headers.get("set-cookie", "")
    assert "httponly" in sc.lower()
    assert "secure" in sc.lower()
    assert "samesite=lax" in sc.lower()
    assert f"path={webapp.public_prefix()}" in sc.lower()


def test_pwa_manifest_matches_public_prefix(client):
    response = client.get(webapp.public_prefix() + "/manifest.webmanifest")
    assert response.status_code == 200
    payload = response.json()
    prefix = webapp.public_prefix() + "/"
    assert payload["start_url"] == prefix
    assert payload["scope"] == prefix
    assert payload["display"] == "standalone"

def test_public_prefix_redirect_is_not_doubled(client):
    response = client.get(webapp.public_prefix() + "/", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers.get("location", "")
    assert location == webapp.public_prefix() + "/login"
    assert webapp.public_prefix() + webapp.public_prefix() not in location


def test_nginx_guard_block_is_transparent():
    import nginx_panel_guard
    block = nginx_panel_guard.block('/fargovpn-admin-testpath', '/run/vpn-service/fargovpn.sock')
    assert 'proxy_redirect' not in block
    assert 'proxy_cookie_path' not in block
    assert 'proxy_set_header X-Forwarded-Prefix /fargovpn-admin-testpath;' in block


def test_rendered_settings_inline_scripts_are_javascript(monkeypatch):
    import re, shutil, subprocess, tempfile
    node = shutil.which('node')
    if not node:
        return
    monkeypatch.setattr(webapp, "require_auth", lambda request: None)
    response = client = __import__('starlette.testclient', fromlist=['TestClient']).TestClient(webapp.app).get(webapp.public_prefix() + '/settings')
    scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', response.text, flags=re.I | re.S)
    assert scripts
    with tempfile.TemporaryDirectory() as d:
        for i, source in enumerate(scripts):
            path = __import__('pathlib').Path(d) / f'script_{i}.js'
            path.write_text(source, encoding='utf-8')
            checked = subprocess.run([node, '--check', str(path)], capture_output=True, text=True)
            assert checked.returncode == 0, checked.stderr

def test_login_then_prefixed_home_keeps_session(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(webapp.config, 'WEB_USERNAME', 'admin')
    monkeypatch.setattr(webapp.config, 'WEB_PASSWORD_HASH', 'not-used')
    monkeypatch.setattr(webapp, 'verify_password', lambda password: password == 'secret')
    class Allowed:
        allowed = True
        retry_after = 0
    monkeypatch.setattr(webapp.auth_security, 'check_login', lambda *a, **k: Allowed())
    monkeypatch.setattr(webapp.auth_security, 'record_success', lambda *a, **k: None)
    monkeypatch.setattr(webapp.auth_security, 'prune', lambda *a, **k: None)
    monkeypatch.setattr(webapp, 'audit', lambda *a, **k: None)
    https_client = TestClient(webapp.app, base_url='https://testserver')
    response = https_client.post(
        webapp.public_prefix() + '/login',
        data={'username':'admin','password':'secret'},
        headers={'Accept':'application/json','X-Requested-With':'XMLHttpRequest'},
    )
    assert response.status_code == 200 and response.json()['ok'] is True
    home = https_client.get(webapp.public_prefix() + '/', follow_redirects=False)
    assert home.status_code == 200
    assert 'Панель' in home.text or 'Обзор' in home.text


def test_nginx_guard_removes_legacy_fargovpn_socket_location():
    from nginx_panel_guard import _strip_location_blocks
    legacy = """server {\nlocation ^~ /fargovpn-admin-testpath/ {\nproxy_pass http://unix:/run/vpn-service/fargovpn.sock:/;\nproxy_redirect / /fargovpn-admin-testpath/;\nproxy_cookie_path / /fargovpn-admin-testpath/;\nproxy_set_header X-Forwarded-Prefix /fargovpn-admin-testpath;\n}\nlocation / { return 200; }\n}"""
    import re
    def predicate(block):
        return bool(re.search(r"(?m)^\s*location\s+(?:=|\^~)\s+[^\s{]*fargovpn-admin[^\s{]*", block)) and bool(re.search(r"(?m)^\s*proxy_pass\s+", block))
    cleaned = _strip_location_blocks(legacy, predicate)
    assert 'proxy_redirect' not in cleaned
    assert 'proxy_cookie_path' not in cleaned
    assert '/fargovpn-admin-testpath/' not in cleaned

def test_guard_marker_regex_removes_plain_managed_location():
    import re, nginx_panel_guard
    legacy = """server {\n# BEGIN FARGOVPN MANAGED LOCATION\nlocation ^~ /fargovpn-admin-testpath/ {\nproxy_pass http://unix:/run/vpn-service/fargovpn.sock:/;\nproxy_redirect / /fargovpn-admin-testpath/;\nproxy_cookie_path / /fargovpn-admin-testpath/;\n}\n# END FARGOVPN MANAGED LOCATION\nlocation / { return 200; }\n}"""
    cleaned = re.sub(r"\n?\s*# BEGIN FARGOVPN(?:-[^\n]*)? MANAGED LOCATION.*?# END FARGOVPN(?:-[^\n]*)? MANAGED LOCATION\s*", "\n", legacy, flags=re.S)
    assert 'proxy_redirect' not in cleaned
    assert 'proxy_cookie_path' not in cleaned
    assert '/fargovpn-admin-testpath/' not in cleaned

def test_duplicate_prefix_request_redirects_to_canonical_prefixed_path(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(webapp, 'require_auth', lambda request: None)
    c = TestClient(webapp.app, base_url='https://testserver')
    dup = webapp.public_prefix() + webapp.public_prefix() + '/settings'
    r = c.get(dup, follow_redirects=False)
    assert r.status_code == 308
    assert r.headers.get('location') == webapp.public_prefix() + '/settings'

def test_release_builder_excludes_private_config_and_runtime_artifacts():
    from pathlib import Path
    s = Path('build_release.sh').read_text(encoding='utf-8')
    assert '--exclude="$PACKAGE_NAME/config.py"' in s
    assert '--exclude="$PACKAGE_NAME/.venv"' in s
    assert '--exclude="$PACKAGE_NAME/__pycache__"' in s

def test_public_path_never_duplicates_prefix(monkeypatch):
    monkeypatch.setattr(webapp.config, 'WEB_PUBLIC_PREFIX', '/fargovpn-admin-test')
    cases = [
        '/login',
        '/fargovpn-admin-test/login',
        '/fargovpn-admin-test/fargovpn-admin-test/login',
        '/fargovpn-admin-test/fargovpn-admin-test/fargovpn-admin-test/login',
        'login',
    ]
    for value in cases:
        result = webapp.public_path(value)
        assert result == '/fargovpn-admin-test/login'
        assert result.count('/fargovpn-admin-test') == 1


def test_nginx_guard_removes_legacy_fargovpn_location():
    from nginx_panel_guard import _insert_into_named_https_server, block
    old = '''server {\n    listen 127.0.0.1:9443 ssl proxy_protocol;\n    server_name example.com;\n    location = /fargovpn-admin-test { return 301 /fargovpn-admin-test/; }\n    location ^~ /fargovpn-admin-test/ {\n        proxy_pass http://127.0.0.1:8088;\n        proxy_redirect / /fargovpn-admin-test/;\n        proxy_cookie_path / /fargovpn-admin-test/;\n        proxy_set_header X-Forwarded-Prefix /fargovpn-admin-test;\n    }\n    location / { return 404; }\n}\n'''
    new = _insert_into_named_https_server(old, block('/fargovpn-admin-test','/run/vpn.sock'))
    assert new is not None
    assert new.count('location ^~ /fargovpn-admin-test/') == 1
    managed = new[new.index('location ^~ /fargovpn-admin-test/'):]
    assert 'proxy_redirect' not in managed.split('location / {',1)[0]
    assert 'proxy_cookie_path' not in managed.split('location / {',1)[0]
    assert 'proxy_pass http://unix:/run/vpn.sock:/' in managed

def test_public_prefix_does_not_double_in_rendered_pages(client, monkeypatch):
    monkeypatch.setattr(webapp.config, 'WEB_PUBLIC_PREFIX', '/fargovpn-admin-test')
    monkeypatch.setattr(webapp, 'require_auth', lambda request: None)
    paths = ['/settings', '/updates', '/diagnostics', '/backups', '/logs', '/messages', '/users', '/broadcast', '/payments', '/monitoring', '/subscription-tools']
    for path in paths:
        response = client.get('/fargovpn-admin-test' + path, follow_redirects=False)
        assert response.status_code < 500, (path, response.status_code)
        assert '/fargovpn-admin-test/fargovpn-admin-test' not in response.headers.get('location', '')
        if response.headers.get('content-type','').startswith('text/html'):
            assert '/fargovpn-admin-test/fargovpn-admin-test' not in response.text

def test_installer_uses_single_systemd_web_lifecycle_transaction():
    from pathlib import Path
    text = Path('install.sh').read_text(encoding='utf-8')
    assert 'os.kill(pid' not in text
    assert 'systemctl stop vpn-service-web.service vpn-service-web.socket' in text
    assert 'systemctl start vpn-service-web.socket' in text
    assert 'systemctl start vpn-service-web.service' in text
    assert 'systemctl restart vpn-service-web.service\n' not in text


def test_release_is_named_with_version_and_no_pycache_in_builder():
    from pathlib import Path
    builder = Path('build_release.sh').read_text(encoding='utf-8')
    assert 'FargoVPN-$version.tar.gz' in builder
    assert '--exclude="$PACKAGE_NAME/__pycache__"' in builder


def test_runtime_source_has_no_stale_frontend_version_literal():
    from pathlib import Path
    for path in Path('.').glob('*.py'):
        text = path.read_text(encoding='utf-8')
        assert '.'.join(['4','0','7']) not in text
    sw = Path('service-worker.js').read_text(encoding='utf-8')
    assert '__FARGOVPN_VERSION__' in sw

def test_updates_page_uses_prefixed_mutation_urls(client, monkeypatch):
    monkeypatch.setattr(webapp, "require_auth", lambda request: None)
    monkeypatch.setattr(webapp, "can_publish_update", lambda request: True)
    response = client.get(webapp.public_prefix() + "/updates")
    assert response.status_code == 200
    prefix = webapp.public_prefix()
    assert f'action="{prefix}/updates/publish"' in response.text
    assert f'action="{prefix}/updates/force-version"' in response.text
    # Rollback form is rendered only when a valid pre-update backup exists.
    assert "purl('/updates/publish')" in response.text
    assert "purl('/updates/apply')" in response.text


def test_github_upload_contract_uses_binary_and_length(monkeypatch, tmp_path):
    archive = tmp_path / 'release.tar.gz'
    archive.write_bytes(b'abc' * 1024)
    captured = {}
    class Resp:
        status_code = 201
        text = ''
        def json(self):
            return {'id': 88, 'browser_download_url': 'https://github.test/download/a.tar.gz', 'name': 'VPN_Service_Platform_4.0.21_FULL.tar.gz'}
    def fake_request(method, path, **kwargs):
        if method == 'GET' and '/releases/tags/' in path:
            return httpx.Response(404, json={'message':'Not Found'}, request=httpx.Request('GET','https://api.github.test'))
        if method == 'POST' and path.endswith('/releases'):
            return httpx.Response(201, json={'id': 88, 'upload_url':'https://uploads.github.test/repos/Menshikovivan/FargoVPN/releases/88/assets{?name,label}', 'assets':[]}, request=httpx.Request('POST','https://api.github.test'))
        raise AssertionError((method, path))
    def fake_post(url, **kwargs):
        captured.update(url=url, headers=kwargs['headers'], content=kwargs['content'], params=kwargs['params'])
        return Resp()
    monkeypatch.setattr(update_manager, 'github_request', fake_request)
    monkeypatch.setattr(update_manager.httpx, 'post', fake_post)
    monkeypatch.setattr(update_manager, 'inspect_archive', lambda p: {'version':'4.0.21'})
    monkeypatch.setattr(update_manager, '_read_changelog_from_archive', lambda *a: 'changes')
    monkeypatch.setattr(update_manager, 'github_owner', lambda: 'Menshikovivan')
    monkeypatch.setattr(update_manager, 'github_repo', lambda: 'FargoVPN')
    monkeypatch.setattr(update_manager, 'github_release_tag', lambda v: 'FargoVPN-'+v)
    monkeypatch.setattr(update_manager, 'github_release_name', lambda v: 'FargoVPN '+v)
    monkeypatch.setattr(update_manager, 'github_headers', lambda binary=False: {'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10'})
    monkeypatch.setattr(update_manager, 'sha256_file', lambda p: 'a'*64)
    out=update_manager.publish_update(archive, archive.name)
    assert out['github_release_id'] == 88
    assert captured['url'].startswith('https://uploads.github.test/')
    assert captured['headers']['Content-Length'] == str(archive.stat().st_size)
    assert captured['headers']['Content-Type'] == 'application/gzip'
    assert captured['headers']['Accept'] == 'application/vnd.github+json'
    assert captured['content'] == archive.read_bytes()


def test_nginx_loaded_conf_files_reads_nginx_T_markers(monkeypatch, tmp_path):
    import nginx_panel_guard
    cfg = tmp_path / "generated.conf"
    cfg.write_text("server {\n    listen 127.0.0.1:9443 ssl;\n    server_name example.com;\n}\n", encoding="utf-8")
    output = f"# configuration file {cfg}:\nserver {{\n}}\n"
    monkeypatch.setattr(nginx_panel_guard.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": output, "returncode": 0})())
    assert nginx_panel_guard._nginx_loaded_conf_files() == [cfg.resolve()]


def test_nginx_guard_transaction_validates_after_replace(monkeypatch, tmp_path):
    import nginx_panel_guard
    target = tmp_path / 'main.conf'
    target.write_text("server {\n    listen 127.0.0.1:9443 ssl;\n    server_name example.com;\n    location / { return 404; }\n}\n")
    monkeypatch.setattr(nginx_panel_guard, 'MAIN', target)
    values = {'WEB_REVERSE_PROXY': True, 'WEB_PUBLIC_PREFIX': '/fargovpn-admin-test', 'WEB_SOCKET_PATH': '/run/vpn.sock', 'WEB_DOMAIN': 'example.com'}
    monkeypatch.setattr(nginx_panel_guard, 'cfg', lambda name, default=None: values.get(name, default))
    monkeypatch.setattr(nginx_panel_guard, '_candidate_conf_files', lambda: [target])
    calls=[]
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        stdout = 'nginx: configuration file /etc/nginx/nginx.conf test is successful\n'
        if cmd[:2] == ['nginx','-T']:
            stdout += 'location ^~ /fargovpn-admin-test/ {\n    proxy_pass http://unix:/run/vpn.sock:/;\n}\n'
        return type('R', (), {'returncode':0, 'stdout':stdout, 'stderr':''})()
    monkeypatch.setattr(nginx_panel_guard.subprocess, 'run', fake_run)
    assert nginx_panel_guard.ensure_once() is True
    assert any(cmd[:2] == ['nginx','-t'] for cmd in calls)
    assert 'proxy_pass http://unix:/run/vpn.sock:/' in target.read_text()
    assert not target.with_name(target.name+'.fargovpn-backup').exists()


def test_github_upload_retries_405_on_canonical_upload_endpoint(monkeypatch, tmp_path):
    archive = tmp_path / 'release.tar.gz'; archive.write_bytes(b'payload')
    calls = []
    class Resp:
        def __init__(self, status): self.status_code=status; self.text=''; self.headers={}
        def json(self): return {'id': 99, 'browser_download_url':'https://github.test/a', 'name':'VPN_Service_Platform_4.0.21_FULL.tar.gz'}
    def fake_request(method, path, **kwargs):
        if method=='GET' and '/releases/tags/' in path: return httpx.Response(404, json={'message':'Not Found'}, request=httpx.Request('GET','https://api.github.test'))
        if method=='POST' and path.endswith('/releases'): return httpx.Response(201, json={'id':99,'upload_url':'https://uploads.github.test/repos/Menshikovivan/FargoVPN/releases/99/assets{?name,label}','assets':[]}, request=httpx.Request('POST','https://api.github.test'))
        raise AssertionError((method,path))
    def fake_post(url, **kwargs):
        calls.append(url)
        return Resp(405) if len(calls)==1 else Resp(201)
    monkeypatch.setattr(update_manager,'github_request',fake_request)
    monkeypatch.setattr(update_manager.httpx,'post',fake_post)
    monkeypatch.setattr(update_manager,'inspect_archive',lambda p:{'version':'4.0.21'})
    monkeypatch.setattr(update_manager,'_read_changelog_from_archive',lambda *a:'changes')
    monkeypatch.setattr(update_manager,'github_owner',lambda:'Menshikovivan')
    monkeypatch.setattr(update_manager,'github_repo',lambda:'FargoVPN')
    monkeypatch.setattr(update_manager,'github_release_tag',lambda v:'FargoVPN-'+v)
    monkeypatch.setattr(update_manager,'github_release_name',lambda v:'FargoVPN '+v)
    monkeypatch.setattr(update_manager,'github_headers',lambda binary=False:{'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10'})
    monkeypatch.setattr(update_manager,'sha256_file',lambda p:'a'*64)
    out=update_manager.publish_update(archive,archive.name)
    assert out['github_release_id']==99
    assert len(calls)==2
    assert '/releases/99/assets' in calls[1]


def test_github_upload_415_accepts_json_media_type(monkeypatch, tmp_path):
    archive = tmp_path / 'release.tar.gz'; archive.write_bytes(b'payload')
    seen = {}
    class Resp:
        status_code = 201
        text = ''
        def json(self):
            return {'id': 101, 'browser_download_url':'https://github.test/a', 'name':'VPN_Service_Platform_4.0.21_FULL.tar.gz'}
    def fake_request(method, path, **kwargs):
        if method == 'GET' and '/releases/tags/' in path:
            return httpx.Response(404, json={'message':'Not Found'}, request=httpx.Request('GET','https://api.github.test'))
        if method == 'POST' and path.endswith('/releases'):
            return httpx.Response(201, json={'id':101,'upload_url':'https://uploads.github.test/repos/Menshikovivan/FargoVPN/releases/101/assets{?name,label}','assets':[]}, request=httpx.Request('POST','https://api.github.test'))
        raise AssertionError((method, path))
    def fake_post(url, **kwargs):
        seen.update(kwargs.get('headers', {}))
        return Resp()
    monkeypatch.setattr(update_manager, 'github_request', fake_request)
    monkeypatch.setattr(update_manager.httpx, 'post', fake_post)
    monkeypatch.setattr(update_manager, 'inspect_archive', lambda p: {'version':'4.0.21'})
    monkeypatch.setattr(update_manager, '_read_changelog_from_archive', lambda *a:'changes')
    monkeypatch.setattr(update_manager, 'github_owner', lambda:'Menshikovivan')
    monkeypatch.setattr(update_manager, 'github_repo', lambda:'FargoVPN')
    monkeypatch.setattr(update_manager, 'github_release_tag', lambda v:'FargoVPN-'+v)
    monkeypatch.setattr(update_manager, 'github_release_name', lambda v:'FargoVPN '+v)
    monkeypatch.setattr(update_manager, 'sha256_file', lambda p:'a'*64)
    update_manager.publish_update(archive, archive.name)
    assert seen['Accept'] == 'application/vnd.github+json'
    assert seen['Content-Type'] == 'application/gzip'


def test_force_version_json_contract(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(webapp, 'require_auth', lambda request: None)
    monkeypatch.setattr(webapp, 'update_manager', webapp.update_manager)
    monkeypatch.setattr(webapp.update_manager, 'github_release_by_version', lambda v: {'version':'4.0.20'})
    monkeypatch.setattr(webapp.update_manager, 'current_version', lambda:'4.0.21')
    monkeypatch.setattr(webapp.update_manager, 'version_key', lambda v: tuple(int(x) for x in v.split('.')))
    monkeypatch.setattr(webapp.update_manager, 'start_update_job', lambda *a, **k: {'job_id':'job-json'})
    monkeypatch.setattr(webapp.update_manager, 'read_status', lambda: {'state':'queued','job_id':'job-json','progress':3})
    monkeypatch.setattr(webapp, 'audit', lambda *a, **k: None)
    c=TestClient(webapp.app)
    r=c.post(webapp.public_prefix()+'/updates/force-version', data={'version':'4.0.20','confirm_downgrade':'1'}, headers={'Accept':'application/json'})
    assert r.status_code==202
    assert r.headers['content-type'].startswith('application/json')
    assert r.json()['ok'] is True


def test_github_binary_download_uses_octet_stream_and_preserves_archive_size(monkeypatch, tmp_path):
    archive = tmp_path / 'VPN_Service_Platform_4.0.25_FULL.tar.gz'
    payload = b'not-really-a-tar-gz-for-http-test' * 1024
    archive.write_bytes(payload)
    seen = {}

    class FakeResponse:
        status_code = 200
        headers = {'content-length': str(len(payload)), 'content-type': 'application/gzip'}
        def raise_for_status(self):
            return None
        def iter_bytes(self, _size):
            yield payload[:100]
            yield payload[100:]

    class StreamContext:
        def __enter__(self):
            return FakeResponse()
        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_stream(method, url, **kwargs):
        seen.update(method=method, url=url, headers=kwargs.get('headers', {}))
        return StreamContext()

    target_dir = tmp_path / 'updates'
    monkeypatch.setattr(update_manager, 'update_dir', lambda: target_dir)
    monkeypatch.setattr(update_manager.httpx, 'stream', fake_stream)
    monkeypatch.setattr(update_manager, 'inspect_archive', lambda path: {'version': '4.0.25', 'sha256': 'a'*64, 'size': len(payload)})
    monkeypatch.setattr(update_manager, 'current_version', lambda: '4.0.24')
    info = {
        'source': 'github', 'version': '4.0.25', 'filename': archive.name,
        'size': len(payload), 'sha256': '',
        'github_asset_api_url': 'https://api.github.com/repos/Menshikovivan/FargoVPN/releases/assets/25',
        'github_asset_url': 'https://github.com/Menshikovivan/FargoVPN/releases/download/FargoVPN-4.0.25/' + archive.name,
    }
    out = update_manager.obtain_update_archive(info)
    assert out.is_file()
    assert out.stat().st_size == len(payload)
    assert seen['method'] == 'GET'
    assert seen['headers']['Accept'] == 'application/octet-stream'


def test_updates_check_json_contract_does_not_redirect(monkeypatch, client):
    monkeypatch.setattr(webapp, 'require_auth', lambda request: None)
    monkeypatch.setattr(update_manager, 'check_available_update', lambda force=False: {
        'available': True, 'version': '4.0.25', 'installed_version': '4.0.24', 'source': 'github', 'error': ''
    })
    response = client.post(webapp.public_prefix() + '/updates/check', headers={'Accept': 'application/json'})
    assert response.status_code == 200
    assert response.json()['available'] is True
    assert response.json()['version'] == '4.0.25'


def test_updates_check_html_contract_still_redirects(monkeypatch, client):
    monkeypatch.setattr(webapp, 'require_auth', lambda request: None)
    monkeypatch.setattr(update_manager, 'check_available_update', lambda force=False: {
        'available': False, 'installed_version': '4.0.25', 'source': 'github', 'error': ''
    })
    response = client.post(webapp.public_prefix() + '/updates/check', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].endswith('/updates')


def test_updates_page_has_real_prefixed_check_form_and_handler(monkeypatch, client):
    monkeypatch.setattr(webapp, 'require_auth', lambda request: None)
    monkeypatch.setattr(webapp, 'can_publish_update', lambda request: False)
    response = client.get(webapp.public_prefix() + '/updates')
    assert response.status_code == 200
    prefix = webapp.public_prefix()
    assert f'id="check-updates-form"' in response.text
    assert f'action="{prefix}/updates/check"' in response.text
    assert '{html.escape(public_path("/updates/check"), quote=True)}' not in response.text
    assert 'fetchWithTimeout(checkUpdatesForm.action' in response.text

def test_push_test_is_queued_and_does_not_wait_for_provider(monkeypatch, client):
    monkeypatch.setattr(webapp, 'require_auth', lambda request: None)
    monkeypatch.setattr(webapp.push_service, 'active_subscription_count', lambda *args: 1)
    calls=[]
    monkeypatch.setattr(webapp.push_service, 'notify_panel', lambda *args: calls.append(args))
    response = client.post(webapp.public_prefix() + '/api/panel/push/test', headers={'Accept':'application/json'})
    assert response.status_code == 202
    assert response.json()['queued'] is True
    assert calls

def test_push_ui_has_ios_home_screen_gate_and_no_raw_abort_timeout_label():
    text = Path('webapp.py').read_text(encoding='utf-8')
    assert 'const isIOS=' in text
    assert 'const isStandalone=' in text
    assert 'Требуется PWA' in text
    assert "err.name='TimeoutError'" in text
    assert "'ОШИБКА AbortError'" not in text
