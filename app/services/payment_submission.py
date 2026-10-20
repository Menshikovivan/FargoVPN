"""Atomically accept one receipt per purchase and one open payment per user."""
from __future__ import annotations

import config
import db


def submit_receipt_sync(tg_id: int, username: str, file_id: str, amount: int,
                        purchase_token: str, db_path: str, purchased_days: int | None = None) -> tuple[int, bool]:
    purchased_days = int(getattr(config, "SUBSCRIPTION_DAYS", 30) if purchased_days is None else purchased_days)
    if purchased_days <= 0:
        raise ValueError("Количество оплаченных дней должно быть положительным")
    with db.connect(db_path) as connection:
        # Transaction-scoped lock is shared by all bot processes, unlike an
        # asyncio lock. Namespace 493 separates it from other advisory locks.
        connection.execute("SELECT pg_advisory_xact_lock(493, hashtext(?))", (str(tg_id),))
        existing = connection.execute(
            """SELECT id FROM payments WHERE tg_id=? AND (
                purchase_token=? OR COALESCE(status,'pending') IN ('pending','processing')
            ) ORDER BY id DESC LIMIT 1""", (int(tg_id), purchase_token),
        ).fetchone()
        if existing:
            return int(existing[0]), False
        cursor = connection.execute(
            """INSERT INTO payments
                (tg_id,username,telegram_file_id,amount,expected_amount,ocr_status,purchase_token,purchased_days)
                VALUES (?,?,?,?,?,'not_checked',?,?)""",
            (int(tg_id), username, file_id, int(amount), int(amount), purchase_token, purchased_days),
        )
        return int(cursor.lastrowid), True
