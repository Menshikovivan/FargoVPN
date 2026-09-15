"""Persistent login rate limiting for the web control panel."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import config

# A separate per-IP bucket prevents attackers from bypassing the limit by
# changing the submitted username on every request. The leading NUL keeps this
# namespace distinct from any ordinary form value.
GLOBAL_USERNAME_KEY = "\x00__all_panel_users__"
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()


@dataclass(frozen=True)
class LoginState:
    allowed: bool
    failures: int = 0
    blocked_until: int = 0
    retry_after: int = 0
    blocks: int = 0


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


def ensure_schema(db_path: str | Path | None = None) -> None:
    key = str(Path(_db_path(db_path)).resolve())
    if key in _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        with _connect(db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS login_security (
                    identity TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL DEFAULT 0,
                    first_failed_at INTEGER NOT NULL DEFAULT 0,
                    last_failed_at INTEGER NOT NULL DEFAULT 0,
                    blocked_until INTEGER NOT NULL DEFAULT 0,
                    blocks INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_login_security_last_failed
                    ON login_security(last_failed_at);
                """
            )
        _SCHEMA_READY.add(key)


def _identity(ip_address: str, username: str) -> str:
    value = f"{str(ip_address or 'unknown').strip().lower()}\0{str(username or '').strip().lower()}"
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _settings() -> tuple[int, int, int, int]:
    attempts = max(2, int(getattr(config, "WEB_LOGIN_MAX_ATTEMPTS", 5)))
    window = max(30, int(getattr(config, "WEB_LOGIN_WINDOW_SECONDS", 900)))
    base_block = max(30, int(getattr(config, "WEB_LOGIN_BLOCK_SECONDS", 900)))
    max_block = max(base_block, int(getattr(config, "WEB_LOGIN_MAX_BLOCK_SECONDS", 86_400)))
    return attempts, window, base_block, max_block


def check_login(
    ip_address: str,
    username: str,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> LoginState:
    ensure_schema(db_path)
    now = int(now or time.time())
    key = _identity(ip_address, username)
    with _connect(db_path) as connection:
        row = connection.execute("SELECT * FROM login_security WHERE identity=?", (key,)).fetchone()
    if not row:
        return LoginState(True)
    blocked_until = int(row["blocked_until"] or 0)
    retry = max(0, blocked_until - now)
    return LoginState(
        allowed=retry <= 0,
        failures=int(row["failures"] or 0),
        blocked_until=blocked_until,
        retry_after=retry,
        blocks=int(row["blocks"] or 0),
    )


def record_failure(
    ip_address: str,
    username: str,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> LoginState:
    ensure_schema(db_path)
    attempts, window, base_block, max_block = _settings()
    now = int(now or time.time())
    key = _identity(ip_address, username)
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM login_security WHERE identity=?", (key,)).fetchone()
        failures = int(row["failures"] or 0) if row else 0
        first = int(row["first_failed_at"] or 0) if row else 0
        blocked_until = int(row["blocked_until"] or 0) if row else 0
        blocks = int(row["blocks"] or 0) if row else 0

        # An attempt made during an active block does not run another password
        # hash and does not endlessly extend the lock.
        if blocked_until > now:
            return LoginState(False, failures, blocked_until, blocked_until - now, blocks)

        if first <= 0 or now - first > window:
            failures = 0
            first = now
        failures += 1
        blocked_until = 0
        if failures >= attempts:
            blocks += 1
            duration = min(max_block, base_block * (2 ** max(0, blocks - 1)))
            blocked_until = now + duration
            failures = 0
            first = 0

        connection.execute(
            """
            INSERT INTO login_security(
                identity,failures,first_failed_at,last_failed_at,blocked_until,blocks
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(identity) DO UPDATE SET
                failures=excluded.failures,
                first_failed_at=excluded.first_failed_at,
                last_failed_at=excluded.last_failed_at,
                blocked_until=excluded.blocked_until,
                blocks=excluded.blocks
            """,
            (key, failures, first, now, blocked_until, blocks),
        )
    retry = max(0, blocked_until - now)
    return LoginState(retry <= 0, failures, blocked_until, retry, blocks)


def record_success(
    ip_address: str,
    username: str,
    *,
    db_path: str | Path | None = None,
) -> None:
    ensure_schema(db_path)
    with _connect(db_path) as connection:
        connection.execute("DELETE FROM login_security WHERE identity=?", (_identity(ip_address, username),))


def prune(*, now: int | None = None, db_path: str | Path | None = None) -> int:
    ensure_schema(db_path)
    now = int(now or time.time())
    cutoff = now - max(86_400, int(getattr(config, "WEB_LOGIN_SECURITY_RETENTION_DAYS", 30)) * 86_400)
    with _connect(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM login_security WHERE last_failed_at<? AND blocked_until<?",
            (cutoff, now),
        )
        return max(0, int(cursor.rowcount or 0))
