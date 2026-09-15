from pathlib import Path


def test_xui_snapshot_cache_status_is_read_only():
    from services import xui_api
    result = xui_api.snapshot_cache_status()
    assert isinstance(result, dict)
    assert "cache_fresh" in result
    assert "cache_age_seconds" in result


def test_web_status_api_does_not_call_live_users(monkeypatch):
    import webapp
    monkeypatch.setattr(webapp, "require_auth", lambda request: None)
    monkeypatch.setattr(webapp, "live_users", lambda *a, **k: (_ for _ in ()).throw(AssertionError("live_users called")))
    monkeypatch.setattr(webapp, "snapshot_cache_status", lambda: {"stale": True, "ts": 0})
    monkeypatch.setattr(webapp, "fetch_control_snapshot_sync", lambda: {"status": {}, "fail2ban": {}, "nodes": []})
    monkeypatch.setattr(webapp, "dashboard_backup_status", lambda: {"state": "neutral", "label": "—"})
    monkeypatch.setattr(webapp, "service_state", lambda _u: "inactive")
    monkeypatch.setattr(webapp, "_database_health", lambda: True)
    monkeypatch.setattr(webapp.update_manager, "current_version", lambda: "4.0.30")
    request = webapp.Request({"type":"http","method":"GET","path":"/api/system-status","headers":[],"session":{"auth":True,"user":"admin"}})
    response = webapp.system_status_api(request)
    assert response.status_code == 200


def test_telegram_identity_refresh_is_not_in_message_critical_path():
    text = Path('main.py').read_text(encoding='utf-8')
    block = text[text.index('class EventJournalMiddleware'):text.index('# Dispatcher must exist')]
    assert 'await asyncio.to_thread(refresh_telegram_identity' not in block
    assert '_schedule_identity_refresh(tg_id, username)' in block


def test_yandex_test_write_local_mode_uses_configured_directory(monkeypatch, tmp_path):
    import backup
    monkeypatch.setattr(backup, 'yandex_mode', lambda _value=None: 'local')
    monkeypatch.setattr(backup.config, 'YANDEX_DISK_PATH', 'ProbeDir')
    monkeypatch.setattr(backup.config, 'YANDEX_LOCAL_PATH', str(tmp_path))
    monkeypatch.setattr(backup, '_upload_to_yandex_webdav', lambda probe, directory, root: None)
    monkeypatch.setattr(backup, '_copy_to_yandex_mount', lambda probe, root, directory: (True, 'copied'))
    monkeypatch.setattr(backup, '_test_yandex_local_path', lambda root: (True, 'ok'))
    ok, detail = backup.test_yandex_write(mode='local', local_path=tmp_path)
    assert ok
    assert 'copied' in detail


def test_detached_backup_runtime_unit_is_root(monkeypatch, tmp_path):
    import detached_jobs
    runtime_dir = tmp_path / 'systemd'
    runtime_dir.mkdir()
    monkeypatch.setattr(detached_jobs, 'Path', detached_jobs.Path)
    # Validate the source contract directly: fallback must never inherit an
    # unrelated interactive service identity.
    source = Path('detached_jobs.py').read_text(encoding='utf-8')
    assert 'User=root\\n' in source
    assert 'Group=root\\n' in source


def test_user_menu_builder_has_no_database_lookup():
    text = Path('main.py').read_text(encoding='utf-8')
    block = text[text.index('def get_user_menu('):text.index('def get_messages_panel_url') if 'def get_messages_panel_url' in text else text.index('async def get_user_menu_async')]
    assert 'db_get_user(' not in block

def test_xui_sync_snapshot_select_contains_enable_column():
    text = Path('services/xui_api.py').read_text(encoding='utf-8')
    start = text.index('def sync_snapshot_to_db')
    end = text.index('def fetch_and_sync', start)
    block = text[start:end]
    assert 'expiry_time,enable,last_reminder_days' in block
    assert 'source_row["enable"]' in block


def test_xui_snapshot_sync_handles_legacy_local_row_without_enable_key(tmp_path, monkeypatch):
    from services import xui_api
    import sqlite3
    db = tmp_path / "fargo.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE users (tg_id INTEGER PRIMARY KEY, username TEXT, uuid TEXT, email TEXT, expiry_time INTEGER, enable INTEGER, last_reminder_days INTEGER)"
        )
        conn.execute(
            "INSERT INTO users(tg_id,username,uuid,email,expiry_time,enable,last_reminder_days) VALUES(?,?,?,?,?,?,?)",
            (123, "legacy", "u-1", "legacy@example", 0, 1, -1),
        )
        conn.commit()
    changed = xui_api.sync_snapshot_to_db({"clients": []}, db)
    assert changed >= 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT enable FROM users WHERE tg_id=123").fetchone()[0] == 0


def test_user_menu_async_explicitly_offloads_db_lookup():
    text = Path("main.py").read_text(encoding="utf-8")
    start = text.index("async def get_user_menu_async")
    end = text.index("def get_messages_panel_url", start)
    block = text[start:end]
    assert "await asyncio.to_thread(db_get_user" in block


def test_send_user_stats_reuses_resolved_user_for_cabinet_url():
    text = Path("main.py").read_text(encoding="utf-8")
    start = text.index("async def send_user_stats")
    end = text.index("def payment_text", start)
    block = text[start:end]
    assert "get_personal_cabinet_url(user_id, u)" in block
