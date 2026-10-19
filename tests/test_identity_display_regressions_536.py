"""Regression tests for trusted Telegram identity and username-only links in 5.1.38."""
from __future__ import annotations

import ast
import html
from pathlib import Path

from user_identity import get_display_name, is_generated_identity_comment, latest_trusted_telegram_usernames, normalize_username

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def test_latest_incoming_telegram_identity_repairs_polluted_stored_usernames():
    events = [
        {"id": 10, "tg_id": 422172767, "username": "kiryan_avd", "direction": "in", "actor": "telegram:@kiryan_avd"},
        {"id": 11, "tg_id": 422172767, "username": "Kirill", "direction": "system", "actor": "web"},
        {"id": 20, "tg_id": 693845923, "username": "AnnaSam111", "direction": "in", "actor": "telegram:@AnnaSam111"},
        {"id": 21, "tg_id": 693845923, "username": "Anna", "direction": "out", "actor": "telegram_bot"},
        {"id": 22, "tg_id": 123, "username": "not_a_trusted_source", "direction": "in", "actor": "web"},
    ]
    trusted = latest_trusted_telegram_usernames(events)
    assert trusted == {422172767: "kiryan_avd", 693845923: "AnnaSam111"}
    assert get_display_name({"tg_id": 422172767, "username": trusted[422172767], "notes": "kiryan_avd"}) == "@kiryan_avd"
    assert get_display_name({"tg_id": 693845923, "username": trusted[693845923], "notes": "AnnaSam111"}) == "@AnnaSam111"


def test_latest_incoming_update_can_clear_a_removed_handle_without_trusting_web_events():
    events = [
        {"id": 1, "tg_id": 123, "username": "alice", "direction": "in", "actor": "telegram:@alice"},
        {"id": 2, "tg_id": 123, "username": "", "direction": "in", "actor": "telegram:123"},
        {"id": 3, "tg_id": 123, "username": "bob", "direction": "in", "actor": "web"},
    ]
    assert latest_trusted_telegram_usernames(events) == {123: ""}


def test_synthetic_id_username_falls_back_to_operator_comment_then_id():
    user = {"tg_id": 1180284364, "username": "id", "notes": "Брат Мишы Авхадеева"}
    assert normalize_username("id", 1180284364) == ""
    assert normalize_username("id_1180284364", 1180284364) == ""
    assert normalize_username("id_693845923", 1180284364) == ""
    assert get_display_name(user) == "Брат Мишы Авхадеева"
    assert get_display_name({"tg_id": 1180284364, "username": "id", "notes": "   "}) == "ID 1180284364"


def test_empty_and_string_none_values_do_not_win_over_comments():
    for bad in (None, "", " ", "None", "NULL", "undefined", "id", "id_123", "123"):
        assert get_display_name({"tg_id": 123, "username": bad, "notes": "Комментарий CRM"}) == "Комментарий CRM"
    assert get_display_name({"tg_id": 123, "username": "   ", "notes": " "}) == "ID 123"


def test_telegram_profile_link_uses_valid_username_only_and_escapes_dynamic_text():
    fn = _function(APP / "webapp.py", "_telegram_profile_link")
    namespace = {"Any": object, "html": html, "normalize_username": normalize_username}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "_telegram_profile_link", "exec"), namespace)
    link = namespace["_telegram_profile_link"](422172767, "@kiryan_avd <test>", username="@kiryan_avd")
    assert 'href="https://t.me/kiryan_avd"' in link
    assert "@kiryan_avd &lt;test&gt;" in link
    assert "<test>" not in link
    no_username = namespace["_telegram_profile_link"](1180284364, "Брат Мишы Авхадеева <test>")
    assert "<a " not in no_username
    assert "tg://user?id=" not in no_username
    assert "Брат Мишы Авхадеева &lt;test&gt;" in no_username


def test_audit_layout_does_not_force_details_column_to_one_pixel():
    css = (APP / "static/panel.css").read_text(encoding="utf-8")
    assert ".audit-details{min-width:0;max-width:none;width:48%" in css
    assert ".audit-filters{display:grid" in css
    assert "@media(max-width:560px){.audit-filters{grid-template-columns:minmax(0,1fr)}" in css


def test_version_strings_are_coherent_for_release():
    assert (APP / "VERSION").read_text(encoding="utf-8").strip() == "5.1.38"
    assert (APP / "static/VERSION").read_text(encoding="utf-8").strip() == "5.1.38"
    js = (APP / "static/panel.js").read_text(encoding="utf-8")
    assert "const PANEL_BUILD_VERSION = '5.1.38';" in js


def test_family_member_and_payment_labels_are_clickable_telegram_profiles():
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    assert 'billing.telegram_identity_label(int(x[0]' in source
    assert '_telegram_profile_link(int(x[0]), billing.telegram_identity_label' in source
    assert 'payment_user_link = _telegram_profile_link(' in source
    assert '<td data-label="Пользователь">{payment_user_link}' in source
    assert 'payment_user_link} · Telegram ID' in source


def test_historical_username_display_fallback_helpers_are_not_used_or_shipped():
    user_events_source = (APP / "user_events.py").read_text(encoding="utf-8")
    cabinet_source = (APP / "cabinet_service.py").read_text(encoding="utf-8")
    webapp_source = (APP / "webapp.py").read_text(encoding="utf-8")
    assert "def latest_valid_usernames(" not in user_events_source
    assert "def last_known_username(" not in user_events_source
    assert "def identity_candidates(" not in cabinet_source
    assert "def _cabinet_identity_candidates(" not in webapp_source


def test_identity_import_and_registration_paths_reject_synthetic_handles():
    registration_clean = _function(APP / "registration_access.py", "_clean_username")
    registration_ns = {"Any": object, "re": __import__("re"), "normalize_username": normalize_username}
    exec(compile(ast.Module(body=[registration_clean], type_ignores=[]), "registration_clean", "exec"), registration_ns)
    assert registration_ns["_clean_username"]("id_1180284364", 1180284364) == ""
    assert registration_ns["_clean_username"]("id", 1180284364) == ""
    assert registration_ns["_clean_username"]("@Anna", 693845923) == "Anna"

    import_clean = _function(APP / "identity_migration.py", "_clean_username")
    import_ns = {"Any": object, "re": __import__("re"), "normalize_username": normalize_username}
    exec(compile(ast.Module(body=[import_clean], type_ignores=[]), "import_clean", "exec"), import_ns)
    assert import_ns["_clean_username"]("id_1180284364", 1180284364) == ""
    assert import_ns["_clean_username"]("Anna", 693845923) == "Anna"
    assert import_ns["_clean_username"]("Local Person", -1) == "Local Person"


def test_generated_id_comments_are_ignored_but_real_operator_comments_are_preserved():
    tg_id = 1180284364
    email = "id_1180284364+1180284364"
    assert is_generated_identity_comment("id", tg_id, email)
    assert is_generated_identity_comment("id_1180284364", tg_id, email)
    assert is_generated_identity_comment("id_999", tg_id, email)
    assert not is_generated_identity_comment("Брат Мишы Авхадеева", tg_id, email)
    assert not is_generated_identity_comment("id", tg_id, "Anna+1180284364")
