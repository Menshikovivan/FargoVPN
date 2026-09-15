"""Idempotent referral reward ledger for FargoVPN.

The reward is keyed by the invited Telegram user, not by payment id, because the
business rule grants exactly one reward after that user's first successful payment.
The ledger is intentionally separate from 3x-ui and is safe across payment retries,
process restarts and concurrent approval attempts.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from services.xui_api import get_client_record_sync, update_client_sync

logger = logging.getLogger(__name__)
REWARD_DAYS = 10


PENDING = "pending"
GRANTED = "granted"
SKIPPED_UNLIMITED = "skipped_unlimited"
LEGACY_IGNORED = "legacy_ignored"


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path or config.DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS referral_rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_tg_id INTEGER NOT NULL,
            referred_tg_id INTEGER NOT NULL UNIQUE,
            reward_days INTEGER NOT NULL DEFAULT 10,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            granted_at TEXT,
            target_expiry_ms INTEGER,
            observed_expiry_before_ms INTEGER,
            details TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer ON referral_rewards(referrer_tg_id, id DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_referral_rewards_status ON referral_rewards(status, id DESC)"
    )


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _user_row(connection: sqlite3.Connection, tg_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM users WHERE tg_id=?", (int(tg_id),)).fetchone()


def _set_reward_status(
    connection: sqlite3.Connection,
    referred_tg_id: int,
    status: str,
    *,
    target_expiry_ms: int | None = None,
    observed_expiry_before_ms: int | None = None,
    details: str = "",
) -> None:
    connection.execute(
        """
        UPDATE referral_rewards
           SET status=?,
               granted_at=CASE WHEN ? IN ('granted','skipped_unlimited','legacy_ignored') THEN CURRENT_TIMESTAMP ELSE granted_at END,
               target_expiry_ms=COALESCE(?,target_expiry_ms),
               observed_expiry_before_ms=COALESCE(?,observed_expiry_before_ms),
               details=CASE WHEN ?<>'' THEN ? ELSE details END
         WHERE referred_tg_id=?
        """,
        (status, status, target_expiry_ms, observed_expiry_before_ms, details, details, int(referred_tg_id)),
    )


def register_referral_if_needed(
    referred_tg_id: int,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Create a pending reward ledger entry for an invited user, once.

    Returns the row when the invited user has a valid referrer. Self-referrals and
    users without a positive Telegram ID are ignored.
    """
    referred_tg_id = int(referred_tg_id)
    if referred_tg_id <= 0:
        return None
    with _connect(db_path) as connection:
        ensure_schema(connection)
        user = _user_row(connection, referred_tg_id)
        if not user:
            return None
        referrer = int(user["referred_by_tg_id"] or 0)
        if referrer <= 0 or referrer == referred_tg_id:
            return None
        connection.execute(
            """
            INSERT OR IGNORE INTO referral_rewards(
                referrer_tg_id,referred_tg_id,reward_days,status
            ) VALUES(?,?,?,?)
            """,
            (referrer, referred_tg_id, REWARD_DAYS, PENDING),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM referral_rewards WHERE referred_tg_id=?", (referred_tg_id,)
        ).fetchone()
        return dict(row) if row else None


def baseline_existing_paid_referrals(db_path: str | Path | None = None) -> int:
    """Protect historical users from accidental retroactive referral rewards.

    A referral whose invited user had already reached an approved payment before the
    referral ledger existed is marked LEGACY_IGNORED. Future referrals remain eligible.
    """
    changed = 0
    with _connect(db_path) as connection:
        ensure_schema(connection)
        rows = connection.execute(
            """
            SELECT u.tg_id AS referred_tg_id, u.referred_by_tg_id AS referrer_tg_id
              FROM users u
             WHERE u.tg_id>0
               AND COALESCE(u.referred_by_tg_id,0)>0
               AND EXISTS (
                   SELECT 1 FROM payments p
                    WHERE p.tg_id=u.tg_id AND p.status='approved'
               )
            """
        ).fetchall()
        for row in rows:
            cur = connection.execute(
                """
                INSERT OR IGNORE INTO referral_rewards(
                    referrer_tg_id,referred_tg_id,reward_days,status,granted_at,details
                ) VALUES(?,?,?,?,CURRENT_TIMESTAMP,?)
                """,
                (
                    int(row["referrer_tg_id"]),
                    int(row["referred_tg_id"]),
                    REWARD_DAYS,
                    LEGACY_IGNORED,
                    "существующая подтверждённая оплата до внедрения реферального бонуса",
                ),
            )
            changed += int(cur.rowcount > 0)
        connection.commit()
    return changed


def referral_stats(user_tg_id: int, db_path: str | Path | None = None) -> dict[str, int]:
    user_tg_id = int(user_tg_id)
    with _connect(db_path) as connection:
        ensure_schema(connection)
        invited = int(connection.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by_tg_id=?", (user_tg_id,)
        ).fetchone()[0] or 0)
        paid = int(connection.execute(
            """
            SELECT COUNT(*)
              FROM users u
             WHERE u.referred_by_tg_id=?
               AND EXISTS (SELECT 1 FROM payments p WHERE p.tg_id=u.tg_id AND p.status='approved')
            """,
            (user_tg_id,),
        ).fetchone()[0] or 0)
        rewards = int(connection.execute(
            "SELECT COUNT(*) FROM referral_rewards WHERE referrer_tg_id=? AND status='granted'",
            (user_tg_id,),
        ).fetchone()[0] or 0)
        days = int(connection.execute(
            "SELECT COALESCE(SUM(reward_days),0) FROM referral_rewards WHERE referrer_tg_id=? AND status='granted'",
            (user_tg_id,),
        ).fetchone()[0] or 0)
    return {"invited": invited, "paid": paid, "rewards": rewards, "days": days}


def _load_reward(db_path: str | Path | None, referred_tg_id: int) -> dict[str, Any] | None:
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM referral_rewards WHERE referred_tg_id=?",
            (int(referred_tg_id),),
        ).fetchone()
        return dict(row) if row else None


def _reserve_target(
    referred_tg_id: int,
    candidate_target: int,
    observed_expiry: int,
    *,
    payment_id: int | None,
    db_path: str | Path | None,
) -> int:
    """Persist exactly one target expiry for this referral reward.

    The target becomes the durable idempotency key before any 3x-ui side effect.
    Concurrent workers therefore cannot turn +10 days into +20/+30 by observing
    the already-modified remote expiry between retries.
    """
    details = f"payment_id={int(payment_id or 0)}" if payment_id else ""
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status,target_expiry_ms FROM referral_rewards WHERE referred_tg_id=?",
            (int(referred_tg_id),),
        ).fetchone()
        if not row:
            raise RuntimeError("Не найдена запись реферального бонуса")
        status = str(row["status"] or PENDING)
        existing_target = int(row["target_expiry_ms"] or 0)
        if status in {GRANTED, SKIPPED_UNLIMITED, LEGACY_IGNORED}:
            return existing_target
        if existing_target > 0:
            return existing_target
        cur = connection.execute(
            """
            UPDATE referral_rewards
               SET target_expiry_ms=?,
                   observed_expiry_before_ms=?,
                   details=CASE WHEN ?<>'' THEN ? ELSE details END
             WHERE referred_tg_id=?
               AND status='pending'
               AND target_expiry_ms IS NULL
            """,
            (
                int(candidate_target), int(observed_expiry), details, details,
                int(referred_tg_id),
            ),
        )
        if cur.rowcount == 1:
            connection.commit()
            return int(candidate_target)
        row = connection.execute(
            "SELECT target_expiry_ms FROM referral_rewards WHERE referred_tg_id=?",
            (int(referred_tg_id),),
        ).fetchone()
        connection.commit()
        existing_target = int(row[0] or 0) if row else 0
        if existing_target <= 0:
            raise RuntimeError("Не удалось зафиксировать целевой срок реферального бонуса")
        return existing_target


def apply_referral_reward(
    referred_tg_id: int,
    *,
    payment_id: int | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Award +10 days exactly once after the invited user's first approved payment.

    The durable target expiry is reserved before the remote 3x-ui side effect. This
    prevents a race where a second concurrent approval observes the first update and
    adds another ten days. Remote writes are exact assignments, never increments.
    """
    referred_tg_id = int(referred_tg_id)
    if referred_tg_id <= 0:
        return {"status": "not_eligible", "message": "Некорректный Telegram ID приглашённого"}

    with _connect(db_path) as connection:
        ensure_schema(connection)
        invited = _user_row(connection, referred_tg_id)
        if not invited:
            return {"status": "not_eligible", "message": "Приглашённый пользователь не найден"}
        referrer_tg_id = int(invited["referred_by_tg_id"] or 0)
        if referrer_tg_id <= 0 or referrer_tg_id == referred_tg_id:
            return {"status": "not_eligible", "message": "Реферальная связь отсутствует"}
        connection.execute(
            "INSERT OR IGNORE INTO referral_rewards(referrer_tg_id,referred_tg_id,reward_days,status) VALUES(?,?,?,?)",
            (referrer_tg_id, referred_tg_id, REWARD_DAYS, PENDING),
        )
        row = connection.execute(
            "SELECT * FROM referral_rewards WHERE referred_tg_id=?", (referred_tg_id,)
        ).fetchone()
        if not row:
            return {"status": "not_eligible", "message": "Не удалось создать запись реферального бонуса"}
        reward = dict(row)
        if reward["status"] in {GRANTED, SKIPPED_UNLIMITED, LEGACY_IGNORED}:
            return {"status": str(reward["status"]), "already_processed": True, **reward}
        existing_target = int(reward.get("target_expiry_ms") or 0)

    with _connect(db_path) as connection:
        referrer = _user_row(connection, referrer_tg_id)
    if not referrer:
        return {"status": "failed", "message": "Реферер не найден"}

    email = str(referrer["email"] or "").strip()
    if not email:
        return {"status": "failed", "message": "У реферера нет привязанного клиента 3x-ui"}

    try:
        record = get_client_record_sync(email)
        client = record.get("client") or {}
        current_expiry = int(client.get("expiryTime") or record.get("normalized", {}).get("expiry_time") or 0)
        if current_expiry <= 0:
            with _connect(db_path) as connection:
                cur = connection.execute(
                    """
                    UPDATE referral_rewards
                       SET status=?, granted_at=CURRENT_TIMESTAMP,
                           observed_expiry_before_ms=0,
                           details=?
                     WHERE referred_tg_id=? AND status='pending'
                    """,
                    (SKIPPED_UNLIMITED, "У реферера бессрочная подписка; срок не изменён", int(referred_tg_id)),
                )
                connection.commit()
                if cur.rowcount != 1:
                    return {"status": SKIPPED_UNLIMITED, "already_processed": True, "referrer_tg_id": referrer_tg_id, "referred_tg_id": referred_tg_id, "reward_days": REWARD_DAYS}
            return {
                "status": SKIPPED_UNLIMITED,
                "already_processed": False,
                "referrer_tg_id": referrer_tg_id,
                "referred_tg_id": referred_tg_id,
                "reward_days": REWARD_DAYS,
            }

        now_ms = int(time.time() * 1000)
        candidate_target = max(now_ms, current_expiry) + REWARD_DAYS * 86_400_000
        target_expiry = existing_target or _reserve_target(
            referred_tg_id,
            candidate_target,
            current_expiry,
            payment_id=payment_id,
            db_path=db_path,
        )

        # A later retry must never reduce a subscription that another admin action
        # legitimately extended beyond the reserved reward target. Reaching or
        # exceeding the target means the ten-day benefit is already economically
        # satisfied, so mark it granted without overwriting the newer expiry.
        latest = get_client_record_sync(email)
        latest_client = latest.get("client") or {}
        latest_expiry = int(latest_client.get("expiryTime") or latest.get("normalized", {}).get("expiry_time") or 0)
        if latest_expiry <= 0:
            final_expiry = 0
            with _connect(db_path) as connection:
                cur = connection.execute(
                    """
                    UPDATE referral_rewards
                       SET status=?, granted_at=CURRENT_TIMESTAMP,
                           target_expiry_ms=COALESCE(target_expiry_ms,?),
                           observed_expiry_before_ms=COALESCE(observed_expiry_before_ms,?),
                           details=?
                     WHERE referred_tg_id=? AND status='pending'
                    """,
                    (
                        SKIPPED_UNLIMITED, target_expiry, current_expiry,
                        "После фиксации бонуса клиент стал бессрочным; срок не изменён",
                        int(referred_tg_id),
                    ),
                )
                connection.commit()
                if cur.rowcount != 1:
                    return {"status": SKIPPED_UNLIMITED, "already_processed": True, "referrer_tg_id": referrer_tg_id, "referred_tg_id": referred_tg_id, "reward_days": REWARD_DAYS}
            return {
                "status": SKIPPED_UNLIMITED,
                "already_processed": False,
                "referrer_tg_id": referrer_tg_id,
                "referred_tg_id": referred_tg_id,
                "reward_days": REWARD_DAYS,
            }
        if latest_expiry >= target_expiry:
            final_expiry = latest_expiry
        else:
            update_client_sync(email, {"expiryTime": target_expiry, "enable": True}, record=latest)
            final_record = get_client_record_sync(email)
            final_client = final_record.get("client") or {}
            final_expiry = int(final_client.get("expiryTime") or final_record.get("normalized", {}).get("expiry_time") or 0)
            if final_expiry < target_expiry:
                raise RuntimeError(f"3x-ui не подтвердила целевой срок {target_expiry}; получено {final_expiry}")

        with _connect(db_path) as connection:
            connection.execute(
                "UPDATE users SET expiry_time=?,enable=1,last_reminder_days=-1 WHERE tg_id=?",
                (final_expiry, referrer_tg_id),
            )
            cur = connection.execute(
                """
                UPDATE referral_rewards
                   SET status=?, granted_at=CURRENT_TIMESTAMP,
                       target_expiry_ms=COALESCE(target_expiry_ms,?),
                       observed_expiry_before_ms=COALESCE(observed_expiry_before_ms,?),
                       details=CASE WHEN ?<>'' THEN ? ELSE details END
                 WHERE referred_tg_id=? AND status='pending'
                """,
                (
                    GRANTED, target_expiry, current_expiry,
                    f"payment_id={int(payment_id or 0)}" if payment_id else "",
                    f"payment_id={int(payment_id or 0)}" if payment_id else "",
                    int(referred_tg_id),
                ),
            )
            connection.commit()
            if cur.rowcount != 1:
                return {
                    "status": GRANTED,
                    "already_processed": True,
                    "referrer_tg_id": referrer_tg_id,
                    "referred_tg_id": referred_tg_id,
                    "reward_days": REWARD_DAYS,
                    "expiry_time": final_expiry,
                }
        return {
            "status": GRANTED,
            "already_processed": False,
            "referrer_tg_id": referrer_tg_id,
            "referred_tg_id": referred_tg_id,
            "reward_days": REWARD_DAYS,
            "expiry_time": final_expiry,
        }
    except Exception as error:
        logger.exception("Не удалось начислить реферальный бонус за %s", referred_tg_id)
        with _connect(db_path) as connection:
            _set_reward_status(connection, referred_tg_id, PENDING, details=f"Последняя ошибка: {str(error)[:700]}")
            connection.commit()
        return {"status": "failed", "message": str(error), "referrer_tg_id": referrer_tg_id, "referred_tg_id": referred_tg_id}
