"""Shared subscription status and family billing helpers."""
from __future__ import annotations

from typing import Any
import config
import db


def has_subscription(user: dict | None) -> bool:
    return bool(user and any(str(user.get(key) or '').strip()
                             for key in ('uuid', 'email', 'sub_id')))


def subscription_label(user: dict | None, now_ms: int | None = None) -> str:
    if not user:
        return "Нет профиля"
    registration = str(user.get('registration_status') or 'active').strip().lower()
    if registration in {'new', 'awaiting_invite'}:
        return "Не прошёл проверку PIN-кода"
    if registration == 'banned':
        return "Заблокирован"
    if not has_subscription(user):
        return "Подписка не оплачена"
    try:
        expiry = int(user.get('expiry_time') or 0)
    except (TypeError, ValueError):
        expiry = 0
    if not bool(user.get('enable', True)):
        return "Доступ отключён"
    if expiry <= 0:
        return "Бессрочная подписка"
    import time
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return "Подписка истекла" if expiry <= now_ms else "Подписка активна"


def profile_owner(tg_id: int, db_path: str | None = None) -> int:
    with db.connect(db_path) as connection:
        row = connection.execute(
            "SELECT profile_tg_id FROM telegram_profile_links WHERE telegram_id=?", (int(tg_id),)
        ).fetchone()
    return int(row[0]) if row and int(row[0] or 0) > 0 else int(tg_id)


def linked_count(tg_id: int, db_path: str | None = None) -> int:
    owner = profile_owner(tg_id, db_path)
    with db.connect(db_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM telegram_profile_links WHERE profile_tg_id=? AND telegram_id<>?", (owner, owner)
        ).fetchone()
    return 1 + int(row[0] or 0)


def expected_payment_amount(tg_id: int, db_path: str | None = None) -> int:
    owner = profile_owner(tg_id, db_path)
    with db.connect(db_path) as connection:
        row = connection.execute(
            "SELECT custom_payment_price FROM users WHERE tg_id=?", (owner,)
        ).fetchone()
    override = row[0] if row else None
    if override is not None:
        return max(0, int(override))
    base = max(0, int(round(float(getattr(config, 'PAYMENT_PRICE', 0) or 0))))
    return base * linked_count(owner, db_path)


def apply_registration_filter(rows: list[dict[str, Any]], db_path: str | None = None) -> list[dict[str, Any]]:
    ids = [int(row.get('tg_id') or 0) for row in rows if int(row.get('tg_id') or 0) > 0]
    states: dict[int, str] = {}
    if ids:
        with db.connect(db_path) as connection:
            placeholders = ','.join('?' for _ in ids)
            found = connection.execute(
                f"SELECT tg_id,COALESCE(registration_status,'active') FROM users WHERE tg_id IN ({placeholders})", ids
            ).fetchall()
        states = {int(row[0]): str(row[1] or 'active') for row in found}
    for row in rows:
        row['registration_status'] = states.get(int(row.get('tg_id') or 0), str(row.get('registration_status') or 'active'))
    return rows


def payment_profile_id(tg_id: int, db_path: str | None = None) -> int:
    return profile_owner(tg_id, db_path)


def expected_payment_amount_for_payment(payment_id: int, db_path: str | None = None) -> int:
    with db.connect(db_path) as connection:
        row = connection.execute("SELECT COALESCE(expected_amount,amount,0) FROM payments WHERE id=?", (int(payment_id),)).fetchone()
    return max(0, int(row[0] or 0)) if row else 0


def profile_accounts(tg_id: int, db_path: str | None = None) -> list[dict[str, Any]]:
    owner = profile_owner(tg_id, db_path)
    with db.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT u.tg_id,u.username FROM telegram_profile_links l JOIN users u ON u.tg_id=l.telegram_id WHERE l.profile_tg_id=? ORDER BY CASE WHEN l.telegram_id=? THEN 0 ELSE 1 END,l.telegram_id",
            (owner, owner),
        ).fetchall()
    return [{"tg_id": int(row[0]), "username": str(row[1] or "")} for row in rows]


def approve_profile_link_sync(token: str, telegram_id: int, db_path: str | None = None) -> int:
    """Consume a short-lived request only when the invited account starts the bot."""
    import hashlib, time
    digest = hashlib.sha256(str(token).encode()).hexdigest()
    with db.connect(db_path) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(494, hashtext(?))", (digest,))
        row = connection.execute(
            "SELECT profile_tg_id,expires_at,consumed_at FROM telegram_profile_link_requests WHERE token_hash=?",
            (digest,),
        ).fetchone()
        if not row or row[2] or int(row[1] or 0) < int(time.time()):
            raise ValueError("Ссылка привязки недействительна, истекла или уже использована")
        owner = int(row[0])
        if owner == int(telegram_id):
            raise ValueError("Нельзя привязать основной аккаунт к самому себе")
        users = connection.execute(
            "SELECT tg_id,COALESCE(registration_status,'active'),COALESCE(uuid,''),COALESCE(email,''),COALESCE(sub_id,''),COALESCE(expiry_time,0) FROM users WHERE tg_id IN (?,?) ORDER BY tg_id FOR UPDATE",
            (owner, int(telegram_id)),
        ).fetchall()
        by_id = {int(item[0]): item for item in users}
        if owner not in by_id or int(telegram_id) not in by_id:
            raise ValueError("Оба аккаунта должны начать диалог с ботом")
        if str(by_id[owner][1]).lower() != 'active' or str(by_id[int(telegram_id)][1]).lower() != 'active':
            raise ValueError("Оба аккаунта должны пройти проверку PIN-кода")
        alias = by_id[int(telegram_id)]
        if str(alias[2] or '').strip() or str(alias[3] or '').strip() or str(alias[4] or '').strip() or int(alias[5] or 0) > 0:
            raise ValueError("У дополнительного аккаунта уже есть данные VPN-подписки; сначала разберите профиль в панели")
        connection.execute(
            "INSERT INTO telegram_profile_links(telegram_id,profile_tg_id,linked_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(telegram_id) DO NOTHING",
            (int(telegram_id), owner),
        )
        current = connection.execute(
            "SELECT profile_tg_id FROM telegram_profile_links WHERE telegram_id=?", (int(telegram_id),)
        ).fetchone()
        if not current or int(current[0]) != owner:
            raise ValueError("Этот аккаунт уже связан с другим профилем")
        connection.execute(
            "UPDATE telegram_profile_link_requests SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL",
            (int(time.time()), digest),
        )
        return owner
