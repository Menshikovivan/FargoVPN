"""Atomically accept one receipt per purchase and one open payment per user."""
from __future__ import annotations

import secrets
import time

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


# Keep the last displayed quote through a bot restart and through retries of
# the same receipt. An explicit cancel or a new purchase after submission may
# replace it. Quotes carry no Telegram file or payment account credentials.
QUOTE_LIFETIME_SECONDS = 7 * 86400


def open_purchase_quote_sync(payer_id: int, owner_id: int, amount: int, days: int, db_path: str) -> dict:
    now = int(time.time())
    with db.connect(db_path) as connection:
        row = connection.execute(
            """INSERT INTO payment_quotes
               (payer_tg_id,billing_tg_id,expected_amount,purchased_days,purchase_token,cancel_token,expires_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (payer_tg_id) DO UPDATE SET
                 billing_tg_id=EXCLUDED.billing_tg_id,expected_amount=EXCLUDED.expected_amount,
                 purchased_days=EXCLUDED.purchased_days,purchase_token=EXCLUDED.purchase_token,
                 cancel_token=EXCLUDED.cancel_token,expires_at=EXCLUDED.expires_at
               WHERE payment_quotes.expires_at<=? OR EXISTS (
                 SELECT 1 FROM payments WHERE purchase_token=payment_quotes.purchase_token
               )
               RETURNING *""",
            (int(payer_id), int(owner_id), int(amount), int(days), secrets.token_hex(24),
             secrets.token_hex(8), now + QUOTE_LIFETIME_SECONDS, now),
        ).fetchone()
        if row is None:
            row = connection.execute("SELECT * FROM payment_quotes WHERE payer_tg_id=?", (int(payer_id),)).fetchone()
        return dict(row)


def get_purchase_quote_sync(payer_id: int, db_path: str, *, include_submitted: bool = True) -> dict | None:
    with db.connect(db_path) as connection:
        query = "SELECT * FROM payment_quotes WHERE payer_tg_id=? AND expires_at>?"
        if not include_submitted:
            query += " AND NOT EXISTS (SELECT 1 FROM payments WHERE purchase_token=payment_quotes.purchase_token)"
        row = connection.execute(query, (int(payer_id), int(time.time()))).fetchone()
        return dict(row) if row else None


def cancel_purchase_quote_sync(payer_id: int, cancel_token: str, db_path: str) -> bool:
    with db.connect(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM payment_quotes WHERE payer_tg_id=? AND cancel_token=?",
            (int(payer_id), cancel_token),
        )
        return cursor.rowcount == 1


def submit_quoted_receipt_sync(
    payer_id: int, purchase_token: str, username: str, file_id: str, needs_review: bool, db_path: str,
) -> tuple[int, bool]:
    """Record the original terms even when a transfer needs manual review."""
    with db.connect(db_path) as connection:
        quote = connection.execute(
            "SELECT * FROM payment_quotes WHERE payer_tg_id=? AND purchase_token=? AND expires_at>? FOR UPDATE",
            (int(payer_id), purchase_token, int(time.time())),
        ).fetchone()
        if not quote:
            return 0, False
        terms = dict(quote)
        owner = int(terms["billing_tg_id"])
        connection.execute("SELECT pg_advisory_xact_lock(493, hashtext(?))", (str(owner),))
        existing = connection.execute(
            "SELECT id FROM payments WHERE purchase_token=?", (purchase_token,),
        ).fetchone()
        if existing:
            return int(existing[0]), False
        if not needs_review:
            existing = connection.execute(
                """SELECT id FROM payments WHERE tg_id=? AND
                   COALESCE(status,'pending') IN ('pending','processing') ORDER BY id DESC LIMIT 1""",
                (owner,),
            ).fetchone()
            if existing:
                return int(existing[0]), False
        reason = ("Условия оплаты изменились после показа реквизитов. Сверьте чек и исходный профиль."
                  if needs_review else None)
        result = connection.execute(
            """INSERT INTO payments
               (tg_id,payer_tg_id,username,telegram_file_id,amount,expected_amount,
                ocr_status,purchase_token,purchased_days,quote_needs_review,last_error)
               VALUES (?,?,?,?,?,?,'not_checked',?,?,?,?)""",
            (owner, int(payer_id), username, file_id, int(terms["expected_amount"]), int(terms["expected_amount"]),
             purchase_token, int(terms["purchased_days"]), int(needs_review), reason),
        )
        return int(result.lastrowid), True
