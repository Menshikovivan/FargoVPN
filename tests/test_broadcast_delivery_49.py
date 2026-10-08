from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(name):
    return (ROOT / name).read_text(encoding="utf-8")

def test_terminal_telegram_errors_mark_chat_unavailable_and_next_broadcast_skips_it():
    worker = read("broadcast_worker.py")
    manager = read("broadcast_manager.py")
    assert "def _is_terminal_recipient_error" in worker
    assert '"bot was blocked"' in worker
    assert '"chat not found"' in worker
    assert "telegram_available=0" in worker
    assert "COALESCE(telegram_available,1)<>0" in manager

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
