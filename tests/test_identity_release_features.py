from __future__ import annotations

import ast
import html
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

if "config" not in sys.modules:
    _config_spec = importlib.util.spec_from_file_location("config", APP / "config.example.py")
    assert _config_spec and _config_spec.loader
    _config_module = importlib.util.module_from_spec(_config_spec)
    sys.modules["config"] = _config_module
    _config_spec.loader.exec_module(_config_module)

import normalize_user_identities as migration
import user_identity
import webauthn_service


def test_canonical_display_name_order_and_normalization():
    assert user_identity.get_display_name({"tg_id": 7, "username": "@alice", "notes": "Client A"}) == "@alice"
    assert user_identity.get_display_name({"tg_id": 8, "username": "  ", "notes": "  Иван   Петров  "}) == "Иван Петров"
    assert user_identity.get_display_name({"tg_id": 9, "username": None, "display_name": "Telegram Full Name", "notes": "None"}) == "ID 9"
    assert user_identity.get_display_name({"tg_id": 10, "username": "None", "notes": "Комментарий"}) == "Комментарий"
    assert user_identity.get_display_name({"tg_id": 693845923, "username": None, "notes": "id_693845923", "email": "Anna_Pavlovna"}) == "ID 693845923"
    assert user_identity.get_display_name({"tg_id": 11, "username": "id_11", "notes": "Участник семьи"}) == "Участник семьи"
    assert user_identity.get_display_name({"tg_id": 12, "username": "   ", "notes": ""}) == "ID 12"
    assert user_identity.get_display_name({"tg_id": 13, "username": "bad name", "notes": "Операторский комментарий"}) == "Операторский комментарий"


@pytest.mark.parametrize("value", [None, "", "   ", "None", "null", "undefined", "12345", "id_77"])
def test_invalid_and_synthetic_usernames_are_blank(value):
    assert user_identity.normalize_username(value, tg_id=77) == ""


def test_long_username_and_emoji_comment_are_bounded_without_losing_identity():
    long_username = "A_" * 100
    normalized = user_identity.normalize_username(long_username, 88)
    assert len(normalized) == 100
    assert user_identity.get_display_name({"tg_id": 88, "username": long_username}) == "@" + normalized
    label = user_identity.get_display_name({"tg_id": 89, "username": " ", "notes": "🚀 Клиент 😀"})
    assert label == "🚀 Клиент 😀"
    assert len(user_identity.normalize_comment("😀" * 300)) == 255


def test_family_owner_and_member_share_the_same_display_rule():
    owner = {"tg_id": 100, "username": "", "notes": "Создатель семьи"}
    member = {"tg_id": 200, "username": None, "notes": "Участник семьи"}
    assert user_identity.get_display_name(owner) == "Создатель семьи"
    assert user_identity.get_display_name(member) == "Участник семьи"


def test_webauthn_origin_and_rp_id_follow_the_configured_https_domain(monkeypatch):
    monkeypatch.setattr(webauthn_service.config, "WEB_DOMAIN", "https://panel.example.test/fargovpn", raising=False)
    monkeypatch.setattr(webauthn_service.config, "BOT_PANEL_URL", "", raising=False)
    request = SimpleNamespace(
        headers={"host": "panel.example.test"},
        url=SimpleNamespace(scheme="http", netloc="panel.example.test"),
    )
    assert webauthn_service.webauthn_context(request) == ("panel.example.test", "https://panel.example.test")

    request.headers = {"host": "other.example.test"}
    with pytest.raises(ValueError, match="не совпадает"):
        webauthn_service.webauthn_context(request)


def test_webauthn_refuses_http_for_a_public_domain(monkeypatch):
    monkeypatch.setattr(webauthn_service.config, "WEB_DOMAIN", "http://panel.example.test", raising=False)
    monkeypatch.setattr(webauthn_service.config, "BOT_PANEL_URL", "", raising=False)
    request = SimpleNamespace(headers={"host": "panel.example.test"}, url=SimpleNamespace(scheme="http", netloc="panel.example.test"))
    with pytest.raises(ValueError, match="только по HTTPS"):
        webauthn_service.webauthn_context(request)


def test_login_page_keeps_password_fallback_and_passkey_button_and_javascript_parses(tmp_path):
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    node = next(
        item for item in ast.parse(source).body
        if isinstance(item, ast.FunctionDef) and item.name == "login_page"
    )
    node.decorator_list = []
    for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        argument.annotation = None
    namespace = {
        "html": html,
        "json": json,
        "config": SimpleNamespace(SERVICE_NAME="FargoVPN"),
        "public_path": lambda path: "/panel" + (path if path.startswith("/") else "/" + path),
        "page": lambda _request, _title, body: body,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "login_page", "exec"), namespace)
    body = namespace["login_page"](object())
    assert "Войти по паролю" in body
    assert "Войти по Face ID / Touch ID" in body
    assert 'autocomplete="username"' in body
    assert 'autocomplete="current-password"' in body
    assert 'method="post" action="/panel/login"' in body
    assert "fetch(f.action" not in body
    assert ".replace(/\\+/g,'-').replace(/\\//g,'_')" in body
    assert "api/webauthn/authentication/options" in body
    assert "api/webauthn/authentication/verify" in body
    scripts = [script for script in re.findall(r"<script[^>]*>(.*?)</script>", body, re.S) if script.strip()]
    assert len(scripts) == 2
    node_bin = os.environ.get("FARGOVPN_NODE") or shutil_which("node")
    if node_bin:
        for index, script in enumerate(scripts):
            path = tmp_path / f"login-{index}.js"
            path.write_text(script, encoding="utf-8")
            checked = subprocess.run([node_bin, "--check", str(path)], capture_output=True, text=True, timeout=20)
            assert checked.returncode == 0, checked.stderr
        profile_node = next(
            item for item in ast.parse(source).body
            if isinstance(item, ast.FunctionDef) and item.name == "profile_page"
        )
        profile_literals = [
            item.value for item in ast.walk(profile_node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
            and "function fromB64" in item.value and "function credentialJSON" in item.value
        ]
        assert profile_literals, "Не найден скрипт регистрации passkey в профиле"
        profile_script = re.search(r"<script>(.*?)</script>", profile_literals[0], re.S).group(1)
        assert ".replace(/\\+/g,'-').replace(/\\//g,'_')" in profile_script
        assert "setTimeout(()=>{if(confirm(" not in profile_script
        path = tmp_path / "profile-passkey.js"
        path.write_text(profile_script, encoding="utf-8")
        checked = subprocess.run([node_bin, "--check", str(path)], capture_output=True, text=True, timeout=20)
        assert checked.returncode == 0, checked.stderr


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _MigrationConnection:
    def __init__(self, users: list[dict], events: list[dict], columns: set[str] | None = None, has_events: bool = True, operations=None):
        self.users = users
        self.events = events
        self.columns = set(columns) if columns is not None else {"tg_id", "username", "display_name", "notes", "identity_source", "email"}
        self.has_events = has_events
        self.operations = operations if operations is not None else []

    def __enter__(self):
        self.operations.append("connect")
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        compact = " ".join(str(sql).split())
        if "information_schema.tables" in compact and "table_name='users'" in compact:
            return _Rows([(1,)]) if self.columns else _Rows([])
        if "information_schema.columns" in compact and "table_name='users'" in compact:
            return _Rows([(c,) for c in self.columns])
        if "information_schema.columns" in compact and "table_name='user_events'" in compact:
            return _Rows([(c,) for c in ("id", "tg_id", "username", "direction", "actor")]) if self.has_events else _Rows([])
        if "information_schema.tables" in compact and "table_name='user_events'" in compact:
            return _Rows([(1,)]) if self.has_events else _Rows([])
        if compact.startswith("SELECT ") and " FROM users ORDER BY tg_id" in compact:
            selected = compact.split("SELECT ", 1)[1].split(" FROM users", 1)[0].split(",")
            return _Rows([tuple(row.get(column) for column in selected) for row in sorted(self.users, key=lambda item: item["tg_id"])])
        if compact.startswith("SELECT id,tg_id,username,direction,actor FROM user_events"):
            return _Rows([
                (event["id"], event["tg_id"], event.get("username"), event.get("direction", "system"), event.get("actor", "web"))
                for event in self.events
            ])
        if compact.startswith("UPDATE users SET "):
            before_where = compact.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            names = [item.split("=", 1)[0] for item in before_where.split(",")]
            values = params[:-1]
            tg_id = int(params[-1])
            row = next(item for item in self.users if int(item["tg_id"]) == tg_id)
            row.update(dict(zip(names, values)))
            self.operations.append(("update_user", tg_id, dict(zip(names, values))))
            return _Rows([])
        if compact.startswith("UPDATE user_events SET username="):
            normalized, event_id = params
            row = next(item for item in self.events if int(item["id"]) == int(event_id))
            row["username"] = normalized
            self.operations.append(("update_event", int(event_id), normalized))
            return _Rows([])
        raise AssertionError(f"Unexpected migration SQL: {compact}")


def _migration_users():
    return [
        {"tg_id": 1, "username": "@alice", "display_name": "Alice\n Smith", "notes": "  Клиент A  "},
        {"tg_id": 2, "username": "None", "display_name": "None", "notes": "   "},
        {"tg_id": 3, "username": "   ", "display_name": "Bob", "notes": None},
        {"tg_id": 4, "username": "id_4", "display_name": "Member", "notes": "Семья"},
        {"tg_id": 5, "username": "12345", "display_name": "A", "notes": ""},
    ]


def test_identity_migration_backs_up_first_reports_changes_and_is_idempotent(tmp_path, monkeypatch):
    users, events = _migration_users(), [
        {"id": 1, "tg_id": 4, "username": "id_4", "direction": "in", "actor": "telegram:4"},
        {"id": 2, "tg_id": 1, "username": "alice", "direction": "in", "actor": "telegram:@alice"},
        {"id": 3, "tg_id": 4, "username": "member_real", "direction": "system", "actor": "web"},
    ]
    operations = []
    connection = _MigrationConnection(users, events, operations=operations)
    monkeypatch.setattr(migration.config, "DB_PATH", "unused", raising=False)
    monkeypatch.setattr(migration.config, "BACKUP_DATABASE_TIMEOUT_SECONDS", 60, raising=False)
    monkeypatch.setattr(migration.database_adapter, "database_url", lambda: "postgresql+psycopg://test", raising=False)

    def snapshot(path, **_kwargs):
        operations.append("backup")
        Path(path).write_text("-- fake but non-empty backup snapshot\n", encoding="utf-8")
        return Path(path)

    monkeypatch.setattr(migration, "postgres_snapshot", snapshot)
    monkeypatch.setattr(migration.database_adapter, "connect", lambda *_args, **_kwargs: connection)
    report = migration.run(backup_dir=str(tmp_path))
    assert operations[0] == "backup"
    assert report["users_checked"] == 5
    assert report["users_changed"] == 5
    assert report["usernames_normalized"] == 5
    assert report["comments_normalized"] == 2
    assert report["display_names_normalized"] == 2
    assert report["events_checked"] == 3
    assert report["event_usernames_normalized"] == 1
    assert report["historical_usernames_recovered"] == 0
    assert report["historical_usernames_ignored"] >= 1
    assert Path(report["backup"]).stat().st_mode & 0o777 == 0o600
    assert users[0]["username"] == "alice"
    assert users[0]["display_name"] == "Alice Smith"
    assert users[0]["notes"] == "Клиент A"
    assert users[1]["username"] is None and users[1]["display_name"] is None and users[1]["notes"] is None
    assert users[3]["username"] is None
    assert events[0]["username"] is None

    operations.clear()
    second = migration.run(backup_dir=str(tmp_path))
    assert second["users_checked"] == 5
    assert second["users_changed"] == 0
    assert second["event_usernames_normalized"] == 0
    assert second["historical_usernames_recovered"] == 0


def test_identity_migration_handles_old_schema_without_notes_or_event_table(tmp_path, monkeypatch):
    users = [{"tg_id": 99, "username": "None", "display_name": "Legacy"}]
    operations = []
    connection = _MigrationConnection(users, [], columns={"tg_id", "username", "display_name"}, has_events=False, operations=operations)
    monkeypatch.setattr(migration.config, "DB_PATH", "unused", raising=False)
    monkeypatch.setattr(migration.database_adapter, "database_url", lambda: "postgresql+psycopg://test", raising=False)
    monkeypatch.setattr(migration, "postgres_snapshot", lambda path, **_kwargs: (Path(path).write_text("backup", encoding="utf-8") or Path(path)))
    monkeypatch.setattr(migration.database_adapter, "connect", lambda *_args, **_kwargs: connection)
    report = migration.run(backup_dir=str(tmp_path))
    assert report["users_checked"] == 1
    assert report["users_changed"] == 1
    assert users[0]["username"] is None
    assert users[0]["display_name"] == "Legacy"


@pytest.mark.parametrize("family_size", [2, 3, 4])
def test_subscription_report_pages_500_records_and_never_exceeds_telegram_limit(family_size):
    source = (APP / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "subscription_report_text")
    fn.decorator_list = []
    now_seconds = 1_700_000_000
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE users(
            tg_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, expiry_time INTEGER,
            last_reminder_days INTEGER, enable INTEGER, uuid TEXT, email TEXT, sub_id TEXT,
            registration_status TEXT, notes TEXT, custom_payment_price INTEGER
        );
        CREATE TABLE telegram_profile_links(telegram_id INTEGER, profile_tg_id INTEGER);
    """)
    connection.executemany(
        "INSERT INTO users(tg_id,username,display_name,expiry_time,last_reminder_days,enable,uuid,email,sub_id,registration_status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (tid, f"user_{tid:03}", "😀" * 120, now_seconds * 1000 + 30 * 86_400_000, -1, 1, f"uuid-{tid}", f"{tid}@example", f"sub-{tid}", "active", "")
            for tid in range(1, 501)
        ],
    )
    connection.commit()
    from services import billing
    connection.executemany("INSERT INTO telegram_profile_links VALUES(?,1)", [(i,) for i in range(2, family_size + 1)])
    def family_price(profile_id, db_path=None):
        members = connection.execute("SELECT COUNT(*) FROM telegram_profile_links WHERE profile_tg_id=? AND telegram_id<>?", (profile_id, profile_id)).fetchone()[0]
        return 200 * (1 + members)
    namespace = {
        "database_adapter": SimpleNamespace(connect=lambda _path, timeout: connection),
        "config": SimpleNamespace(DB_PATH="unused", PAYMENT_PRICE=150, SUBSCRIPTION_DAYS=30),
        "time": SimpleNamespace(time=lambda: now_seconds),
        "html": html,
        "billing": SimpleNamespace(expected_payment_amount=family_price, telegram_identity_label=billing.telegram_identity_label),
        "logger": SimpleNamespace(warning=Mock(), debug=Mock()),
        "normalize_username": __import__("user_identity").normalize_username,
        "latest_trusted_telegram_usernames": __import__("user_identity").latest_trusted_telegram_usernames,
        "SUBSCRIPTION_REPORT_PAGE_SIZE": 8,
        "local_date_from_timestamp": lambda value: datetime.fromtimestamp(value).date(),
        "now_local": lambda: datetime.fromtimestamp(now_seconds),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "subscription_report_text", "exec"), namespace)
    first = namespace["subscription_report_text"](1)
    last = namespace["subscription_report_text"](63)
    assert "Страница 1/63" in first
    owner_card = first.split("\n\n", 1)[1].split("\n\n", 1)[0]
    assert f" · {family_size * 200} ₽" in owner_card
    assert first.count('<a href="tg://user?id=') == 8
    assert "Страница 63/63" in last
    assert last.count('<a href="tg://user?id=') == 4
    split_node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "split_subscription_report")
    split_node.decorator_list = []
    split_ns = {"re": re}
    exec(compile(ast.Module(body=[split_node], type_ignores=[]), "split_subscription_report", "exec"), split_ns)
    for page in (first, last):
        assert all(len(chunk.encode("utf-16-le")) // 2 <= 3900 for chunk in split_ns["split_subscription_report"](page))
    connection.close()


def test_identity_migration_is_safe_before_users_table_exists_on_fresh_install(tmp_path, monkeypatch):
    operations = []
    connection = _MigrationConnection([], [], columns=set(), has_events=False, operations=operations)
    monkeypatch.setattr(migration.config, "DB_PATH", "unused", raising=False)
    monkeypatch.setattr(migration.database_adapter, "database_url", lambda: "postgresql+psycopg://test", raising=False)

    def snapshot(path, **_kwargs):
        operations.append("backup")
        Path(path).write_text("-- empty database snapshot\n", encoding="utf-8")
        return Path(path)

    monkeypatch.setattr(migration, "postgres_snapshot", snapshot)
    monkeypatch.setattr(migration.database_adapter, "connect", lambda *_args, **_kwargs: connection)
    report = migration.run(backup_dir=str(tmp_path))
    assert operations[0] == "backup"
    assert report["users_checked"] == 0
    assert report["users_changed"] == 0
    assert report["skipped_reason"] == "users table does not exist yet; clean install"
    assert Path(report["backup"]).is_file()


def test_family_profile_accounts_uses_current_username_and_comment_without_event_fallback(monkeypatch):
    import services.billing as billing_module

    queries = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return list(self.rows)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            compact = " ".join(str(sql).split())
            queries.append(compact)
            if compact.startswith("SELECT profile_tg_id FROM telegram_profile_links"):
                return Result([(100,)])
            if compact.startswith("SELECT u.tg_id,"):
                return Result([
                    (100, "", "Owner", "Имя владельца"),
                    (200, "", "Member", "Имя участника"),
                ])
            raise AssertionError(compact)

    monkeypatch.setattr(billing_module.db, "connect", lambda *_args, **_kwargs: Connection())
    accounts = billing_module.profile_accounts(100)
    assert [row["label"] for row in accounts] == ["Имя владельца", "Имя участника"]
    assert all("FROM user_events" not in query for query in queries)


def test_bot_crm_list_unpacks_comment_and_displays_comment_fallback():
    source = (APP / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "send_crm_list")
    for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs):
        argument.annotation = None
    function.returns = None

    class Rows:
        def fetchall(self):
            return [(100, "", "", 1, 0, "uuid", "e@example.test", "sub", "active", "Клиент без ника")]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=()):
            return Rows()

    class Builder:
        def __init__(self):
            self.buttons = []

        def button(self, **kwargs):
            self.buttons.append(kwargs)

        def adjust(self, *_args):
            return None

        def as_markup(self):
            return self.buttons

    class Message:
        def __init__(self):
            self.answers = []

        async def answer(self, *args, **kwargs):
            self.answers.append((args, kwargs))

    import asyncio
    import services.billing as billing_module

    builder = Builder()
    namespace = {
        "asyncio": asyncio,
        "config": SimpleNamespace(DB_PATH="unused"),
        "database_adapter": SimpleNamespace(connect=lambda *_args, **_kwargs: Connection()),
        "billing": SimpleNamespace(
            family_profile_map=lambda *_args: {},
            subscription_label=lambda _account: "Активна",
            subscription_status_emoji=lambda _state: "🟢",
            telegram_identity_label=billing_module.telegram_identity_label,
        ),
        "InlineKeyboardBuilder": lambda: builder,
        "get_admin_menu": lambda: "admin-menu",
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "send_crm_list", "exec"), namespace)
    message = Message()
    asyncio.run(namespace["send_crm_list"](message))
    assert builder.buttons
    assert builder.buttons[0]["text"].startswith("🟢 Клиент без ника")
    assert builder.buttons[0]["callback_data"] == "crmv_100"
    assert message.answers and message.answers[0][1]["parse_mode"] == "HTML"


def test_live_message_dialogs_return_server_resolved_display_label():
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    start = source.index("@app.get(\"/api/panel/messages/feed\")")
    end = source.index("\n\nAPP_LOG_DEFAULT", start)
    endpoint = source[start:end]
    assert 'dialog["display_label"] = _panel_user_label(' in endpoint
    assert '"dialogs": changed_dialogs' in endpoint


def test_identity_migration_recovers_handle_from_latest_direct_telegram_update_only(tmp_path, monkeypatch):
    """The latest inbound Telegram handle wins; web/XUI/bot labels never do."""
    users = [
        {"tg_id": 422172767, "username": "Kirill", "display_name": "Kirill", "notes": "kiryan_avd", "identity_source": "3x-ui", "email": "Kirill_Avdeenko"},
        {"tg_id": 693845923, "username": "Anna", "display_name": "Anna", "notes": "id_693845923", "identity_source": "3x-ui", "email": "Anna_Pavlovna"},
    ]
    events = [
        {"id": 1, "tg_id": 422172767, "username": "kiryan_avd", "direction": "in", "actor": "telegram:@kiryan_avd"},
        {"id": 2, "tg_id": 422172767, "username": "Kirill", "direction": "system", "actor": "web"},
        {"id": 3, "tg_id": 693845923, "username": "Anna", "direction": "out", "actor": "telegram_bot"},
    ]
    connection = _MigrationConnection(users, events)
    monkeypatch.setattr(migration.config, "DB_PATH", "unused", raising=False)
    monkeypatch.setattr(migration.config, "BACKUP_DATABASE_TIMEOUT_SECONDS", 60, raising=False)
    monkeypatch.setattr(migration.database_adapter, "database_url", lambda: "postgresql+psycopg://test", raising=False)

    def snapshot(path, **_kwargs):
        Path(path).write_text("-- fake but non-empty backup snapshot\n", encoding="utf-8")
        return Path(path)

    monkeypatch.setattr(migration, "postgres_snapshot", snapshot)
    monkeypatch.setattr(migration.database_adapter, "connect", lambda *_args, **_kwargs: connection)
    report = migration.run(backup_dir=str(tmp_path))

    assert report["telegram_usernames_recovered"] == 1
    assert report["untrusted_panel_usernames_cleared"] == 1
    assert users[0]["username"] == "kiryan_avd"
    # The supplied backup has no trusted incoming Telegram snapshot or stored
    # @AnnaSam111 for this ID. Never invent/link the 3x-ui label @Anna.
    assert users[1]["username"] is None
    assert users[0]["notes"] == "kiryan_avd"
    assert users[1]["notes"] == "id_693845923"


