from pathlib import Path
import re, tarfile, subprocess

ROOT = Path(__file__).resolve().parents[1]

def test_next_version_and_clean_archive_contract():
    assert (ROOT / "VERSION").read_text().strip() == "4.0.33"
    assert (ROOT / "app" / "VERSION").read_text().strip() == "4.0.33"

def test_socket_directory_is_not_service_runtime_directory():
    text=(ROOT/"install.sh").read_text()
    assert "RuntimeDirectory=vpn-service" not in text
    assert "d /run/vpn-service 0755 root root -" in text
    assert "SocketUser=root" in text
    assert "SocketGroup=$NGINX_SOCKET_GROUP" in text
    assert "detect_nginx_socket_group" in text
    assert "SocketMode=0660" in text

def test_web_service_logs_and_uses_socket_service():
    text=(ROOT/"install.sh").read_text()
    assert "Requires=vpn-service-web.socket nginx.service" in text
    assert "ExecStart=$TARGET/web_start.sh" in text
    assert "StandardOutput=append:/var/log/vpn_bot.log" in text
    assert "StandardError=append:/var/log/vpn_bot.log" in text

def test_public_route_is_socket_and_no_8088():
    text=(ROOT/"nginx_panel_guard.py").read_text()
    assert "proxy_pass http://unix:{socket_path}:/" in text
    assert "proxy_redirect" not in text.split('def block',1)[1].split('def _strip_location_blocks',1)[0]

def test_log_api_is_fixed_path_and_protected():
    text=(ROOT/"webapp.py").read_text()
    assert '@app.get("/api/panel/app-log")' in text
    assert 'require_auth(request)' in text
    assert 'APP_LOG_DEFAULT = "/var/log/vpn_bot.log"' in text

def test_no_runtime_407_in_serving_code():
    for path in [ROOT/"webapp.py", ROOT/"service-worker.js", ROOT/"static"/"panel.js", ROOT/"static"/"panel.css"]:
        text=path.read_text(encoding="utf-8", errors="ignore")
        assert "4.0.7" not in text

def test_archive_release_builder_has_clean_name():
    text=(ROOT/"build_release.sh").read_text()
    assert 'ARCHIVE="$ROOT/../../FargoVPN-$version.tar.gz"' in text
    assert 'PACKAGE_NAME="$(basename "$ROOT")"' in text


def test_app_log_ui_and_endpoint_contract():
    text=(ROOT/"webapp.py").read_text()
    assert 'Лог приложения' in text
    assert 'id="app-log-output"' in text
    assert 'id="app-log-refresh"' in text
    assert '/api/panel/app-log' in text
    assert 'require_auth(request)' in text

def test_messages_unread_api_and_navigation_badge_contract():
    text=(ROOT/"webapp.py").read_text()
    assert '@app.get("/api/panel/messages/unread")' in text
    assert 'data-unread-total' in text
    assert 'unread_messages_summary' in text
    assert 'message-unread-badge' in text


def test_unread_polling_is_single_flight_and_visibility_aware():
    text=(ROOT/"static"/"panel.js").read_text()
    assert 'function initUnreadMessages()' in text
    assert 'inFlight' in text
    assert "visibilitychange" in text
    assert "pagehide" in text
    assert "15000" in text


def test_latency_sensitive_telegram_paths_offload_sync_work():
    text=(ROOT/"main.py").read_text()
    assert 'event_id = await asyncio.to_thread(' in text
    assert 'current_user = await asyncio.to_thread(db_get_user' in text
    assert 'await asyncio.to_thread(_save_pending_registration)' in text
    assert 'await asyncio.to_thread(admin_summary_text)' in text
