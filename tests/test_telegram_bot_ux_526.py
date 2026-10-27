from __future__ import annotations

import ast
from pathlib import Path
import secrets
from types import SimpleNamespace


MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def _functions():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _strings(node):
    return [item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)]


def test_family_link_explains_pin_code_and_next_step():
    source = MAIN.read_text(encoding="utf-8")
    assert "Чтобы завершить регистрацию, отправьте боту 4-значный код приглашения" in source
    assert "и проверьте статус общей подписки" in source
    assert "Регистрация завершена. Посмотрите статус общей подписки" in source
    assert "семейная подписка станет доступна" not in source


def test_active_family_invitee_gets_user_menu_and_explicit_next_step():
    functions = _functions()
    handler = functions["cmd_start"]
    active_branch = next(
        node for node in ast.walk(handler)
        if isinstance(node, ast.If)
        and "registration_access.ACTIVE" in ast.unparse(node.test)
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "get_user_menu_async"
            for statement in node.body for child in ast.walk(statement)
        )
    )
    assert any(
        isinstance(node, ast.keyword) and node.arg == "reply_markup"
        for statement in active_branch.body for node in ast.walk(statement)
    )
    assert any("Посмотрите статус общей подписки в разделе «Моя подписка» ниже" in item for item in _strings(handler))
    inactive_calls = [
        node for statement in active_branch.orelse for node in ast.walk(statement)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "answer"
    ]
    assert inactive_calls
    assert all(not any(keyword.arg == "reply_markup" for keyword in call.keywords) for call in inactive_calls)


def test_family_member_label_falls_back_to_username_then_telegram_id():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "family_member_label"
    )
    namespace = {"Any": object}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "main.py", "exec"), namespace)
    label = namespace["family_member_label"]
    assert label({"tg_id": 123, "username": "member"}) == "@member"
    assert label({"tg_id": 123}) == "Telegram ID 123"
    assert label({"tg_id": 123, "label": "Name", "username": "member"}) == "Name"


def test_community_button_uses_config_default_and_blank_disables_it():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    get_menu = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "get_user_menu"
    )

    class Builder:
        def __init__(self):
            self.buttons = []
            self.row_widths = ()

        def button(self, **kwargs):
            self.buttons.append(kwargs)

        def adjust(self, *widths):
            self.row_widths = widths

        def as_markup(self):
            return self

    def build(setting=...):
        builder = Builder()
        values = {"ADMIN_IDS": []}
        if setting is not ...:
            values["BOT_COMMUNITY_URL"] = setting
        namespace = {
            "has_active_subscription": lambda _user: False,
            "InlineKeyboardBuilder": lambda: builder,
            "config": SimpleNamespace(**values),
            "get_personal_cabinet_url": lambda *_args: "",
        }
        exec(compile(ast.Module(body=[get_menu], type_ignores=[]), "get_user_menu", "exec"), namespace)
        return namespace["get_user_menu"](100)

    default_menu = build()
    community = [button for button in default_menu.buttons if button.get("url")]
    assert len(community) == 1
    assert community[0]["url"] == "https://t.me/fargovpn"
    assert community[0]["text"] == "📣 Новости и сообщество"
    assert {
        "menu:buy", "menu:stats", "menu:faq", "menu:whitelist", "menu:support"
    } <= {button.get("callback_data") for button in default_menu.buttons}

    disabled_menu = build("")
    assert not any(button.get("url") for button in disabled_menu.buttons)


def test_admin_relabels_preserve_legacy_text_handlers_and_callback_routes():
    source = MAIN.read_text(encoding="utf-8")
    for legacy_label in ("Управление", "Синхронизация", "Администратора", "Рассылка"):
        assert legacy_label in source
    tree = ast.parse(source)
    menu = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "get_admin_menu"
    )
    callbacks = {
        node.value.value
        for node in ast.walk(menu)
        if isinstance(node, ast.keyword)
        and node.arg == "callback_data"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert {
        "admin:summary", "admin:users", "admin:subscriptions", "admin:broadcast",
        "admin:backup", "admin:sync", "admin:grant", "menu:home",
    } <= callbacks


def test_connection_instructions_explain_manual_import_without_promising_connection():
    functions = _functions()
    source = MAIN.read_text(encoding="utf-8")
    assert "Если приложение не открылось" in source
    assert "скопируйте ссылку на странице и добавьте её вручную" in source
    assert "Если импорт не начался" in source
    assert "Не отправляйте ссылку подписки или коды доступа." in source
    assert "получит вашу подписку автоматически" not in source
    assert any("Работа зависит от вашей сети и доступности сервера" in item for item in _strings(functions["home_text"]))


def test_purchase_wait_has_explicit_cancel_and_cancel_clears_only_wait_state():
    functions = _functions()
    purchase = functions["start_purchase"]
    markup = functions["purchase_cancel_markup"]
    assert "purchase:cancel:" in _strings(markup)
    assert any("Отменить ожидание чека" in item for item in _strings(markup))
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "purchase_cancel_markup"
        for node in ast.walk(purchase)
    )

    cancel = functions["cancel_receipt_wait"]
    strings = _strings(cancel)
    assert any("Ожидание чека остановлено" in item for item in strings)
    assert any("покупку " in item and "продление подписки" in item for item in strings)
    assert "purchase" not in strings
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "clear"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "state"
        for node in ast.walk(cancel)
    )
    assert "FSMStates.wait_for_receipt.state" in ast.unparse(cancel)
    assert "purchase:cancel:" in _strings(purchase_cancel_markup := _functions()["purchase_cancel_markup"])
    assert len("purchase:cancel:" + secrets.token_hex(8).encode().decode()) <= 64


def test_purchase_cancel_rejects_legacy_and_stale_tokens_but_accepts_current_token():
    helper = _functions()["purchase_cancel_token_matches"]
    namespace = {"secrets": secrets}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "purchase_cancel_token_matches", "exec"), namespace)
    matches = namespace["purchase_cancel_token_matches"]
    assert matches("purchase:cancel:new-session", "new-session")
    assert not matches("purchase:cancel", "new-session")
    assert not matches("purchase:cancel:old-session", "new-session")
    assert not matches("purchase:cancel:new-session", "")
    assert len(("purchase:cancel:" + secrets.token_hex(8)).encode("ascii")) <= 64


def test_family_unlink_is_confirmed_before_billing_mutation():
    family = _functions()["family_callbacks"]
    source = ast.unparse(family)
    assert "action[1:3] == ['remove', 'confirm']" in source
    assert "family:remove:confirm:{member_id}" in source
    assert "family:remove:cancel" in source
    confirm_position = source.index("action[1:3] == ['remove', 'confirm']")
    unlink_position = source.index("billing.unlink_profile_member_sync")
    assert confirm_position < unlink_position
    assert "Отвязка отменена" in source

