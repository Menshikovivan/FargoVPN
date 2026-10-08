"""Small PostgreSQL-backed fixed-window limits for user-triggered actions."""
from __future__ import annotations

import time
from dataclasses import dataclass

import config
import db as database_adapter


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    count: int
    retry_after: int


def consume(identity: str, action: str, limit: int, window_seconds: int) -> LimitResult:
    now = int(time.time())
    limit = max(1, int(limit))
    window_seconds = max(1, int(window_seconds))
    with database_adapter.connect(config.DB_PATH, timeout=20) as connection:
        row = connection.execute(
            """
            INSERT INTO action_rate_limits(identity,action,window_started,request_count,updated_at)
            VALUES(?,?,?,1,CURRENT_TIMESTAMP)
            ON CONFLICT(identity,action) DO UPDATE SET
                request_count=CASE
                    WHEN ?-action_rate_limits.window_started>=?
                    THEN 1 ELSE action_rate_limits.request_count+1 END,
                window_started=CASE
                    WHEN ?-action_rate_limits.window_started>=?
                    THEN ? ELSE action_rate_limits.window_started END,
                updated_at=CURRENT_TIMESTAMP
            RETURNING request_count,window_started
            """,
            (str(identity)[:160], str(action)[:80], now, now, window_seconds, now, window_seconds, now),
        ).fetchone()
        connection.commit()
    count = int(row[0])
    started = int(row[1])
    return LimitResult(count <= limit, count, max(0, started + window_seconds - now))

