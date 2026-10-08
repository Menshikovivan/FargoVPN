from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def func(source_name, func_name):
    src = read(source_name)
    match = re.search(rf"(?ms)^(?:async )?def {re.escape(func_name)}\b.*?(?=^@app\.|^def |^async def |\Z)", src)
    assert match, func_name
    return match.group(0)


def test_incremental_feed_is_auth_protected_and_cursor_based():
    src = func("webapp.py", "panel_messages_feed_api")
    assert "require_auth(request)" in src
    assert "recent_all_events(after_id=after_id, limit=500" in src
    assert '"next_after_id": next_after_id' in src
    assert '"selected_events": selected_events' in src


def test_incremental_journal_query_uses_append_only_id_index():
    src = read("user_events.py")
    init = read("init_db.py")
    block = func("user_events.py", "recent_all_events")
    assert "WHERE id>? ORDER BY id ASC LIMIT ?" in block
    assert "after_id = max(0, int(after_id or 0))" in block
    assert "limit = max(1, min(int(limit or 500), 1000))" in block
    assert "idx_user_events_tg_id_id" in init


def test_dialog_delta_only_recomputes_changed_users():
    block = func("user_events.py", "conversation_users_for_ids")
    assert "WHERE events.tg_id IN ({placeholders})" in block
    assert "GROUP BY events.tg_id" in block
    assert "MAX(events.id) AS last_event_id" in block


def test_messages_page_has_composer_and_reuses_existing_send_endpoint():
    src = func("webapp.py", "messages_page")
    assert 'action="/users/{selected}/message"' in src
    assert 'id="messages-compose-form"' in src
    assert 'id="messages-compose-text"' in src
    assert 'accept="image/*,video/*"' in src
    assert "fetch(compose.action" in src
    assert 'headers:{Accept:\'application/json\'}' in src or 'headers:{{Accept:{Accept' in src or "Accept:'application/json'" in src
    assert 'return JSONResponse({"ok": True, "detail": detail, "event": event}, status_code=200)' in read("webapp.py")


def test_messages_composer_enter_and_shift_enter_behavior():
    src = func("webapp.py", "messages_page")
    assert "event.key==='Enter'" in src
    assert "!event.shiftKey" in src
    assert "event.preventDefault()" in src
    assert "compose?.requestSubmit()" in src


def test_messages_polling_preserves_scroll_and_does_not_reload_page():
    src = func("webapp.py", "messages_page")
    assert "after_id" in src
    assert "setTimeout(poll,3000)" in src
    assert "data-message-live-feed=\"1\"" in src
    assert "const atBottom=(chat.scrollHeight-chat.scrollTop-chat.clientHeight)<64" in src
    assert "chat.scrollTop=Math.max(0,chat.scrollTop+(chat.scrollHeight-oldHeight))" in src
    assert "location.reload" not in src
    assert "window.location.reload" not in src


def test_dynamic_message_dom_uses_textcontent_for_user_controlled_text():
    src = func("webapp.py", "messages_page")
    assert "text.textContent=String(item.text||item.event_type||'Событие')" in src
    assert "muted.textContent=direction+' · '+kind" in src
    assert "strong.textContent='@'+username" in src
    assert "row.innerHTML=" in src  # static shell only; user data is not interpolated into it


def test_web_send_journal_module_is_imported():
    source = read("webapp.py")
    assert "import message_journal" in source
    assert "message_journal.record_outgoing_sync" in source


def test_exact_event_echo_prevents_parallel_admin_race():
    src = read("webapp.py")
    assert "event_id = _safe_journal_outgoing_sync(" in src
    assert "event = _safe_event_for_id(int(tg_id), event_id) if return_event else None" in src
    route = func("webapp.py", "message_user")
    assert "telegram_send(tg_id, message, event_type=\"admin_message\", return_event=True)" in route
    assert "telegram_send_media(tg_id, media, message, return_event=True)" in route


def test_friendly_telegram_errors_cover_requested_cases():
    block = func("webapp.py", "friendly_telegram_error")
    for needle in (
        "bot can't initiate conversation",
        "bot was blocked by the user",
        "chat not found",
        "too many requests",
        "timeout",
    ):
        assert needle in block


def test_no_database_migration_was_added_for_live_messages():
    src = read("user_events.py")
    assert "CREATE TABLE" not in func("user_events.py", "recent_all_events")
    assert "ALTER TABLE" not in func("user_events.py", "recent_all_events")
    assert "CREATE TABLE" not in func("user_events.py", "conversation_users_for_ids")
    assert "ALTER TABLE" not in func("user_events.py", "conversation_users_for_ids")
