"""Database compatibility layer for FargoVPN PostgreSQL runtime.

All application data is stored in PostgreSQL. The wrapper keeps the existing
small DB API (execute/fetchone/fetchall/commit/rollback/context manager) while
normalising legacy SQLite-style '?' parameters used by the application.
The x-ui database is intentionally outside this layer; modern 3x-ui installations may use PostgreSQL, with SQLite retained only for legacy discovery.
"""
from __future__ import annotations

import os
import re
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, Row

_ENGINE: Engine | None = None
LOGGER = logging.getLogger("vpn-service-db")


def _slow_query_seconds() -> float:
    try:
        import config
        configured = getattr(config, "DB_SLOW_QUERY_SECONDS", 1.0)
        return max(0.001, float(os.getenv("FARGOVPN_DB_SLOW_QUERY_SECONDS", str(configured))))
    except (TypeError, ValueError):
        return 1.0


def _query_label(sql: str) -> str:
    # Never log bind values: payment metadata and authentication values may be
    # present there. Compact SQL text alone is enough to locate a slow query.
    return " ".join(str(sql).split())[:500]


def _log_if_slow(operation: str, started: float, sql: str = "", rows: int | None = None) -> None:
    elapsed = time.monotonic() - started
    if elapsed < _slow_query_seconds():
        return
    LOGGER.warning(
        "performance operation=%s duration_ms=%d rows=%s sql=%s",
        operation,
        int(elapsed * 1000),
        "unknown" if rows is None else rows,
        _query_label(sql) if sql else "",
    )

def database_url() -> str:
    import config
    # Explicit environment DSNs are authoritative for migrations/tests and
    # controlled one-shot operations. A stale config.py value must not win.
    url = str(
        os.getenv("FARGOVPN_DATABASE_URL", "") or
        os.getenv("DATABASE_URL", "") or
        getattr(config, "DATABASE_URL", "")
    ).strip()
    if not url:
        raise RuntimeError(
            "PostgreSQL is not configured. Set DATABASE_URL "
            "(for example postgresql+psycopg://fargovpn:password@127.0.0.1:5432/fargovpn)."
        )
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if "+psycopg2" in url:
        url = url.replace("+psycopg2", "+psycopg")
    if not url.startswith("postgresql+"):
        raise RuntimeError("DATABASE_URL must use a PostgreSQL SQLAlchemy URL")
    return url

def engine() -> Engine:
    global _ENGINE
    if _ENGINE is None:
        import config
        configured_pool = int(getattr(config, "DATABASE_POOL_SIZE", 4))
        configured_overflow = int(getattr(config, "DATABASE_MAX_OVERFLOW", 4))
        configured_timeout = int(getattr(config, "DATABASE_POOL_TIMEOUT", 15))
        _ENGINE = create_engine(
            database_url(),
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=max(1, int(os.getenv("FARGOVPN_DB_POOL_SIZE", str(configured_pool)))),
            max_overflow=max(0, int(os.getenv("FARGOVPN_DB_MAX_OVERFLOW", str(configured_overflow)))),
            pool_timeout=max(1, int(os.getenv("FARGOVPN_DB_POOL_TIMEOUT", str(configured_timeout)))),
            future=True,
        )
        from sqlalchemy import event
        @event.listens_for(_ENGINE, "connect")
        def _set_public_schema(dbapi_connection, connection_record):
            # Some PostgreSQL installations/roles may have an empty search_path.
            # FargoVPN uses the public schema for application tables.
            cur = dbapi_connection.cursor()
            try:
                cur.execute("CREATE SCHEMA IF NOT EXISTS public")
                cur.execute("SET search_path TO public")
            finally:
                cur.close()
    return _ENGINE

def dispose() -> None:
    global _ENGINE
    if _ENGINE is not None:
        _ENGINE.dispose()
        _ENGINE = None

def _replace_qmarks(sql: str, params: Sequence[Any] | None) -> tuple[str, dict[str, Any]]:
    if not params:
        return sql, {}
    out: list[str] = []
    names: dict[str, Any] = {}
    idx = 0
    in_single = in_double = False
    escaped = False
    for ch in str(sql):
        if ch == "'" and not in_double and not escaped:
            in_single = not in_single
        elif ch == '"' and not in_single and not escaped:
            in_double = not in_double
        if ch == "?" and not in_single and not in_double:
            name = f"p{idx}"
            out.append(":" + name)
            names[name] = params[idx] if idx < len(params) else None
            idx += 1
        else:
            out.append(ch)
        escaped = (ch == "\\") and not escaped
        if ch != "\\":
            escaped = False
    if idx != len(params):
        # Useful guard for accidental driver mismatch.
        raise ValueError(f"SQL parameter count mismatch: expected {idx}, got {len(params)}")
    return "".join(out), names

def _normalize_sql(sql: str) -> str:
    s = str(sql).strip()
    if re.match(r"^PRAGMA\s+(busy_timeout|journal_mode|foreign_keys)\b", s, re.I):
        return ""
    # SQLite idioms retained in legacy code.
    s = re.sub(r"^\s*BEGIN(?:\s+(?:IMMEDIATE|EXCLUSIVE))?\s*$", "", s, flags=re.I)
    s = re.sub(r"^\s*INSERT\s+OR\s+IGNORE\s+", "INSERT ", s, count=1, flags=re.I)
    if re.search(r"\bINSERT\s+", s, re.I) and "ON CONFLICT" not in s.upper():
        # Only safe for statements historically using INSERT OR IGNORE. The
        # original marker is intentionally preserved before this function is
        # called by _rewrite_ignore.
        pass
    # SQLite datetime('now', ?) -> PostgreSQL text comparison.
    # created_at is intentionally stored as legacy ISO text, so compare it to
    # the same YYYY-MM-DD HH:MM:SS representation instead of mixing text with timestamptz.
    s = re.sub(
        r"datetime\(\s*'now'\s*,\s*\?\s*\)",
        "TO_CHAR(CURRENT_TIMESTAMP + (? || ' seconds')::interval, 'YYYY-MM-DD HH24:MI:SS')",
        s,
        flags=re.I,
    )
    # SQLite scalar MAX(a,b) is PostgreSQL GREATEST(a,b); aggregate MAX(col)
    # remains untouched because there is no comma inside its argument list.
    s = re.sub(r"MAX\((COALESCE\([^()]+\),COALESCE\([^()]+\))\)", r"GREATEST(\1)", s, flags=re.I)
    # SQLite epoch expression used by diagnostics.
    s = re.sub(
        r"CAST\(strftime\(\s*'%s'\s*,\s*'now'\s*\)\s+AS\s+INTEGER\)",
        "CAST(EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) AS BIGINT)",
        s,
        flags=re.I,
    )
    return s

class CompatResult:
    def __init__(self, result: Any, prefetched: Any = None):
        self._result = result
        self._prefetched = prefetched
        self._lastrowid = None
        if prefetched is not None:
            try: self._lastrowid = prefetched[0]
            except Exception: pass
    def fetchone(self):
        if self._prefetched is not None:
            row, self._prefetched = self._prefetched, None
        else:
            row = self._result.fetchone()
        return CompatRow(row) if row is not None else None
    def fetchall(self):
        rows = []
        if self._prefetched is not None:
            rows.append(self._prefetched)
            self._prefetched = None
        rows.extend(self._result.fetchall())
        return [CompatRow(r) for r in rows]
    def fetchmany(self, size=100):
        rows = []
        if size > 0 and self._prefetched is not None:
            rows.append(self._prefetched)
            self._prefetched = None
        if size > len(rows):
            rows.extend(self._result.fetchmany(size - len(rows)))
        return [CompatRow(r) for r in rows]
    def __iter__(self):
        if self._prefetched is not None:
            row, self._prefetched = self._prefetched, None
            yield CompatRow(row)
        for r in self._result:
            yield CompatRow(r)
    @property
    def rowcount(self):
        return getattr(self._result, "rowcount", -1)
    @property
    def lastrowid(self):
        return self._lastrowid

class CompatRow:
    def __init__(self, row: Any):
        self._row = row
    def __getitem__(self, key: Any):
        if isinstance(key, int):
            return self._row[key]
        try:
            return self._row._mapping[key]
        except Exception:
            return dict(self._row._mapping).get(key)
    def __iter__(self):
        return iter(tuple(self._row))
    def keys(self):
        return self._row._mapping.keys()
    def __len__(self):
        return len(self._row)
    def __repr__(self):
        return repr(self._row)

def _metadata_emulation(sql: str, params: Sequence[Any] | None) -> CompatResult | None:
    """Handle the small SQLite metadata surface retained by legacy modules."""
    import re as _re
    raw = str(sql).strip()

    typ_m = _re.search(r"type\s*=\s*'([^']+)'", raw, _re.I)
    if "sqlite_master" in raw.lower() and typ_m:
        typ = typ_m.group(1).lower()
        wanted = None
        if params:
            wanted = str(params[0]) if len(params) else None
        with engine().connect() as c:
            if typ == "table":
                rows = c.execute(text("""
                    SELECT tablename AS name
                    FROM pg_catalog.pg_tables
                    WHERE schemaname='public' AND tablename NOT LIKE 'pg_%'
                    ORDER BY tablename
                """)).fetchall()
            elif typ == "trigger":
                rows = c.execute(text("""
                    SELECT tg.tgname AS name
                    FROM pg_catalog.pg_trigger tg
                    JOIN pg_catalog.pg_class cl ON cl.oid=tg.tgrelid
                    JOIN pg_catalog.pg_namespace ns ON ns.oid=cl.relnamespace
                    WHERE ns.nspname='public' AND NOT tg.tgisinternal
                    ORDER BY tg.tgname
                """)).fetchall()
            else:
                return None
        if wanted is not None:
            rows=[r for r in rows if str(r[0])==wanted]
        return _SimpleResult(rows)
    m = _re.match(r"PRAGMA\s+table_info\((.+)\)", raw, _re.I)
    if m:
        table = m.group(1).strip().strip('"').replace('""','"')
        with engine().connect() as c:
            rows = c.execute(text("""
                SELECT row_number() OVER (ORDER BY a.attnum)-1 AS cid,
                       a.attname AS name,
                       pg_catalog.format_type(a.atttypid,a.atttypmod) AS type,
                       CASE WHEN a.attnotnull THEN 1 ELSE 0 END AS notnull,
                       pg_get_expr(d.adbin, d.adrelid) AS dflt_value,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM pg_catalog.pg_constraint pc
                           WHERE pc.conrelid=a.attrelid AND pc.contype='p'
                             AND a.attnum = ANY(pc.conkey)
                       ) THEN 1 ELSE 0 END AS pk
                FROM pg_catalog.pg_attribute a
                JOIN pg_catalog.pg_class cl ON cl.oid=a.attrelid
                JOIN pg_catalog.pg_namespace ns ON ns.oid=cl.relnamespace
                LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
                WHERE ns.nspname='public' AND cl.relname=:table AND a.attnum>0 AND NOT a.attisdropped
                ORDER BY a.attnum
            """), {"table": table}).fetchall()
        return _SimpleResult(rows)
    if _re.match(r"PRAGMA\s+(quick_check|integrity_check)", raw, _re.I):
        return _SimpleResult([("ok",)])
    if _re.match(r"PRAGMA\s+journal_mode", raw, _re.I):
        return _SimpleResult([("wal",)])
    return None

class _SimpleResult:
    rowcount = 0
    def __init__(self, rows): self.rows=list(rows)
    def fetchone(self): return self.rows.pop(0) if self.rows else None
    def fetchall(self): rows=self.rows; self.rows=[]; return rows
    def fetchmany(self, size=100): out=self.rows[:size]; self.rows=self.rows[size:]; return out
    def __iter__(self): return iter(self.rows)

class ConnectionWrapper:
    def __init__(self, conn: Connection):
        self._conn = conn
        self.row_factory = None
    def execute(self, sql: str, params: Sequence[Any] | None = None):
        started = time.monotonic()
        raw = str(sql)
        emu = _metadata_emulation(raw, params)
        if emu is not None:
            return emu
        normalized = _normalize_sql(raw)
        if not normalized:
            class _Noop:
                rowcount = 0
                def fetchone(self): return None
                def fetchall(self): return []
            return CompatResult(_Noop())
        # Preserve semantics of historical INSERT OR IGNORE.
        ignore = bool(re.match(r"^\s*INSERT\s+OR\s+IGNORE\b", raw, re.I))
        normalized = re.sub(r"^\s*INSERT\s+OR\s+IGNORE\b", "INSERT", normalized, count=1, flags=re.I)
        normalized, bind = _replace_qmarks(normalized, params)
        if ignore and "ON CONFLICT" not in normalized.upper():
            normalized = normalized.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        # Legacy SQLite callers use cursor.lastrowid after INSERTs. PostgreSQL
        # does not expose it, so request the generated id explicitly for tables
        # whose primary key is named `id`. This must not depend on whether `id`
        # was explicitly present in the INSERT column list (normally it is not).
        wants_last_id = False
        prefetched = None
        if "RETURNING" not in normalized.upper():
            candidate = re.match(
                r"^\s*INSERT\s+INTO\s+[\"]?([A-Za-z_][\w]*)[\"]?",
                normalized,
                re.I,
            )
            if candidate and candidate.group(1).lower() in {
                "payments", "user_events", "audit_log", "backup_runs", "metrics",
                "message_log", "identity_import_runs", "panel_push_logs",
                "panel_push_subscriptions", "push_subscriptions",
                "referral_rewards", "manual_keys", "web_push_subscriptions",
            }:
                wants_last_id = True
                normalized = normalized.rstrip().rstrip(";") + " RETURNING id"
        try:
            result = self._conn.execute(text(normalized), bind)
        except Exception:
            _log_if_slow("db_query_error", started, normalized)
            raise
        if wants_last_id and "RETURNING" in normalized.upper():
            prefetched = result.fetchone()
        _log_if_slow("db_query", started, normalized, getattr(result, "rowcount", None))
        return CompatResult(result, prefetched=prefetched)
    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        started = time.monotonic()
        raw = str(sql)
        ignore = bool(re.match(r"^\s*INSERT\s+OR\s+IGNORE\b", raw, re.I))
        normalized = _normalize_sql(raw)
        normalized = re.sub(r"^\s*INSERT\s+OR\s+IGNORE\b", "INSERT", normalized, count=1, flags=re.I)
        params_list = [tuple(params) for params in seq_of_params]
        if not params_list:
            return _SimpleResult([])
        # Convert SQLite-style positional '?' placeholders to SQLAlchemy named
        # parameters once, then pass a homogeneous list of mappings for true
        # executemany semantics.  The previous implementation accidentally
        # executed the unconverted SQL containing '?' placeholders.
        converted: list[dict[str, Any]] = []
        converted_sql, first_bind = _replace_qmarks(normalized, params_list[0])
        converted.append(first_bind)
        for params in params_list[1:]:
            _, bind = _replace_qmarks(normalized, params)
            converted.append(bind)
        normalized = converted_sql
        if ignore and "ON CONFLICT" not in normalized.upper():
            normalized = normalized.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        try:
            result = self._conn.execute(text(normalized), converted)
        except Exception:
            _log_if_slow("db_executemany_error", started, normalized)
            raise
        _log_if_slow("db_executemany", started, normalized, getattr(result, "rowcount", None))
        return CompatResult(result)
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        self._conn.close()
    def cursor(self):
        return _CursorWrapper(self)
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()

class _CursorWrapper:
    def __init__(self, conn):
        self.conn=conn
        self._result=None
    def execute(self, sql, params=()):
        self._result=self.conn.execute(sql, params)
        return self
    def executemany(self, sql, seq):
        self._result=self.conn.executemany(sql, seq)
        return self
    def fetchone(self): return self._result.fetchone() if self._result else None
    def fetchall(self): return self._result.fetchall() if self._result else []
    @property
    def rowcount(self): return self._result.rowcount if self._result else -1
    @property
    def lastrowid(self): return self._result.lastrowid if self._result else None

def connect(db_path: str | os.PathLike | None = None, **_: Any) -> ConnectionWrapper:
    # db_path is accepted so callers can keep their existing signatures. The
    # application data backend is selected exclusively by DATABASE_URL.
    started = time.monotonic()
    try:
        connection = engine().connect()
    except Exception:
        _log_if_slow("db_connect_error", started)
        raise
    _log_if_slow("db_connect", started)
    return ConnectionWrapper(connection)
