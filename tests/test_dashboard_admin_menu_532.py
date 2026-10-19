"""Regressions for the dashboard 500 and Telegram admin-menu readability."""
from __future__ import annotations

import ast
import html
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _dashboard_snapshot_helper(live_users, local_users):
    node = _function(ROOT / "app/webapp.py", "dashboard_users_snapshot")
    namespace = {
        "Any": object,
        "time": SimpleNamespace(time=lambda: 1_800_000_000),
        "LOGGER": SimpleNamespace(exception=Mock()),
        "live_users": live_users,
        "_local_users": local_users,
        "remaining_days": lambda expiry, now: max(0, (int(expiry or 0) - int(now)) // 86400000) if int(expiry or 0) > 0 else None,
        "cabinet_service": SimpleNamespace(
            public_username=lambda tg_id, *candidates: next(
                (str(item).strip().lstrip("@") for item in candidates
                 if item and str(item).strip().lstrip("@") not in {str(tg_id), f"id_{tg_id}"}),
                "",
            )
        ),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "dashboard_users_snapshot", "exec"), namespace)
    return namespace["dashboard_users_snapshot"]


def test_dashboard_keeps_live_snapshot_when_users_exist():
    clients = [{"tg_id": 42, "username": "id_42", "latest_username": "@real_user"}]
    snapshot = {"stale": False, "server_traffic_summary": {"up": 123}, "traffic_summary": {}}
    helper = _dashboard_snapshot_helper(lambda: (clients, snapshot), lambda: [])

    users, actual_snapshot = helper()

    assert users[0]["username"] == ""
    assert actual_snapshot is snapshot
    assert actual_snapshot["stale"] is False
    assert actual_snapshot["server_traffic_summary"]["up"] == 123


def test_dashboard_falls_back_to_local_users_and_marks_snapshot_stale():
    local = [{"tg_id": 7, "username": "id_7", "latest_username": "local_nick"}]

    def fail_live():
        raise RuntimeError("upstream unavailable")

    helper = _dashboard_snapshot_helper(fail_live, lambda: local)
    users, snapshot = helper()

    assert users[0]["username"] == ""
    assert snapshot["stale"] is True
    assert snapshot["error"] == "upstream unavailable"
    assert snapshot["server_traffic_summary"] == {}


def test_dashboard_route_uses_isolated_snapshot_helper():
    node = _function(ROOT / "app/webapp.py", "dashboard")
    calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)]
    assert any(
        isinstance(item.func, ast.Name) and item.func.id == "dashboard_users_snapshot"
        for item in calls
    )
    assert not any(
        isinstance(item.func, ast.Name) and item.func.id == "live_users"
        for item in calls
    )


def test_admin_menu_contains_all_existing_actions_and_uses_clear_rows():
    main_path = ROOT / "app/main.py"
    node = _function(main_path, "get_admin_menu")

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

    builder = Builder()
    namespace = {
        "InlineKeyboardBuilder": lambda: builder,
        "get_messages_panel_url": lambda: "https://panel.example.test/messages",
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "get_admin_menu", "exec"), namespace)
    result = namespace["get_admin_menu"]()

    actions = {item.get("callback_data") for item in result.buttons if item.get("callback_data")}
    labels = {item.get("text") for item in result.buttons}
    assert actions == {
        "admin:summary", "admin:users", "admin:subscriptions", "admin:broadcast",
        "admin:backup", "admin:sync", "admin:grant", "menu:home",
    }
    assert {"📊 Сводка", "👥 Пользователи", "📋 Подписки", "💾 Бэкап", "🔄 Синхронизация 3x-ui"} <= labels
    assert any(item.get("url") == "https://panel.example.test/messages" for item in result.buttons)
    assert result.row_widths == (2, 1, 2, 1, 2, 1)


def test_admin_entry_text_has_section_hierarchy_and_summary_is_grouped():
    node = _function(ROOT / "app/main.py", "admin_menu_text")
    namespace = {"config": SimpleNamespace(SERVICE_NAME="FargoVPN"), "html": html}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "admin_menu_text", "exec"), namespace)
    text = namespace["admin_menu_text"]()
    assert "📊 Мониторинг" in text
    assert "⚙️ Обслуживание" in text
    assert "💬 Коммуникации" in text

    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    summary = source[source.index("def admin_summary_text") : source.index("def subscription_report_text")]
    assert "СВОДКА СЕРВИСА" in summary
    assert "Пользователи и подписки" in summary
    assert "Платежи" in summary
    assert "Коммуникации" in summary
