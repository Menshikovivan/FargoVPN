"""Persistent user conversation, media metadata and unread-message state.

The Telegram bot and web panel share one append-only SQLite journal. Binary
media is not stored in SQLite: only compact Telegram ``file_id`` metadata is
kept. A small ``user_message_state`` table makes unread polling inexpensive.
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import config

logger = logging.getLogger(__name__)

MAX_TEXT = 8_000
MAX_METADATA = 12_000
MAX_PREVIEW = 500
MESSAGE_EVENT_TYPE = "telegram_message"
UNREAD_EVENT_TYPES = frozenset(
    {
        MESSAGE_EVENT_TYPE,
        "telegram_photo",
        "telegram_video",
        "telegram_document",
        "telegram_audio",
        "telegram_voice",
        "telegram_sticker",
    }
)
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()
_UNREAD_TRIGGER = "trg_user_events_unread_insert_v268"
_RECONCILE_LOCK = threading.Lock()
_RECONCILE_LAST: dict[str, float] = {}
_RECONCILE_INTERVAL_SECONDS = 60.0


def _db_path(db_path: str | Path | None = None) -> str:
    return str(db_path or config.DB_PATH)


@contextmanager
def _connect(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(_db_path(db_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=20000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _event_type_clause() -> tuple[str, list[str]]:
    values = sorted(UNREAD_EVENT_TYPES)
    return ",".join("?" for _ in values), values


def _admin_ids() -> list[int]:
    return sorted({int(value) for value in getattr(config, "ADMIN_IDS", []) if str(value).lstrip("-").isdigit()})


def _install_unread_trigger(connection: sqlite3.Connection) -> None:
    """Keep unread state atomic even when a caller inserts journal rows directly.

    Earlier releases updated ``user_message_state`` only from Python after the
    event INSERT. A database trigger makes the journal row and the red badge one
    indivisible SQLite transaction and also covers future/import code paths that
    append directly to ``user_events``.
    """
    event_types = ",".join("'" + value.replace("'", "''") + "'" for value in sorted(UNREAD_EVENT_TYPES))
    admins = _admin_ids()
    admin_clause = ""
    if admins:
        admin_clause = " AND NEW.tg_id NOT IN (" + ",".join(str(value) for value in admins) + ")"
    # Remove every trigger name used by prerelease/final builds so reinstalling
    # the same database cannot leave two counters incrementing in parallel.
    for trigger_name in (
        "trg_user_events_unread_insert",
        "trg_user_events_unread_insert_v267",
        _UNREAD_TRIGGER,
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    connection.executescript(
        f"""
        CREATE TRIGGER {_UNREAD_TRIGGER}
        AFTER INSERT ON user_events
        WHEN NEW.direction='in'
          AND NEW.event_type IN ({event_types})
          {admin_clause}
        BEGIN
            INSERT INTO user_message_state(
                tg_id,last_read_event_id,last_incoming_event_id,unread_count,
                last_message_at,last_message_text
            ) VALUES(
                NEW.tg_id,0,NEW.id,1,NEW.created_at,SUBSTR(COALESCE(NEW.text,''),1,{MAX_PREVIEW})
            )
            ON CONFLICT(tg_id) DO UPDATE SET
                last_incoming_event_id=MAX(user_message_state.last_incoming_event_id,NEW.id),
                unread_count=CASE
                    WHEN NEW.id>user_message_state.last_read_event_id
                    THEN MAX(0,user_message_state.unread_count)+1
                    ELSE MAX(0,user_message_state.unread_count)
                END,
                last_message_at=NEW.created_at,
                last_message_text=SUBSTR(COALESCE(NEW.text,''),1,{MAX_PREVIEW});
        END;
        """
    )


def _baseline_message_state(connection: sqlite3.Connection) -> None:
    """Mark history that predates unread tracking as already read."""
    placeholders, event_types = _event_type_clause()
    connection.execute(
        f"""
        INSERT OR IGNORE INTO user_message_state(
            tg_id,last_read_event_id,last_incoming_event_id,unread_count,
            last_message_at,last_message_text
        )
        SELECT
            events.tg_id,
            MAX(events.id),
            MAX(events.id),
            0,
            MAX(events.created_at),
            COALESCE((
                SELECT latest.text
                FROM user_events AS latest
                WHERE latest.tg_id=events.tg_id
                  AND latest.direction='in'
                  AND latest.event_type IN ({placeholders})
                ORDER BY latest.id DESC
                LIMIT 1
            ), '')
        FROM user_events AS events
        WHERE events.direction='in' AND events.event_type IN ({placeholders})
        GROUP BY events.tg_id
        """,
        [*event_types, *event_types],
    )


def ensure_schema(db_path: str | Path | None = None) -> None:
    key = str(Path(_db_path(db_path)).resolve())
    if key in _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        with _connect(db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state_existed = _table_exists(connection, "user_message_state")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    tg_id INTEGER NOT NULL,
                    username TEXT,
                    direction TEXT NOT NULL DEFAULT 'system',
                    event_type TEXT NOT NULL DEFAULT 'event',
                    text TEXT,
                    actor TEXT,
                    success INTEGER DEFAULT 1,
                    metadata TEXT
                );
                CREATE TABLE IF NOT EXISTS user_message_state (
                    tg_id INTEGER PRIMARY KEY,
                    last_read_event_id INTEGER NOT NULL DEFAULT 0,
                    last_incoming_event_id INTEGER NOT NULL DEFAULT 0,
                    unread_count INTEGER NOT NULL DEFAULT 0,
                    last_message_at TEXT,
                    last_message_text TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_user_events_tg_id_id
                    ON user_events(tg_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_user_events_created
                    ON user_events(created_at DESC);
                """
            )
            # Older installations can already have these tables with a reduced
            # schema. Add the columns before any index/trigger references to
            # event_type or the extended message-state fields.
            event_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(user_events)")}
            legacy_event_columns = {
                "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
                "tg_id": "INTEGER",
                "username": "TEXT",
                "direction": "TEXT NOT NULL DEFAULT 'system'",
                "event_type": "TEXT NOT NULL DEFAULT 'event'",
                "text": "TEXT",
                "actor": "TEXT",
                "success": "INTEGER DEFAULT 1",
                "metadata": "TEXT",
            }
            for name, definition in legacy_event_columns.items():
                if name not in event_columns:
                    connection.execute(f"ALTER TABLE user_events ADD COLUMN {name} {definition}")
            state_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(user_message_state)")}
            legacy_state_columns = {
                "last_read_event_id": "INTEGER NOT NULL DEFAULT 0",
                "last_incoming_event_id": "INTEGER NOT NULL DEFAULT 0",
                "unread_count": "INTEGER NOT NULL DEFAULT 0",
                "last_message_at": "TEXT",
                "last_message_text": "TEXT",
            }
            for name, definition in legacy_state_columns.items():
                if name not in state_columns:
                    connection.execute(f"ALTER TABLE user_message_state ADD COLUMN {name} {definition}")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_user_events_message_lookup ON user_events(tg_id, direction, event_type, id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_user_events_incoming_id ON user_events(direction,event_type,id,tg_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_user_message_state_unread ON user_message_state(unread_count, last_incoming_event_id DESC)")
            if not state_existed:
                _baseline_message_state(connection)
            _install_unread_trigger(connection)
        _SCHEMA_READY.add(key)


def _compact_metadata(metadata: Any) -> str:
    if metadata in (None, "", {}, []):
        return ""
    if isinstance(metadata, str):
        value = metadata
    else:
        try:
            value = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            value = str(metadata)
    return value[:MAX_METADATA]


def _decode_metadata(value: Any) -> dict[str, Any]:
    raw = str(value or "")
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"raw": raw}
    return loaded if isinstance(loaded, dict) else {"value": loaded}


def _event_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    event = dict(row)
    event["metadata"] = _decode_metadata(event.get("metadata"))
    event["success"] = bool(event.get("success"))
    return event


def plain_text(value: Any) -> str:
    """Return a compact readable representation of HTML/plain Telegram text."""
    text = str(value or "")
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = html.unescape(text)
    import re

    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()[:MAX_TEXT]


def _is_unread(direction: str, event_type: str, tg_id: int) -> bool:
    if direction != "in" or event_type not in UNREAD_EVENT_TYPES:
        return False
    return int(tg_id) not in set(_admin_ids())


def record_event(
    tg_id: int,
    *,
    username: str | None = None,
    direction: str = "system",
    event_type: str = "event",
    text: Any = "",
    actor: str = "system",
    success: bool = True,
    metadata: Any = None,
    db_path: str | Path | None = None,
    created_at: str | None = None,
) -> int:
    """Append one event and atomically update unread state when applicable."""
    tg_id = int(tg_id)
    if tg_id == 0:
        raise ValueError("tg_id must not be zero")
    direction = direction if direction in {"in", "out", "system"} else "system"
    event_type = str(event_type or "event")[:80]
    username_value = str(username or "").strip().lstrip("@")[:100]
    actor_value = str(actor or "system")[:120]
    timestamp = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text_value = plain_text(text)
    ensure_schema(db_path)
    started = time.monotonic()
    with _connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO user_events(
                created_at,tg_id,username,direction,event_type,text,actor,success,metadata
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                timestamp,
                tg_id,
                username_value,
                direction,
                event_type,
                text_value,
                actor_value,
                int(bool(success)),
                _compact_metadata(metadata),
            ),
        )
        event_id = int(cursor.lastrowid)
        if _is_unread(direction, event_type, tg_id):
            # The trigger is the normal atomic path. Keep a defensive Python
            # repair for databases whose trigger was removed by an external
            # restore/import while the long-running bot process stayed alive.
            state = connection.execute(
                "SELECT last_read_event_id,last_incoming_event_id FROM user_message_state WHERE tg_id=?",
                (tg_id,),
            ).fetchone()
            if not state or int(state["last_incoming_event_id"] or 0) != event_id:
                placeholders, event_types = _event_type_clause()
                last_read = max(0, int(state["last_read_event_id"] or 0)) if state else 0
                unread_count = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*) FROM user_events
                        WHERE tg_id=? AND direction='in'
                          AND event_type IN ({placeholders}) AND id>?
                        """,
                        [tg_id, *event_types, last_read],
                    ).fetchone()[0]
                    or 0
                )
                connection.execute(
                    """
                    INSERT INTO user_message_state(
                        tg_id,last_read_event_id,last_incoming_event_id,unread_count,
                        last_message_at,last_message_text
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(tg_id) DO UPDATE SET
                        last_read_event_id=excluded.last_read_event_id,
                        last_incoming_event_id=excluded.last_incoming_event_id,
                        unread_count=excluded.unread_count,
                        last_message_at=excluded.last_message_at,
                        last_message_text=excluded.last_message_text
                    """,
                    (tg_id, last_read, event_id, unread_count, timestamp, text_value[:MAX_PREVIEW]),
                )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if elapsed_ms >= 1000:
            logger.warning("performance operation=user_events_record tg_id=%s event_type=%s duration_ms=%s", tg_id, event_type, elapsed_ms)
        return event_id


def safe_record_event(*args: Any, **kwargs: Any) -> int | None:
    try:
        return record_event(*args, **kwargs)
    except Exception as error:
        logger.exception("Не удалось записать событие пользователя: %s", error)
        return None


def _reconcile_unread_state_once(db_path: str | Path | None = None) -> int:
    """Repair compact counters from the append-only journal with bounded work.

    The trigger is the normal fast path. This reconciliation is deliberately
    small and exists for databases upgraded from builds where a message row could
    be present while the compact badge row was missing or stale.
    """
    placeholders, event_types = _event_type_clause()
    admins = _admin_ids()
    admin_sql = ""
    admin_params: list[Any] = []
    if admins:
        admin_sql = " AND events.tg_id NOT IN (" + ",".join("?" for _ in admins) + ")"
        admin_params.extend(admins)
    repaired = 0
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        candidates = connection.execute(
            f"""
            SELECT events.tg_id,MAX(events.id) AS latest_id
            FROM user_events AS events
            LEFT JOIN user_message_state AS state ON state.tg_id=events.tg_id
            WHERE events.direction='in'
              AND events.event_type IN ({placeholders})
              AND events.id>COALESCE(state.last_incoming_event_id,0)
              {admin_sql}
            GROUP BY events.tg_id
            ORDER BY latest_id DESC
            LIMIT 1000
            """,
            [*event_types, *admin_params],
        ).fetchall()
        suspicious = connection.execute(
            """
            SELECT tg_id,last_incoming_event_id
            FROM user_message_state
            WHERE unread_count=0 AND last_incoming_event_id>last_read_event_id
            LIMIT 1000
            """
        ).fetchall()
        candidate_ids = {int(row["tg_id"]) for row in candidates}
        candidate_ids.update(int(row["tg_id"]) for row in suspicious)
        candidate_ids.difference_update(admins)
        for tg_id in candidate_ids:
            state = connection.execute(
                "SELECT last_read_event_id FROM user_message_state WHERE tg_id=?",
                (tg_id,),
            ).fetchone()
            last_read = max(0, int(state["last_read_event_id"] or 0)) if state else 0
            latest = connection.execute(
                f"""
                SELECT id,created_at,text
                FROM user_events
                WHERE tg_id=? AND direction='in' AND event_type IN ({placeholders})
                ORDER BY id DESC LIMIT 1
                """,
                [tg_id, *event_types],
            ).fetchone()
            if not latest:
                continue
            latest_id = int(latest["id"] or 0)
            unread_count = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM user_events
                    WHERE tg_id=? AND direction='in' AND event_type IN ({placeholders}) AND id>?
                    """,
                    [tg_id, *event_types, last_read],
                ).fetchone()[0]
                or 0
            )
            connection.execute(
                """
                INSERT INTO user_message_state(
                    tg_id,last_read_event_id,last_incoming_event_id,unread_count,
                    last_message_at,last_message_text
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    last_incoming_event_id=excluded.last_incoming_event_id,
                    unread_count=excluded.unread_count,
                    last_message_at=excluded.last_message_at,
                    last_message_text=excluded.last_message_text
                """,
                (
                    tg_id,
                    last_read,
                    latest_id,
                    unread_count,
                    str(latest["created_at"] or ""),
                    str(latest["text"] or "")[:MAX_PREVIEW],
                ),
            )
            repaired += 1
    return repaired


def reconcile_unread_state(db_path: str | Path | None = None) -> int:
    ensure_schema(db_path)
    key = str(Path(_db_path(db_path)).resolve())
    now = time.monotonic()
    with _RECONCILE_LOCK:
        if now - _RECONCILE_LAST.get(key, 0.0) < _RECONCILE_INTERVAL_SECONDS:
            return 0
        _RECONCILE_LAST[key] = now
    try:
        return int(_with_busy_retry(lambda: _reconcile_unread_state_once(db_path)))
    except Exception:
        # Let the next request retry sooner after a transient busy/error state.
        with _RECONCILE_LOCK:
            _RECONCILE_LAST.pop(key, None)
        raise



def last_known_username(tg_id: int, *, db_path: str | Path | None = None) -> str:
    ensure_schema(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT username FROM user_events WHERE tg_id=? AND TRIM(COALESCE(username,''))<>'' ORDER BY id DESC LIMIT 1",
            (int(tg_id),),
        ).fetchone()
    return str(row[0] or "") if row else ""


def conversation_users(*, limit: int = 2000, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return every Telegram chat found in the journal, including users without subscriptions."""
    ensure_schema(db_path)
    limit = max(1, min(int(limit), 10000))
    with _connect(db_path) as connection:
        rows = connection.execute(
            """SELECT events.tg_id,
                COALESCE(NULLIF(users.username,''), NULLIF(events.username,''), 'id_' || events.tg_id) AS username,
                COUNT(*) AS total_events,
                SUM(CASE WHEN events.direction='in' THEN 1 ELSE 0 END) AS incoming_events,
                SUM(CASE WHEN events.direction='out' THEN 1 ELSE 0 END) AS outgoing_events,
                MAX(events.id) AS last_event_id, MAX(events.created_at) AS last_event_at
            FROM user_events AS events LEFT JOIN users ON users.tg_id=events.tg_id
            WHERE events.tg_id>0 GROUP BY events.tg_id ORDER BY last_event_id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(tg_id=int(r['tg_id']), username=str(r['username'] or f"id_{int(r['tg_id'])}"),
        total_events=int(r['total_events'] or 0), incoming_events=int(r['incoming_events'] or 0),
        outgoing_events=int(r['outgoing_events'] or 0), last_event_id=int(r['last_event_id'] or 0),
        last_event_at=str(r['last_event_at'] or '')) for r in rows]


def all_events(*, limit: int = 1000, tg_id: int | None = None, direction: str = 'all', before_id: int = 0, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return the global journal for the dedicated Messages page."""
    ensure_schema(db_path)
    limit=max(1,min(int(limit),5000))
    query=("SELECT events.*, COALESCE(NULLIF(users.username,''), NULLIF(events.username,''), 'id_' || events.tg_id) AS display_username "
           "FROM user_events AS events LEFT JOIN users ON users.tg_id=events.tg_id WHERE events.tg_id>0")
    params=[]
    if tg_id is not None and int(tg_id)>0:
        query+=' AND events.tg_id=?'; params.append(int(tg_id))
    if direction in {'in','out','system'}:
        query+=' AND events.direction=?'; params.append(direction)
    if int(before_id)>0:
        query+=' AND events.id<?'; params.append(int(before_id))
    query+=' ORDER BY events.id DESC LIMIT ?'; params.append(limit)
    with _connect(db_path) as connection:
        rows=connection.execute(query,params).fetchall()
    result=[]
    for row in rows:
        item=_event_from_row(row); item['display_username']=str(row['display_username'] or f"id_{int(row['tg_id'])}"); result.append(item)
    return result

def recent_events(
    tg_id: int,
    *,
    limit: int = 200,
    after_id: int = 0,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(db_path)
    limit = max(1, min(int(limit), 1000))
    query = "SELECT * FROM user_events WHERE tg_id=?"
    params: list[Any] = [int(tg_id)]
    if int(after_id) > 0:
        query += " AND id>?"
        params.append(int(after_id))
        query += " ORDER BY id ASC LIMIT ?"
    else:
        query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect(db_path) as connection:
        rows = [_event_from_row(row) for row in connection.execute(query, params).fetchall()]
    if int(after_id) <= 0:
        rows.reverse()
    return rows


def get_event(
    tg_id: int,
    event_id: int,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    ensure_schema(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM user_events WHERE tg_id=? AND id=?",
            (int(tg_id), int(event_id)),
        ).fetchone()
    return _event_from_row(row) if row else None


def unread_messages_summary(
    *,
    limit: int = 2_000,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the compact list used by the panel's notification poller."""
    ensure_schema(db_path)
    try:
        reconcile_unread_state(db_path)
    except Exception as error:
        # A busy database must not turn the whole notification API into HTTP 500.
        logger.warning("Не удалось сверить счётчики непрочитанных: %s", error)
    limit = max(1, min(int(limit), 10_000))
    with _connect(db_path) as connection:
        users_exists = _table_exists(connection, "users")
        join = "LEFT JOIN users ON users.tg_id=state.tg_id" if users_exists else ""
        username = (
            "COALESCE(NULLIF(users.username,''),"
            "(SELECT NULLIF(latest.username,'') FROM user_events AS latest "
            "WHERE latest.tg_id=state.tg_id ORDER BY latest.id DESC LIMIT 1),'')"
            if users_exists
            else "COALESCE((SELECT NULLIF(latest.username,'') FROM user_events AS latest "
            "WHERE latest.tg_id=state.tg_id ORDER BY latest.id DESC LIMIT 1),'')"
        )
        known_user = "CASE WHEN users.tg_id IS NULL THEN 0 ELSE 1 END" if users_exists else "0"
        admins = _admin_ids()
        admin_sql = ""
        admin_params: list[Any] = []
        if admins:
            admin_sql = " AND state.tg_id NOT IN (" + ",".join("?" for _ in admins) + ")"
            admin_params.extend(admins)
        totals = connection.execute(
            f"""
            SELECT COALESCE(SUM(state.unread_count),0), COUNT(*)
            FROM user_message_state AS state
            {join}
            WHERE state.unread_count>0 {admin_sql}
            """,
            admin_params,
        ).fetchone()
        rows = connection.execute(
            f"""
            SELECT
                state.tg_id,
                state.unread_count,
                state.last_incoming_event_id,
                state.last_message_at,
                state.last_message_text,
                {username} AS username,
                {known_user} AS known_user
            FROM user_message_state AS state
            {join}
            WHERE state.unread_count>0 {admin_sql}
            ORDER BY state.last_incoming_event_id DESC
            LIMIT ?
            """,
            [*admin_params, limit],
        ).fetchall()
    items = [
        {
            "tg_id": int(row["tg_id"]),
            "count": max(0, int(row["unread_count"] or 0)),
            "last_event_id": max(0, int(row["last_incoming_event_id"] or 0)),
            "last_message_at": str(row["last_message_at"] or ""),
            "preview": str(row["last_message_text"] or "")[:MAX_PREVIEW],
            "username": str(row["username"] or ""),
            "known_user": bool(row["known_user"]),
        }
        for row in rows
    ]
    return {
        "total": max(0, int(totals[0] or 0)),
        "users": max(0, int(totals[1] or 0)),
        "items": items,
        "last_event_id": max((int(item["last_event_id"]) for item in items), default=0),
    }


def unread_states(
    tg_ids: Iterable[int] | None = None,
    *,
    db_path: str | Path | None = None,
) -> dict[int, dict[str, Any]]:
    """Compatibility view for user-list rendering without scanning history."""
    ensure_schema(db_path)
    params: list[Any] = []
    query = "SELECT * FROM user_message_state WHERE unread_count>0"
    if tg_ids is not None:
        values = sorted({int(value) for value in tg_ids})
        if not values:
            return {}
        placeholders = ",".join("?" for _ in values)
        query += f" AND tg_id IN ({placeholders})"
        params.extend(values)
    query += " ORDER BY last_incoming_event_id DESC"
    with _connect(db_path) as connection:
        rows = connection.execute(query, params).fetchall()
    return {
        int(row["tg_id"]): {
            "tg_id": int(row["tg_id"]),
            "unread_count": max(0, int(row["unread_count"] or 0)),
            "last_incoming_event_id": max(0, int(row["last_incoming_event_id"] or 0)),
            "last_incoming_at": str(row["last_message_at"] or ""),
            "preview": str(row["last_message_text"] or "")[:MAX_PREVIEW],
        }
        for row in rows
    }


def unread_summary(*, db_path: str | Path | None = None) -> dict[str, int]:
    snapshot = unread_messages_summary(db_path=db_path)
    return {"messages": int(snapshot["total"]), "users": int(snapshot["users"])}


def _mark_messages_read_once(
    tg_id: int,
    *,
    through_event_id: int | None,
    clear_current: bool,
    db_path: str | Path | None,
) -> dict[str, int]:
    """Update the compact unread cursor inside one immediate transaction."""
    placeholders, event_types = _event_type_clause()
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        actual_latest = int(
            connection.execute(
                f"""
                SELECT COALESCE(MAX(id),0) FROM user_events
                WHERE tg_id=? AND direction='in' AND event_type IN ({placeholders})
                """,
                [tg_id, *event_types],
            ).fetchone()[0]
            or 0
        )
        row = connection.execute(
            """
            SELECT last_read_event_id,last_incoming_event_id,last_message_at,last_message_text
            FROM user_message_state WHERE tg_id=?
            """,
            (tg_id,),
        ).fetchone()
        previous_read = max(0, int(row["last_read_event_id"] or 0)) if row else 0
        stored_last = max(0, int(row["last_incoming_event_id"] or 0)) if row else 0
        last_incoming = max(stored_last, actual_latest)

        if clear_current:
            # Opening a conversation is an explicit read action. The old compact
            # row can be inconsistent after imports or earlier releases, so the
            # actual journal boundary replaces (rather than merely extends) stale
            # cursor values and the counter is reset unconditionally.
            target = actual_latest
            last_incoming = actual_latest
            remaining = 0
        else:
            if through_event_id is None:
                target = last_incoming
            else:
                target = min(last_incoming, max(previous_read, int(through_event_id)))
            target = max(previous_read, target)
            remaining = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM user_events
                    WHERE tg_id=? AND direction='in'
                      AND event_type IN ({placeholders}) AND id>?
                    """,
                    [tg_id, *event_types, target],
                ).fetchone()[0]
                or 0
            )

        last_message_at = str(row["last_message_at"] or "") if row else ""
        last_message_text = str(row["last_message_text"] or "")[:MAX_PREVIEW] if row else ""
        connection.execute(
            """
            INSERT INTO user_message_state(
                tg_id,last_read_event_id,last_incoming_event_id,unread_count,
                last_message_at,last_message_text
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(tg_id) DO UPDATE SET
                last_read_event_id=excluded.last_read_event_id,
                last_incoming_event_id=excluded.last_incoming_event_id,
                unread_count=excluded.unread_count,
                last_message_at=COALESCE(NULLIF(user_message_state.last_message_at,''), excluded.last_message_at),
                last_message_text=COALESCE(NULLIF(user_message_state.last_message_text,''), excluded.last_message_text)
            """,
            (
                tg_id,
                target,
                last_incoming,
                remaining,
                last_message_at,
                last_message_text,
            ),
        )
        return {
            "last_read_event_id": target,
            "last_incoming_event_id": last_incoming,
            "remaining": remaining,
        }


def _with_busy_retry(operation):
    for attempt in range(5):
        try:
            return operation()
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if attempt >= 4 or not any(token in message for token in ("locked", "busy")):
                raise
            time.sleep(0.04 * (2 ** attempt))
    raise RuntimeError("Не удалось обновить состояние прочтения")


def mark_messages_read(
    tg_id: int,
    *,
    through_event_id: int | None = None,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """Acknowledge incoming messages through an exact rendered event boundary."""
    tg_id = int(tg_id)
    ensure_schema(db_path)
    return _with_busy_retry(
        lambda: _mark_messages_read_once(
            tg_id,
            through_event_id=through_event_id,
            clear_current=False,
            db_path=db_path,
        )
    )


def mark_conversation_opened(
    tg_id: int,
    *,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """Clear every unread item that exists when the administrator opens the chat."""
    tg_id = int(tg_id)
    ensure_schema(db_path)
    return _with_busy_retry(
        lambda: _mark_messages_read_once(
            tg_id,
            through_event_id=None,
            clear_current=True,
            db_path=db_path,
        )
    )


def mark_read(
    tg_id: int,
    *,
    up_to_event_id: int | None = None,
    db_path: str | Path | None = None,
) -> int:
    return int(
        mark_messages_read(
            tg_id,
            through_event_id=up_to_event_id,
            db_path=db_path,
        )["remaining"]
    )


def rekey_message_state(
    connection: sqlite3.Connection,
    old_tg_id: int,
    new_tg_id: int,
) -> None:
    """Merge unread state after related history rows were re-keyed."""
    old_tg_id, new_tg_id = int(old_tg_id), int(new_tg_id)
    if old_tg_id == new_tg_id or not _table_exists(connection, "user_message_state"):
        return
    old_row = connection.execute(
        "SELECT * FROM user_message_state WHERE tg_id=?", (old_tg_id,)
    ).fetchone()
    new_row = connection.execute(
        "SELECT * FROM user_message_state WHERE tg_id=?", (new_tg_id,)
    ).fetchone()
    if not old_row and not new_row:
        return

    rows = [row for row in (old_row, new_row) if row]
    last_read = max(int(row["last_read_event_id"] or 0) for row in rows)
    last_incoming = max(int(row["last_incoming_event_id"] or 0) for row in rows)
    latest_row = max(rows, key=lambda item: int(item["last_incoming_event_id"] or 0))
    placeholders, event_types = _event_type_clause()
    remaining = int(
        connection.execute(
            f"""
            SELECT COUNT(*) FROM user_events
            WHERE tg_id=? AND direction='in' AND event_type IN ({placeholders}) AND id>?
            """,
            [new_tg_id, *event_types, last_read],
        ).fetchone()[0]
        or 0
    )
    connection.execute(
        """
        INSERT INTO user_message_state(
            tg_id,last_read_event_id,last_incoming_event_id,unread_count,
            last_message_at,last_message_text
        ) VALUES(?,?,?,?,?,?)
        ON CONFLICT(tg_id) DO UPDATE SET
            last_read_event_id=excluded.last_read_event_id,
            last_incoming_event_id=excluded.last_incoming_event_id,
            unread_count=excluded.unread_count,
            last_message_at=excluded.last_message_at,
            last_message_text=excluded.last_message_text
        """,
        (
            new_tg_id,
            last_read,
            last_incoming,
            remaining,
            str(latest_row["last_message_at"] or ""),
            str(latest_row["last_message_text"] or "")[:MAX_PREVIEW],
        ),
    )
    connection.execute("DELETE FROM user_message_state WHERE tg_id=?", (old_tg_id,))


def rekey_chat_state(connection: sqlite3.Connection, old_tg_id: int, new_tg_id: int) -> None:
    rekey_message_state(connection, old_tg_id, new_tg_id)


def prune_events(
    *,
    keep_days: int = 365,
    max_rows: int = 250_000,
    db_path: str | Path | None = None,
) -> int:
    """Bound history growth and reconcile the compact unread counters."""
    ensure_schema(db_path)
    deleted = 0
    placeholders, event_types = _event_type_clause()
    with _connect(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM user_events WHERE created_at < datetime('now', ?)",
            (f"-{max(1, int(keep_days))} days",),
        )
        deleted += max(0, int(cursor.rowcount or 0))
        count = int(connection.execute("SELECT COUNT(*) FROM user_events").fetchone()[0])
        overflow = max(0, count - max(1_000, int(max_rows)))
        if overflow:
            cursor = connection.execute(
                "DELETE FROM user_events WHERE id IN (SELECT id FROM user_events ORDER BY id ASC LIMIT ?)",
                (overflow,),
            )
            deleted += max(0, int(cursor.rowcount or 0))
        if deleted:
            connection.execute(
                f"""
                UPDATE user_message_state
                SET unread_count=(
                    SELECT COUNT(*) FROM user_events AS events
                    WHERE events.tg_id=user_message_state.tg_id
                      AND events.direction='in'
                      AND events.event_type IN ({placeholders})
                      AND events.id>user_message_state.last_read_event_id
                )
                """,
                event_types,
            )
    return deleted
