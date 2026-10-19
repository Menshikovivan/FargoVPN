"""Panel identity queries and labels must never treat Telegram IDs as usernames."""
import ast
from contextlib import contextmanager
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from user_identity import normalize_username, get_display_name
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_panel_user_query_skips_synthetic_history_and_label_uses_safe_fallback(tmp_path):
    db_path = tmp_path / "panel-identities.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users(tg_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, notes TEXT);
            CREATE TABLE user_events(id INTEGER PRIMARY KEY, tg_id INTEGER, username TEXT);
            INSERT INTO users VALUES(100, 'id_100', 'Telegram Full Name', 'Комментарий владельца');
            INSERT INTO user_events VALUES(1, 100, 'owner_real');
            INSERT INTO user_events VALUES(2, 100, '@id_100');
            INSERT INTO user_events VALUES(3, 100, '100');
            """
        )

    @contextmanager
    def database():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    panel_namespace = {"Any": Any, "database": database}
    local_users = _function(ROOT / "app/webapp.py", "_local_users")
    exec(
        compile(ast.Module(body=[local_users], type_ignores=[]), "_local_users", "exec"),
        panel_namespace,
    )
    rows = panel_namespace["_local_users"]()
    assert rows[0]["username"] == "id_100"
    assert "latest_username" not in rows[0]
    assert rows[0]["notes"] == "Комментарий владельца"

    cabinet_source = ROOT / "app/cabinet_service.py"
    public_username = _function(cabinet_source, "public_username")
    cabinet_namespace = {"Any": Any, "re": re, "normalize_username": normalize_username}
    exec(
        compile(ast.Module(body=[public_username], type_ignores=[]), "public_username", "exec"),
        cabinet_namespace,
    )
    billing_identity_label = _function(ROOT / "app/services/billing.py", "telegram_identity_label")
    billing_namespace = {"get_display_name": get_display_name}
    exec(
        compile(
            ast.Module(body=[billing_identity_label], type_ignores=[]),
            "telegram_identity_label",
            "exec",
        ),
        billing_namespace,
    )
    label_namespace = {
        "Any": Any,
        "cabinet_service": SimpleNamespace(public_username=cabinet_namespace["public_username"]),
        "billing": SimpleNamespace(
            telegram_identity_label=billing_namespace["telegram_identity_label"]
        ),
    }
    panel_label = _function(ROOT / "app/webapp.py", "_panel_user_label")
    exec(
        compile(ast.Module(body=[panel_label], type_ignores=[]), "_panel_user_label", "exec"),
        label_namespace,
    )
    assert label_namespace["_panel_user_label"](
        100, rows[0]["username"], rows[0]["display_name"], rows[0]["notes"]
    ) == "Комментарий владельца"
    assert label_namespace["_panel_user_label"](100, "id_100", "Telegram Full Name", "") == "ID 100"


def test_panel_username_resolution_keeps_real_handle_and_rejects_numeric_id():
    public_username = _function(ROOT / "app/cabinet_service.py", "public_username")
    namespace = {"Any": Any, "re": re, "normalize_username": normalize_username}
    exec(
        compile(ast.Module(body=[public_username], type_ignores=[]), "public_username", "exec"),
        namespace,
    )

    resolve = namespace["public_username"]
    assert resolve(100, "id_100", "@owner_nickname") == "owner_nickname"
    assert resolve(100, "100", "") == ""
    assert resolve(100, "real_telegram_user") == "real_telegram_user"


def test_message_dialogs_resolve_real_username_and_use_name_when_unavailable(tmp_path):
    db_path = tmp_path / "message-identities.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users(tg_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, notes TEXT);
            CREATE TABLE user_events(
                id INTEGER PRIMARY KEY, tg_id INTEGER, username TEXT,
                direction TEXT, created_at TEXT
            );
            INSERT INTO users VALUES(100, 'id_100', 'Telegram Full Name', 'Комментарий владельца');
            INSERT INTO users VALUES(200, '200', 'Member Name', 'Комментарий участника');
            INSERT INTO user_events VALUES(1, 100, '@id_100', 'in', '2026-01-01');
            INSERT INTO user_events VALUES(2, 100, 'owner_nickname', 'in', '2026-01-02');
            INSERT INTO user_events VALUES(3, 200, 'id_200', 'in', '2026-01-03');
            """
        )

    def connect(_db_path):
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    function = _function(ROOT / "app/user_events.py", "conversation_users_for_ids")
    namespace = {
        "Any": Any,
        "Iterable": Iterable,
        "Path": Path,
        "ensure_schema": lambda _db_path: None,
        "_connect": connect,
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "conversation_users_for_ids", "exec"),
        namespace,
    )

    rows = namespace["conversation_users_for_ids"]([100, 200], db_path=db_path)
    by_id = {row["tg_id"]: row for row in rows}
    assert by_id[100]["username"] == "id_100"
    assert by_id[100]["display_name"] == "Telegram Full Name"
    assert by_id[100]["notes"] == "Комментарий владельца"
    assert by_id[200]["username"] == "200"
    assert by_id[200]["display_name"] == "Member Name"

    all_dialogs = _function(ROOT / "app/user_events.py", "conversation_users")
    all_namespace = {
        "Any": Any,
        "Path": Path,
        "ensure_schema": lambda _db_path: None,
        "_connect": connect,
    }
    exec(
        compile(ast.Module(body=[all_dialogs], type_ignores=[]), "conversation_users", "exec"),
        all_namespace,
    )
    initial_rows = all_namespace["conversation_users"](db_path=db_path)
    initial_by_id = {row["tg_id"]: row for row in initial_rows}
    assert initial_by_id[100]["username"] == "id_100"
    assert initial_by_id[200]["username"] == "200"

    events_namespace = {
        "Any": Any,
        "Path": Path,
        "ensure_schema": lambda _db_path: None,
        "_connect": connect,
        "_event_from_row": lambda row: dict(row),
        "_event_filters": lambda **kwargs: (
            " WHERE events.tg_id=?" if kwargs.get("tg_id") else " WHERE events.tg_id>0",
            [kwargs["tg_id"]] if kwargs.get("tg_id") else [],
        ),
    }
    all_events = _function(ROOT / "app/user_events.py", "all_events")
    exec(
        compile(ast.Module(body=[all_events], type_ignores=[]), "all_events", "exec"),
        events_namespace,
    )
    paged_events = events_namespace["all_events"](tg_id=100, db_path=db_path)
    assert paged_events[0]["display_username"] == "id_100"
    assert paged_events[0]["display_name"] == "Telegram Full Name"

    recent_events = _function(ROOT / "app/user_events.py", "recent_all_events")
    exec(
        compile(ast.Module(body=[recent_events], type_ignores=[]), "recent_all_events", "exec"),
        events_namespace,
    )
    live_events = events_namespace["recent_all_events"](db_path=db_path)
    live_by_id = {event["id"]: event for event in live_events}
    assert live_by_id[1]["display_username"] == "id_100"
    assert live_by_id[3]["display_username"] == "200"
    assert live_by_id[3]["display_name"] == "Member Name"
