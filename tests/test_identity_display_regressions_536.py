"""Regression tests for current Telegram identity, labels and links in 5.1.36."""
from __future__ import annotations

import ast
import html
from pathlib import Path

from user_identity import get_display_name, is_generated_identity_comment, normalize_username

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def test_current_handle_beats_older_comment_and_history_is_not_a_display_source():
    assert get_display_name({"tg_id": 422172767, "username": "Kirill", "notes": "kiryan_avd"}) == "@Kirill"
    assert get_display_name({"tg_id": 693845923, "username": "Anna", "notes": "AnnaSam111"}) == "@Anna"


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


def test_telegram_profile_link_uses_numeric_id_and_escapes_the_display_label():
    fn = _function(APP / "webapp.py", "_telegram_profile_link")
    namespace = {"Any": object, "html": html}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "_telegram_profile_link", "exec"), namespace)
    link = namespace["_telegram_profile_link"](1180284364, "Брат Мишы Авхадеева <test>")
    assert 'href="tg://user?id=1180284364"' in link
    assert "Брат Мишы Авхадеева &lt;test&gt;" in link
    assert "<test>" not in link


def test_audit_layout_does_not_force_details_column_to_one_pixel():
    css = (APP / "static/panel.css").read_text(encoding="utf-8")
    assert ".audit-details{min-width:0;max-width:none;width:48%" in css
    assert ".audit-filters{display:grid" in css
    assert "@media(max-width:560px){.audit-filters{grid-template-columns:minmax(0,1fr)}" in css


def test_version_strings_are_coherent_for_release():
    assert (APP / "VERSION").read_text(encoding="utf-8").strip() == "5.1.36"
    assert (APP / "static/VERSION").read_text(encoding="utf-8").strip() == "5.1.36"
    js = (APP / "static/panel.js").read_text(encoding="utf-8")
    assert "const PANEL_BUILD_VERSION = '5.1.36';" in js


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
