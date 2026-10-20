from __future__ import annotations

import ast
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / "app"


def _load_live_feed_functions():
    source = (ROOT / "user_events.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="user_events.py")
    wanted = {"recent_events", "recent_all_events"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {node.name for node in functions} == wanted

    @contextmanager
    def connect(db_path=None):
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    namespace = {
        "Any": Any,
        "Path": Path,
        "ensure_schema": lambda db_path=None: None,
        "_connect": connect,
        "_event_from_row": lambda row: dict(row),
        "normalize_username": lambda username, tg_id: username or "",
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), "user_events_feed_functions.py", "exec"), namespace)
    return namespace["recent_events"], namespace["recent_all_events"]


def _seed_journal(path: Path):
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE user_events(
            id INTEGER PRIMARY KEY, tg_id INTEGER, direction TEXT, message_kind TEXT,
            event_type TEXT, text TEXT, created_at TEXT, delivery_status TEXT,
            success INTEGER, metadata TEXT
        )""")
        connection.execute("CREATE TABLE users(tg_id INTEGER, username TEXT, display_name TEXT, notes TEXT)")
        connection.execute("INSERT INTO users VALUES(123, 'tester', 'Tester', '')")
        rows = [
            (event_id, 123, 'in' if event_id % 2 else 'out', 'message', 'telegram_message',
             f'event {event_id}', f'2026-10-20 10:00:{event_id % 60:02d}',
             'received' if event_id % 2 else 'delivered', 1, '{}')
            for event_id in range(100, 130)
        ]
        rows.append((131, 123, 'out', 'message', 'telegram_message', 'newer committed first',
                     '2026-10-20 10:00:59', 'delivered', 1, '{}'))
        connection.executemany(
            "INSERT INTO user_events(id,tg_id,direction,message_kind,event_type,text,created_at,delivery_status,success,metadata) VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def test_chat_poll_overlap_recovers_lower_id_that_committed_after_cursor(tmp_path):
    recent_events, _ = _load_live_feed_functions()
    db_path = tmp_path / "late-chat-event.sqlite"
    _seed_journal(db_path)
    with sqlite3.connect(db_path) as connection:
        # Simulates an open PostgreSQL transaction whose lower sequence ID becomes visible late.
        connection.execute(
            "INSERT INTO user_events(id,tg_id,direction,message_kind,event_type,text,created_at,delivery_status,success,metadata) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (130,123,'in','message','telegram_message','late commit','2026-10-20 10:00:58','received',1,'{}'),
        )
    assert recent_events(123, after_id=131, overlap=0, db_path=db_path) == []
    recovered = recent_events(123, after_id=131, overlap=5, db_path=db_path)
    assert [event["id"] for event in recovered] == [127, 128, 129, 130, 131]
    assert recovered[-2]["text"] == "late commit"


def test_global_poll_overlap_recovers_lower_id_that_committed_after_cursor(tmp_path):
    _, recent_all_events = _load_live_feed_functions()
    db_path = tmp_path / "late-global-event.sqlite"
    _seed_journal(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO user_events(id,tg_id,direction,message_kind,event_type,text,created_at,delivery_status,success,metadata) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (130,123,'in','message','telegram_message','late global commit','2026-10-20 10:00:58','received',1,'{}'),
        )
    assert recent_all_events(after_id=131, overlap=0, db_path=db_path) == []
    recovered = recent_all_events(after_id=131, overlap=5, db_path=db_path)
    assert [event["id"] for event in recovered] == [127, 128, 129, 130, 131]
    assert recovered[-2]["text"] == "late global commit"
