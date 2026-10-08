from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_sanitizer_masks_secret_and_subscription_data():
    spec = importlib.util.spec_from_file_location("message_sanitize_test", ROOT / "message_sanitize.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    text = (
        "token=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij "
        "Authorization: secret-value "
        "https://example.test/sub/VERYSECRET"
    )
    redacted = module.redact_message_text(text)
    assert "VERYSECRET" not in redacted
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in redacted
    assert "<telegram-token>" in redacted
    assert "<subscription-secret>" in redacted


def load_journal_module(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DB_PATH = "/tmp/fargovpn-test.db"
    cfg.USER_EVENT_JOURNAL_QUEUE_SIZE = 100
    fake_events = types.ModuleType("user_events")
    fake_events.record_events_batch = lambda events, db_path=None: len(list(events))
    fake_events.safe_record_event = lambda **kwargs: 1
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "user_events", fake_events)
    name = "message_journal_473_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "message_journal.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_queue_is_non_blocking_and_flushes_batch(monkeypatch):
    module = load_journal_module(monkeypatch)
    seen = []
    async def fake_to_thread(fn, *args, **kwargs):
        seen.append((fn.__name__, len(args[0])))
        return fn(*args, **kwargs)
    monkeypatch.setattr(module.asyncio, "to_thread", fake_to_thread)
    module.enqueue(module.make_entry(123, text="hello"))
    await module.flush()
    assert seen == [("<lambda>", 1)]
    await module.shutdown()


def test_main_has_one_central_bot_api_interception_point():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    journal = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "JournalBot")
    methods = [node.name for node in journal.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert "__call__" in methods
    assert not any(name in methods for name in ("send_message", "send_photo", "send_document", "send_video"))
    assert source.count("class JournalBot") == 1


def test_messages_page_exposes_direction_type_search_and_pagination():
    source = (ROOT / "webapp.py").read_text(encoding="utf-8")
    start = source.index('@app.get("/messages", response_class=HTMLResponse)')
    end = source.index('@app.get("/users/new"', start)
    block = source[start:end]
    for needle in ("message_kind", "page_no", "per_page", "query_text", "Служебные", "Входящие", "Исходящие", "messages-pagination"):
        assert needle in block


def test_user_events_schema_contains_delivery_columns_and_batch_api():
    init = (ROOT / "init_db.py").read_text(encoding="utf-8")
    events = (ROOT / "user_events.py").read_text(encoding="utf-8")
    assert "message_kind TEXT NOT NULL DEFAULT 'message'" in init
    assert "delivery_status TEXT NOT NULL DEFAULT 'unknown'" in init
    assert '"delivery_error":"TEXT"' in init
    assert "idx_user_events_direction_kind_id" in init
    assert "def record_events_batch" in events


def test_all_outbound_bypass_workers_are_journalled():
    for filename, needle in (
        ("broadcast_worker.py", "record_outgoing_sync"),
        ("subscription_refresh_worker.py", "record_outgoing_sync"),
        ("trigger_reminders.py", "record_outgoing_sync"),
    ):
        source = (ROOT / filename).read_text(encoding="utf-8")
        assert needle in source


def test_update_level_journal_handles_non_message_updates_without_fake_cleanup():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'dp.update.outer_middleware(EventJournalMiddleware())' in source
    assert 'dp.message.outer_middleware(EventJournalMiddleware())' not in source
    assert 'dp.callback_query.outer_middleware(EventJournalMiddleware())' not in source
    assert 'is_message = isinstance(source, Message)' in source


def test_bot_api_allowlist_does_not_journal_chat_actions_as_messages():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    block = source[source.index('def _message_like'):source.index('def _service_like')]
    assert 'sendMessage' in block
    assert 'sendChatAction' not in block


def test_batch_unread_upsert_uses_postgres_greatest():
    source = (ROOT / "user_events.py").read_text(encoding="utf-8")
    assert 'last_incoming_event_id=GREATEST(user_message_state.last_incoming_event_id,excluded.last_incoming_event_id)' in source


def test_legacy_keyboard_cleanup_is_explicitly_journalled():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    block = source[source.index('async def ensure_legacy_reply_keyboard_removed'):source.index('def incoming_event_payload')]
    assert 'event_type="legacy_keyboard_cleanup"' in block
    assert 'message_kind="service"' in block


def test_messages_page_refreshes_unread_snapshot_after_marking_selected_chat_read():
    source = (ROOT / "webapp.py").read_text(encoding="utf-8")
    start = source.index('@app.get("/messages", response_class=HTMLResponse)')
    end = source.index('@app.get("/users/new"', start)
    block = source[start:end]
    assert block.index('user_events.mark_messages_read(') < block.index('unread_snapshot = user_events.unread_messages_summary(')


def test_queue_overflow_uses_detached_persistence_instead_of_drop():
    source = (ROOT / "message_journal.py").read_text(encoding="utf-8")
    assert 'overflow worker' in source
    assert 'safe_record_event, **entry' in source


def test_journal_batch_has_transient_db_retries():
    source = (ROOT / "message_journal.py").read_text(encoding="utf-8")
    assert 'for attempt in range(4)' in source
    assert '0.5 * (2 ** attempt)' in source


def test_delivery_errors_are_redacted_before_persistence():
    source = (ROOT / "user_events.py").read_text(encoding="utf-8")
    assert 'error_value = redact_message_text' in source
    assert 'delivery_error": redact_message_text' in source
