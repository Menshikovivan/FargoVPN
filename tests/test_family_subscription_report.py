import ast
from datetime import datetime
import html
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from services import billing


ROOT = Path(__file__).resolve().parents[1]
NOW_SECONDS = 1_700_000_000
NOW_MS = NOW_SECONDS * 1000


def _report_function(connection):
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "subscription_report_text"
    )
    node.decorator_list = []
    namespace = {
        "database_adapter": SimpleNamespace(connect=lambda _path, timeout: connection),
        "config": SimpleNamespace(DB_PATH="unused"),
        "time": SimpleNamespace(time=lambda: NOW_SECONDS),
        "html": html,
        "billing": billing,
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
            registration_status TEXT
        );
        CREATE TABLE telegram_profile_links (telegram_id INTEGER, profile_tg_id INTEGER);
        CREATE TABLE user_events (id INTEGER PRIMARY KEY, tg_id INTEGER, username TEXT);
        """
    )
    connection.executemany(
        """INSERT INTO users
           (tg_id,username,display_name,expiry_time,last_reminder_days,enable,uuid,email,sub_id,registration_status)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        [
            (100, "owner", "Owner Name", NOW_MS + 86_400_000, -1, 1, "owner-uuid", "owner@example", "owner-sub", "active"),
            (200, "", "Иван Петров", 0, -1, 1, "", "", "", "active"),
            (300, "", "", 0, -1, 1, "", "", "", "active"),
        ],
    )
    connection.execute("INSERT INTO telegram_profile_links VALUES(200,100)")
    report = _report_function(connection)

    first = report()
    assert "Иван Петров — семейная · до " in first
    assert "Без подписки 🟡" in first
    assert "Иван Петров — бессрочно" not in first

    connection.execute("UPDATE users SET expiry_time=? WHERE tg_id=100", (NOW_MS - 1,))
    expired = report()
    assert "Иван Петров — семейная · истёк " in expired
    assert "🟡" in expired
    assert "❌" not in expired

    connection.execute("UPDATE users SET expiry_time=? WHERE tg_id=100", (NOW_MS + 20 * 86_400_000,))
    assert "Иван Петров — семейная · до " in report()

    connection.execute("DELETE FROM telegram_profile_links WHERE telegram_id=200")
    unlinked = report()
    assert "Иван Петров — Без подписки 🟡" in unlinked
    assert "Иван Петров — бессрочно" not in unlinked

    connection.execute("INSERT INTO user_events VALUES(1,200,'family_member')")
    assert "@family_member — Без подписки 🟡" in report()
    connection.close()
