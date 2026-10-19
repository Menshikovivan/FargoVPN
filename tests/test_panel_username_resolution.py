"""Panel identity queries and labels must never treat Telegram IDs as usernames."""
import ast
from contextlib import contextmanager
import re
import sqlite3
import pytest
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

    namespace = {
        "Any": Any,
        "Iterable": Iterable,
        "Path": Path,
        "ensure_schema": lambda _db_path: None,
        "_connect": connect,
        "normalize_username": normalize_username,
    }
    function = _function(ROOT / "app/user_events.py", "conversation_users_for_ids")
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "conversation_users_for_ids", "exec"),
        namespace,
    )

    rows = namespace["conversation_users_for_ids"]([100, 200], db_path=db_path)
    by_id = {row["tg_id"]: row for row in rows}
    assert by_id[100]["username"] == ""
    assert by_id[100]["display_name"] == "Telegram Full Name"
    assert by_id[100]["notes"] == "Комментарий владельца"
    assert by_id[200]["username"] == ""
    assert by_id[200]["display_name"] == "Member Name"

    all_dialogs = _function(ROOT / "app/user_events.py", "conversation_users")
    all_namespace = dict(namespace)
    exec(
        compile(ast.Module(body=[all_dialogs], type_ignores=[]), "conversation_users", "exec"),
        all_namespace,
    )
    initial_rows = all_namespace["conversation_users"](db_path=db_path)
    initial_by_id = {row["tg_id"]: row for row in initial_rows}
    assert initial_by_id[100]["username"] == ""
    assert initial_by_id[200]["username"] == ""

    events_namespace = {
        **namespace,
        "_event_from_row": lambda row: dict(row),
        "logger": SimpleNamespace(warning=lambda *a, **k: None),
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
    assert paged_events[0]["display_username"] == ""
    assert paged_events[0]["display_name"] == "Telegram Full Name"

    recent_events = _function(ROOT / "app/user_events.py", "recent_all_events")
    exec(
        compile(ast.Module(body=[recent_events], type_ignores=[]), "recent_all_events", "exec"),
        events_namespace,
    )
    live_events = events_namespace["recent_all_events"](db_path=db_path)
    live_by_id = {event["id"]: event for event in live_events}
    assert live_by_id[1]["display_username"] == ""
    assert live_by_id[3]["display_username"] == ""
    assert live_by_id[3]["display_name"] == "Member Name"


def test_local_identity_resolver_rejects_conflicting_email_and_uuid():
    from user_identity import build_unique_identity_index, match_local_identity

    rows = [
        {"tg_id": 101, "email": "one@example.test", "uuid": "uuid-one"},
        {"tg_id": 202, "email": "two@example.test", "uuid": "uuid-two"},
    ]
    by_email, dup_email = build_unique_identity_index(rows, "email")
    by_uuid, dup_uuid = build_unique_identity_index(rows, "uuid")
    matched, conflict = match_local_identity("one@example.test", "uuid-two", by_email, by_uuid, dup_email, dup_uuid)
    assert matched is None and conflict is True
    matched, conflict = match_local_identity("one@example.test", "uuid-one", by_email, by_uuid, dup_email, dup_uuid)
    assert matched["tg_id"] == 101 and conflict is False


def test_local_identity_index_marks_duplicate_identity_as_ambiguous():
    from user_identity import build_unique_identity_index, match_local_identity

    rows = [
        {"tg_id": 101, "email": "shared@example.test", "uuid": "uuid-one"},
        {"tg_id": 202, "email": "SHARED@example.test", "uuid": "uuid-two"},
    ]
    by_email, dup_email = build_unique_identity_index(rows, "email")
    by_uuid, dup_uuid = build_unique_identity_index(rows, "uuid")
    assert "shared@example.test" in dup_email
    matched, conflict = match_local_identity("shared@example.test", "uuid-one", by_email, by_uuid, dup_email, dup_uuid)
    assert matched is None and conflict is True


def test_3xui_sync_never_overwrites_users_on_conflicting_email_uuid(tmp_path, monkeypatch):
    """A conflicting live client must be detached, not saved over either owner."""
    import importlib
    import sys
    import types

    psycopg_stub = types.ModuleType("psycopg")
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_stub)
    config_spec = importlib.util.spec_from_file_location("config", ROOT / "app" / "config.example.py")
    config_module = importlib.util.module_from_spec(config_spec)
    assert config_spec.loader is not None
    config_spec.loader.exec_module(config_module)
    monkeypatch.setitem(sys.modules, "config", config_module)
    xui_api = importlib.import_module("services.xui_api")
    db_path = tmp_path / "xui-sync.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE users(
                tg_id INTEGER PRIMARY KEY, username TEXT, uuid TEXT, email TEXT,
                expiry_time INTEGER DEFAULT 0, enable INTEGER DEFAULT 1,
                up INTEGER DEFAULT 0, down INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                sub_id TEXT, last_reminder_days INTEGER DEFAULT -1, notes TEXT,
                last_online TEXT, last_online_ts INTEGER DEFAULT 0, last_sync_at TEXT,
                identity_source TEXT, identity_updated_at TEXT, display_name TEXT,
                registered_at TEXT, registration_status TEXT
            );
            INSERT INTO users(tg_id,username,uuid,email,expiry_time,enable,up,down,total,sub_id,last_reminder_days,notes)
            VALUES(101,'alice','uuid-a','alice@example.test',1000,1,10,20,100,'sub-a',-1,'Alice comment');
            INSERT INTO users(tg_id,username,uuid,email,expiry_time,enable,up,down,total,sub_id,last_reminder_days,notes)
            VALUES(202,'bob','uuid-b','bob@example.test',2000,1,30,40,200,'sub-b',-1,'Bob comment');
        """)

    def connect(path, timeout=20):
        con = sqlite3.connect(path, timeout=timeout)
        con.create_function("GREATEST", -1, lambda *values: max(v for v in values if v is not None))
        return con

    monkeypatch.setattr(xui_api.database_adapter, "connect", connect)
    changed = xui_api.sync_snapshot_to_db({"clients": [{
        "tg_id": 202, "email": "alice@example.test", "uuid": "uuid-b",
        "expiry_time": 9999999999999, "enable": True, "up": 999,
        "down": 888, "total": 100000, "sub_id": "incoming-sub", "last_online_ts": 0,
    }]}, db_path=str(db_path))
    assert changed > 0
    with sqlite3.connect(db_path) as connection:
        accounts = {row[0]: row[1:] for row in connection.execute(
            "SELECT tg_id,username,uuid,email,expiry_time,enable,up,down,sub_id FROM users"
        )}
    assert accounts[101][0:3] == ("alice", "uuid-a", "alice@example.test")
    assert accounts[202][0:3] == ("bob", "uuid-b", "bob@example.test")
    assert accounts[101][4] == 1 and accounts[202][4] == 1
    # The ambiguous panel client stays in a detached negative-ID record.
    detached = [values for tg_id, values in accounts.items() if tg_id < 0]
    assert len(detached) == 1
    assert detached[0][2] == "alice@example.test"
    package = sys.modules.get("services")
    sys.modules.pop("services.xui_api", None)
    if package is not None and getattr(package, "xui_api", None) is xui_api:
        delattr(package, "xui_api")


def test_unlinked_3xui_rows_never_render_user_zero_or_destructive_actions():
    actions = _function(ROOT / "app/webapp.py", "_user_actions_html")
    namespace = {}
    exec(compile(ast.Module(body=[actions], type_ignores=[]), "_user_actions_html", "exec"), namespace)
    render = namespace["_user_actions_html"]

    unlinked = render(0, False, True)
    assert "/users/import-identities" in unlinked
    assert "/users/id/0" not in unlinked
    assert "/users/0/days" not in unlinked
    assert "/users/0/toggle" not in unlinked
    assert "/users/0/delete" not in unlinked

    conflicted = render(123, True, True)
    assert "/users/import-identities" in conflicted
    assert "/users/id/123" not in conflicted
    assert "/users/123/delete" not in conflicted

    linked = render(456, False, True)
    assert "/users/id/456" in linked
    assert "/users/456/days" in linked
    assert "/users/456/toggle" in linked
    assert "/users/456/delete" in linked


@pytest.mark.parametrize("panel_comment", ["Брат Мишы Авхадеева", "id", "id_1180284364"])
def test_3xui_snapshot_clears_synthetic_handle_without_touching_comment_or_subscription(tmp_path, monkeypatch, panel_comment):
    import importlib
    import sys
    import types

    monkeypatch.setitem(sys.modules, "psycopg", types.ModuleType("psycopg"))
    config_spec = importlib.util.spec_from_file_location("config", ROOT / "app" / "config.example.py")
    config_module = importlib.util.module_from_spec(config_spec)
    assert config_spec.loader is not None
    config_spec.loader.exec_module(config_module)
    monkeypatch.setitem(sys.modules, "config", config_module)
    xui_api = importlib.import_module("services.xui_api")
    db_path = tmp_path / "canonical-identity-sync.sqlite"
    tg_id = 1180284364
    email = "id_1180284364+1180284364"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE users(
                tg_id INTEGER PRIMARY KEY, username TEXT, uuid TEXT, email TEXT,
                expiry_time INTEGER DEFAULT 0, enable INTEGER DEFAULT 1,
                up INTEGER DEFAULT 0, down INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                sub_id TEXT, last_reminder_days INTEGER DEFAULT -1, notes TEXT,
                last_online TEXT, last_online_ts INTEGER DEFAULT 0, last_sync_at TEXT,
                identity_source TEXT, identity_updated_at TEXT, display_name TEXT,
                registered_at TEXT, registration_status TEXT
            );
        """)
        connection.execute(
            """INSERT INTO users(tg_id,username,uuid,email,expiry_time,enable,up,down,total,sub_id,last_reminder_days,notes,identity_source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tg_id, "id", "uuid-brother", email, 0, 1, 0, 0, 0, "sub-brother", -1,
             "Брат Мишы Авхадеева", "3x-ui"),
        )

    def connect(path, timeout=20):
        con = sqlite3.connect(path, timeout=timeout)
        con.create_function("GREATEST", -1, lambda *values: max(v for v in values if v is not None))
        return con

    monkeypatch.setattr(xui_api.database_adapter, "connect", connect)
    changed = xui_api.sync_snapshot_to_db({"clients": [{
        "tg_id": tg_id, "email": email, "uuid": "uuid-brother", "expiry_time": 0,
        "enable": True, "up": 0, "down": 0, "total": 0, "sub_id": "sub-brother",
        "last_online_ts": 0, "comment": panel_comment,
    }]}, db_path=str(db_path))
    assert changed > 0
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT username,uuid,email,sub_id,notes FROM users WHERE tg_id=?", (tg_id,)
        ).fetchone()
    assert row == (None, "uuid-brother", email, "sub-brother", "Брат Мишы Авхадеева")
    package = sys.modules.get("services")
    sys.modules.pop("services.xui_api", None)
    if package is not None and getattr(package, "xui_api", None) is xui_api:
        delattr(package, "xui_api")


def test_3xui_merge_never_copies_synthetic_name_into_real_telegram_profile(tmp_path, monkeypatch):
    """Merging a legacy panel row must not reintroduce an email-derived pseudo handle."""
    import importlib
    import sys
    import types

    monkeypatch.setitem(sys.modules, "psycopg", types.ModuleType("psycopg"))
    config_spec = importlib.util.spec_from_file_location("config", ROOT / "app" / "config.example.py")
    config_module = importlib.util.module_from_spec(config_spec)
    assert config_spec.loader is not None
    config_spec.loader.exec_module(config_module)
    monkeypatch.setitem(sys.modules, "config", config_module)
    xui_api = importlib.import_module("services.xui_api")

    db_path = tmp_path / "xui-identity-merge.sqlite"
    tg_id = 1180284364
    email = "id_1180284364+1180284364"
    comment = "Брат Мишы Авхадеева"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE users(
                tg_id INTEGER PRIMARY KEY, username TEXT, uuid TEXT, email TEXT,
                expiry_time INTEGER DEFAULT 0, enable INTEGER DEFAULT 1,
                up INTEGER DEFAULT 0, down INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                sub_id TEXT, last_reminder_days INTEGER DEFAULT -1, notes TEXT,
                last_online TEXT, last_online_ts INTEGER DEFAULT 0, last_sync_at TEXT,
                identity_source TEXT, identity_updated_at TEXT, display_name TEXT,
                registered_at TEXT, registration_status TEXT
            );
        """)
        connection.execute(
            """INSERT INTO users(tg_id,username,uuid,email,expiry_time,enable,up,down,total,sub_id,
                                  last_reminder_days,notes,identity_source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (-9180284364, "id_1180284364", "legacy-uuid", email, 0, 1, 0, 0, 0,
             "legacy-sub", -1, comment, "3x-ui"),
        )
        connection.execute(
            """INSERT INTO users(tg_id,username,uuid,email,expiry_time,enable,up,down,total,sub_id,
                                  last_reminder_days,notes,identity_source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tg_id, "id", "real-uuid", email, 0, 1, 0, 0, 0,
             "real-sub", -1, comment, "3x-ui"),
        )

    def connect(path, timeout=20):
        con = sqlite3.connect(path, timeout=timeout)
        con.create_function("GREATEST", -1, lambda *values: max(v for v in values if v is not None))
        return con

    monkeypatch.setattr(xui_api.database_adapter, "connect", connect)

    # Construct a legacy state where the email key resolved to the negative-ID
    # row while the UUID independently confirms the positive Telegram row.
    # This forces the defensive legacy-merge branch without weakening its checks.
    with sqlite3.connect(db_path) as connection:
        negative_row = dict(zip(
            [column[1] for column in connection.execute("PRAGMA table_info(users)")],
            connection.execute("SELECT * FROM users WHERE tg_id<0").fetchone(),
        ))
    monkeypatch.setattr(
        xui_api, "match_local_identity",
        lambda *args, **kwargs: (negative_row, False),
    )

    changed = xui_api.sync_snapshot_to_db({"clients": [{
        "tg_id": tg_id, "email": email, "uuid": "real-uuid", "expiry_time": 0,
        "enable": True, "up": 0, "down": 0, "total": 0, "sub_id": "real-sub",
        "last_online_ts": 0, "comment": comment,
    }]}, db_path=str(db_path))
    assert changed > 0
    with sqlite3.connect(db_path) as connection:
        users = connection.execute(
            "SELECT tg_id,username,uuid,email,sub_id,notes FROM users ORDER BY tg_id"
        ).fetchall()
    assert users == [(tg_id, None, "real-uuid", email, "real-sub", comment)]

    package = sys.modules.get("services")
    sys.modules.pop("services.xui_api", None)
    if package is not None and getattr(package, "xui_api", None) is xui_api:
        delattr(package, "xui_api")
