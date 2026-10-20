import ast
import asyncio
from datetime import datetime
import html
from html.parser import HTMLParser
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from db import CompatRow
from services import billing
from user_identity import latest_trusted_telegram_usernames, normalize_username


ROOT = Path(__file__).resolve().parents[1]
NOW_SECONDS = 1_700_000_000
NOW_MS = NOW_SECONDS * 1000


def _report_function(connection, logger=None):
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "subscription_report_text"
    )
    node.decorator_list = []
    def fixture_price(profile_id, db_path=None):
        # This renderer test owns its storage boundary; never use runtime PG.
        if not isinstance(connection, sqlite3.Connection):
            return 150
        custom = connection.execute("SELECT custom_payment_price FROM users WHERE tg_id=?", (profile_id,)).fetchone()
        if custom and custom[0] is not None:
            return int(custom[0])
        count = connection.execute("SELECT COUNT(*) FROM telegram_profile_links WHERE profile_tg_id=? AND telegram_id<>?", (profile_id, profile_id)).fetchone()[0]
        return 150 * (1 + int(count))
    namespace = {
        "database_adapter": SimpleNamespace(connect=lambda _path, timeout: connection),
        "config": SimpleNamespace(DB_PATH="unused", PAYMENT_PRICE=150),
        "time": SimpleNamespace(time=lambda: NOW_SECONDS),
        "html": html,
        "billing": SimpleNamespace(telegram_identity_label=billing.telegram_identity_label, expected_payment_amount=fixture_price),
        "logger": logger or SimpleNamespace(warning=Mock(), debug=Mock()),
        "normalize_username": normalize_username,
        "latest_trusted_telegram_usernames": latest_trusted_telegram_usernames,
        "SUBSCRIPTION_REPORT_PAGE_SIZE": 8,
        "re": re,
        "local_date_from_timestamp": lambda value: datetime.fromtimestamp(value).date(),
        "now_local": lambda: datetime.fromtimestamp(NOW_SECONDS),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "subscription_report_text", "exec"), namespace)
    return namespace["subscription_report_text"]


def test_family_report_tracks_owner_expiry_extension_expiration_and_unlink():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE users (
            tg_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, expiry_time INTEGER,
            last_reminder_days INTEGER, enable INTEGER, uuid TEXT, email TEXT, sub_id TEXT,
            registration_status TEXT, notes TEXT, custom_payment_price INTEGER
        );
        CREATE TABLE telegram_profile_links (telegram_id INTEGER, profile_tg_id INTEGER);
        CREATE TABLE user_events (id INTEGER PRIMARY KEY, tg_id INTEGER, username TEXT);
        """
    )
    connection.executemany(
        """INSERT INTO users
           (tg_id,username,display_name,expiry_time,last_reminder_days,enable,uuid,email,sub_id,registration_status,notes)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (100, "owner", "Owner Name", NOW_MS + 86_400_000, -1, 1, "owner-uuid", "owner@example", "owner-sub", "active", ""),
            (200, "", "Telegram Иван Петров", 0, -1, 1, "", "", "", "active", "Иван Петров"),
            (300, "", "", 0, -1, 1, "", "", "", "active", ""),
            (400, "lifetime", "Lifetime User", 0, -1, 1, "lifetime-uuid", "lifetime@example", "lifetime-sub", "active", ""),
        ],
    )
    connection.execute("INSERT INTO telegram_profile_links VALUES(200,100)")
    connection.execute("INSERT INTO telegram_profile_links VALUES(100,100)")
    connection.execute("INSERT INTO telegram_profile_links VALUES(300,300)")
    report = _report_function(connection)

    first = report()
    assert 'Иван Петров' in first and 'href="https://t.me/Иван' not in first
    assert 'href="https://t.me/owner"' in first and '<b>@owner</b>' in first
    assert '<b>ID 300</b>' in first and 'href="https://t.me/300"' not in first
    assert "👨‍👩‍👧 Тип: Семейная" in first
    assert "👨‍👩‍👧 Тип: Обычная" in first
    assert 'href="https://t.me/lifetime"' in first and '<b>@lifetime</b>' in first and "Активна · бессрочно" in first
    assert "истёк 01.01.1970" not in first
    assert "Статус: ⚪ Без подписки" in first
    assert "Страница 1/1" in first

    connection.execute("UPDATE users SET expiry_time=? WHERE tg_id=100", (NOW_MS - 1,))
    expired = report()
    assert 'Иван Петров' in expired and 'href="tg://user?id=' not in expired
    assert "Статус: 🔴 Истекла" in expired
    assert "❌" not in expired

    connection.execute("UPDATE users SET expiry_time=0 WHERE tg_id=100")
    family_lifetime = report()
    assert 'Иван Петров' in family_lifetime and 'href="tg://user?id=' not in family_lifetime
    assert "Статус: 🟢 Активна · бессрочно" in family_lifetime
    assert "👨‍👩‍👧 Тип: Семейная" in family_lifetime
    assert "истёк 01.01.1970" not in family_lifetime

    connection.execute("UPDATE users SET expiry_time=? WHERE tg_id=100", (NOW_MS + 20 * 86_400_000,))
    assert 'Иван Петров' in report() and 'href="tg://user?id=' not in report()

    connection.execute("DELETE FROM telegram_profile_links WHERE telegram_id=200")
    unlinked = report()
    assert 'Иван Петров' in unlinked and 'href="tg://user?id=' not in unlinked
    assert "Статус: ⚪ Без подписки" in unlinked

    connection.execute("INSERT INTO user_events VALUES(1,200,'family_member')")
    assert 'Иван Петров' in report() and 'href="tg://user?id=' not in report()
    connection.close()


def test_subscription_report_skips_null_rows_and_handles_compat_row_slices():
    row = (
        "alice", "Alice", 123, NOW_MS + 86_400_000, -1, 1, "alice-uuid", "", "",
        "active", None, None, None, None, None, None, None, "", 0, None, None,
    )

    class Result:
        def fetchall(self):
            return [None, CompatRow(Row(row))]

    class Row(tuple):
        @property
        def _mapping(self):
            return {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            return Result()

    logger = SimpleNamespace(warning=Mock(), debug=Mock())
    report = _report_function(Connection(), logger)()

    assert 'href="https://t.me/alice"' in report and '<b>@alice</b>' in report
    logger.warning.assert_called_once()


def test_subscription_report_fails_explicitly_when_all_rows_are_null():
    class Result:
        def fetchall(self):
            return [None, None]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            return Result()

    logger = SimpleNamespace(warning=Mock(), debug=Mock())

    with pytest.raises(RuntimeError, match="только пустые строки"):
        _report_function(Connection(), logger)()

    logger.warning.assert_called_once()


def test_subscription_report_chunks_preserve_lines_and_html_entities():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "split_subscription_report"
    )
    namespace = {"list": list, "re": re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "split_subscription_report", "exec"), namespace)
    text = "<b>Подписки</b>\n" + "".join(
        f"• {html.escape(f'Пользователь & <{index}>')} — активна\n"
        for index in range(500)
    )
    text += "• " + html.escape("Имя &<" * 1000) + " — активна"

    chunks = namespace["split_subscription_report"](text)

    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3900 for chunk in chunks)
    for chunk in chunks:
        parser = HTMLParser()
        parser.feed(chunk)
        parser.close()
        assert not re.search(r"&(amp|lt|gt|quot|#\d+)$", chunk)

    sender = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "send_subscription_report"
    )
    sender.decorator_list = []
    for argument in (*sender.args.posonlyargs, *sender.args.args, *sender.args.kwonlyargs):
        argument.annotation = None
    sender.args.vararg = None
    sender.args.kwarg = None
    sender.returns = None

    menu = object()
    answer = AsyncMock()
    sender_namespace = {
        "asyncio": asyncio,
        "subscription_report_text": lambda _page_no=1: text,
        "split_subscription_report": namespace["split_subscription_report"],
        "get_admin_menu": lambda: menu,
        "subscription_report_keyboard": lambda *_args: menu,
        "re": re,
    }
    exec(
        compile(ast.Module(body=[sender], type_ignores=[]), "send_subscription_report", "exec"),
        sender_namespace,
    )
    asyncio.run(sender_namespace["send_subscription_report"](SimpleNamespace(answer=answer)))
    assert answer.await_count == len(chunks)
    assert answer.await_args_list[-1].kwargs["reply_markup"] is menu
    assert all(call.kwargs["parse_mode"] == "HTML" for call in answer.await_args_list)
    assert "".join(call.args[0] for call in answer.await_args_list) == text


def test_admin_subscription_callback_reaches_report():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "dynamic_admin_menu"
    )
    node.decorator_list = []
    for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        argument.annotation = None
    node.args.vararg = None
    node.args.kwarg = None
    node.returns = None

    import asyncio
    from unittest.mock import AsyncMock

    report = AsyncMock()
    acknowledge = AsyncMock()
    namespace = {
        "config": SimpleNamespace(ADMIN_IDS=[7]),
        "safe_callback_answer": acknowledge,
        "send_subscription_report": report,
        "re": re,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "dynamic_admin_menu", "exec"), namespace)
    message = SimpleNamespace()
    call = SimpleNamespace(from_user=SimpleNamespace(id=7), message=message, data="admin:subscriptions")

    asyncio.run(namespace["dynamic_admin_menu"](call, SimpleNamespace()))

    acknowledge.assert_awaited_once_with(call, "Формирую отчёт подписок…")
    report.assert_awaited_once_with(message)


def test_admin_subscription_callback_reports_failure_to_user_and_logs_it():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "dynamic_admin_menu"
    )
    node.decorator_list = []
    for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        argument.annotation = None
    node.args.vararg = None
    node.args.kwarg = None
    node.returns = None

    report = AsyncMock(side_effect=RuntimeError("database unavailable"))
    acknowledge = AsyncMock()
    logger = SimpleNamespace(exception=Mock())
    menu = object()
    answer = AsyncMock()
    namespace = {
        "config": SimpleNamespace(ADMIN_IDS=[7]),
        "safe_callback_answer": acknowledge,
        "send_subscription_report": report,
        "logger": logger,
        "get_admin_menu": lambda: menu,
        "re": re,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "dynamic_admin_menu", "exec"), namespace)
    message = SimpleNamespace(answer=answer)
    call = SimpleNamespace(from_user=SimpleNamespace(id=7), message=message, data="admin:subscriptions")

    asyncio.run(namespace["dynamic_admin_menu"](call, SimpleNamespace()))

    acknowledge.assert_awaited_once_with(call, "Формирую отчёт подписок…")
    report.assert_awaited_once_with(message)
    answer.assert_awaited_once()
    assert "Не удалось сформировать или отправить отчёт подписок" in answer.await_args.args[0]
    assert answer.await_args.kwargs["reply_markup"] is menu
    logger.exception.assert_called_once()


def test_home_menu_passes_current_telegram_identity_to_cabinet_link():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "send_home"
    )
    node.decorator_list = []
    for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        argument.annotation = None
    node.args.vararg = None
    node.args.kwarg = None
    node.returns = None

    resolve_user = AsyncMock(return_value=None)
    get_user_menu = AsyncMock(return_value="menu")
    namespace = {
        "asyncio": asyncio,
        "billing": SimpleNamespace(payment_profile_id=lambda user_id, _path: user_id),
        "config": SimpleNamespace(DB_PATH="unused", PAYMENT_PRICE=150),
        "resolve_user": resolve_user,
        "home_text": lambda **_kwargs: "home",
        "get_user_menu_async": get_user_menu,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "send_home", "exec"), namespace)
    message = SimpleNamespace(answer=AsyncMock())

    asyncio.run(namespace["send_home"](message, 10, "alice", "Alice Example"))

    resolve_user.assert_awaited_once_with(10, None)
    get_user_menu.assert_awaited_once_with(10, None, "alice", "Alice Example")


def test_subscription_report_recovers_same_id_telegram_handles_and_links_by_username():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE users (
            tg_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, expiry_time INTEGER,
            last_reminder_days INTEGER, enable INTEGER, uuid TEXT, email TEXT, sub_id TEXT,
            registration_status TEXT, notes TEXT, custom_payment_price INTEGER
        );
        CREATE TABLE telegram_profile_links (telegram_id INTEGER, profile_tg_id INTEGER);
        CREATE TABLE user_events (
            id INTEGER PRIMARY KEY, tg_id INTEGER, username TEXT,
            direction TEXT, actor TEXT, created_at TEXT
        );
        """
    )
    connection.executemany(
        """INSERT INTO users
           (tg_id,username,display_name,expiry_time,last_reminder_days,enable,uuid,email,sub_id,registration_status,notes)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (1180284364, "id", "Telegram display name", NOW_MS + 30 * 86_400_000, -1, 1, "uuid-brother", "id_1180284364+1180284364", "sub-brother", "active", "Брат Мишы Авхадеева"),
            (422172767, "Kirill", "", NOW_MS + 30 * 86_400_000, -1, 1, "uuid-kirill", "Kirill_Avdeenko+422172767", "sub-kirill", "active", "kiryan_avd"),
            (693845923, "Anna", "", NOW_MS + 30 * 86_400_000, -1, 1, "uuid-anna", "Anna_Pavlovna+693845923", "sub-anna", "active", "AnnaSam111"),
        ],
    )
    # Incoming Telegram snapshots for each exact ID are stronger than polluted
    # users.username values and later non-Telegram/system entries.
    connection.executemany(
        "INSERT INTO user_events VALUES(?,?,?,?,?,?)",
        [
            (1, 422172767, "kiryan_avd", "in", "telegram:@kiryan_avd", "2026-10-18 10:00:00"),
            (2, 422172767, "Kirill", "system", "web", "2026-10-19 10:00:00"),
            (3, 693845923, "AnnaSam111", "in", "telegram:@AnnaSam111", "2026-10-18 10:00:00"),
            (4, 693845923, "Anna", "out", "telegram_bot", "2026-10-19 10:00:00"),
            (5, 1180284364, "id", "in", "telegram:1180284364", "2026-10-18 10:00:00"),
        ],
    )
    report = _report_function(connection)()
    assert "Брат Мишы Авхадеева" in report
    assert '<a href="https://t.me/kiryan_avd"' in report and "<b>@kiryan_avd</b>" in report
    assert '<a href="https://t.me/AnnaSam111"' in report and "<b>@AnnaSam111</b>" in report
    assert 'href="tg://user?id=1180284364"' in report
    assert 'href="tg://user?id=422172767"' not in report
    assert 'href="tg://user?id=693845923"' not in report
    assert 'href="https://t.me/Брат' not in report
    connection.close()
