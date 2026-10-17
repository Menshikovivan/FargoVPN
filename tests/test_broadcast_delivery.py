from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"

def read(name):
    return (ROOT / name).read_text(encoding="utf-8")

def test_terminal_telegram_errors_mark_chat_unavailable_and_next_broadcast_skips_it():
    worker = read("broadcast_worker.py")
    manager = read("broadcast_manager.py")
    assert "def _is_terminal_recipient_error" in worker
    assert '"bot was blocked"' in worker
    assert '"chat not found"' in worker
    assert "telegram_available=0" in worker
    assert "COALESCE(u.telegram_available,1)<>0" in manager
    assert "l.profile_tg_id=u.tg_id OR EXISTS" in manager

def test_incoming_message_reactivates_telegram_delivery():
    source = read("user_events.py")
    assert 'UPDATE users SET telegram_available=1' in source
    assert 'telegram_unavailable_reason=NULL' in source

def test_panel_exposes_telegram_delivery_state_without_public_owner_name():
    panel = read("webapp.py")
    assert "telegram_available" in panel
    assert "Telegram недоступен" in panel
    public_docs = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in ROOT.glob("*.md"))
    assert "только конкретная учётная запись" not in public_docs.lower()
    assert "разрешила служебный api" not in public_docs.lower()


def test_telegram_admin_broadcast_reuses_filtered_family_recipient_selector():
    main = read("main.py")
    manager = read("broadcast_manager.py")
    start = main.index("async def broad_proc(")
    end = main.index("@dp.message", start)
    handler = main[start:end]

    assert "broadcast_manager._recipients" in handler
    assert 'cursor.execute("SELECT tg_id FROM users")' not in handler
    assert "WHERE u.tg_id>0 AND COALESCE(u.telegram_available,1)<>0" in manager
    assert "COALESCE(u.registration_status,'active')='active'" in manager
    # users.tg_id and telegram_profile_links.telegram_id are unique keys, and
    # the family eligibility condition is EXISTS (rather than a multiplying join).
    assert "LEFT JOIN telegram_profile_links l ON l.telegram_id=u.tg_id" in manager
    assert "AND (l.telegram_id IS NULL OR l.profile_tg_id=u.tg_id OR EXISTS (" in manager


def test_telegram_admin_broadcast_reads_recipient_id_from_shared_selector_record():
    main = read("main.py")
    start = main.index("async def broad_proc(")
    end = main.index("@dp.message", start)
    handler = main[start:end]

    assert 'target_id = int(row["tg_id"])' in handler
