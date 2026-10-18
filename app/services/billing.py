"""Shared subscription status and family billing helpers."""
from __future__ import annotations

from typing import Any
import config
import db


def has_subscription(user: dict | None) -> bool:
    if user and user.get('family_subscription'):
        return bool(user.get('family_owner_has_subscription'))
    return bool(user and any(str(user.get(key) or '').strip()
                             for key in ('uuid', 'email', 'sub_id')))


def subscription_label(user: dict | None, now_ms: int | None = None) -> str:
    if not user:
        return "Нет профиля"
    registration = str(user.get('registration_status') or 'active').strip().lower()
    if registration in {'new', 'awaiting_invite'}:
        return "Не зарегистрирован"
    if registration == 'banned':
        return "Заблокирован"
    family = bool(user.get('family_subscription'))
    if not has_subscription(user):
        return "Семейная · без подписки" if family else "Без подписки"
    try:
        expiry = int(
            user.get('family_owner_expiry_time')
            if family else user.get('expiry_time') or 0
        )
    except (TypeError, ValueError):
        expiry = 0
    enabled = user.get('family_owner_enable', True) if family else user.get('enable', True)
    prefix = "Семейная · " if family else ""
    if not bool(enabled):
        return prefix + ("доступ отключён" if family else "Доступ отключён")
    if expiry <= 0:
        return prefix + ("бессрочная подписка" if family else "Бессрочная подписка")
    import time
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if expiry <= now_ms:
        return prefix + ("подписка истекла" if family else "Подписка истекла")
    return prefix + ("подписка активна" if family else "Подписка активна")


def subscription_status_tone(label: str | None) -> str:
    normalized = str(label or "").casefold()
    if any(marker in normalized for marker in ("заблокирован", "отключён")):
        return "bad"
    if any(marker in normalized for marker in ("нет профиля", "не зарегистрирован", "без подписки", "истек")):
        return "warn"
    return "good"


def subscription_status_emoji(label: str | None) -> str:
    return {"bad": "🔴", "warn": "🟡", "good": "🟢"}[subscription_status_tone(label)]


def telegram_identity_label(tg_id: int, username: str = "", display_name: str = "") -> str:
    clean_username = str(username or "").strip().lstrip("@")
    if clean_username:
        return f"@{clean_username}"
    clean_display_name = " ".join(str(display_name or "").split())
    if clean_display_name:
        return clean_display_name
    return f"ID {int(tg_id)}" if int(tg_id or 0) > 0 else "Пользователь"


def family_profile_map(db_path: str | None = None) -> dict[int, dict[str, Any]]:
    with db.connect(db_path) as connection:
        rows = connection.execute(
            """SELECT l.telegram_id,l.profile_tg_id,
                      p.uuid,p.email,p.sub_id,p.expiry_time,p.enable
                 FROM telegram_profile_links l
                 LEFT JOIN users p ON p.tg_id=l.profile_tg_id"""
        ).fetchall()
    profiles: dict[int, dict[str, Any]] = {}
    for row in rows:
        profile = {
            "family_subscription": True,
            "family_owner_tg_id": int(row[1] or 0),
            "family_owner_expiry_time": int(row[5] or 0),
            "family_owner_enable": bool(row[6]) if row[6] is not None else False,
            "family_owner_has_subscription": any(
                str(row[index] or "").strip() for index in (2, 3, 4)
            ),
        }
        profiles[int(row[0])] = profile
    return profiles


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
            """SELECT u.tg_id,
                      COALESCE(NULLIF(u.username,''),(
                          SELECT NULLIF(e.username,'') FROM user_events e
                           WHERE e.tg_id=u.tg_id AND TRIM(COALESCE(e.username,''))<>''
                           ORDER BY e.id DESC LIMIT 1
                      ),'') AS username,
                      u.display_name
                 FROM telegram_profile_links l
                 JOIN users u ON u.tg_id=l.telegram_id
                WHERE l.profile_tg_id=?
                ORDER BY CASE WHEN l.telegram_id=? THEN 0 ELSE 1 END,l.telegram_id""",
            (owner, owner),
        ).fetchall()
    return [
        {
            "tg_id": int(row[0]),
            "username": str(row[1] or ""),
            "display_name": str(row[2] or ""),
            "label": telegram_identity_label(int(row[0]), str(row[1] or ""), str(row[2] or "")),
        }
        for row in rows
    ]


def _has_own_vpn_identity(user_row: Any) -> bool:
    """Whether a Telegram account already owns a separate VPN identity.

    Legacy rows can retain a positive expiry timestamp after a client was
    removed. Expiry alone is not a credential and should not block an invited
    account from joining a family profile.
    """
    return any(str(user_row[index] or "").strip() for index in (2, 3, 4))


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
        # Serialize membership changes for one profile so concurrent invites
        # cannot exceed the four-account cap.
        connection.execute("SELECT pg_advisory_xact_lock(495, hashtext(?))", (str(owner),))
        users = connection.execute(
            "SELECT tg_id,COALESCE(registration_status,'active'),COALESCE(uuid,''),COALESCE(email,''),COALESCE(sub_id,''),COALESCE(expiry_time,0) FROM users WHERE tg_id IN (?,?) ORDER BY tg_id FOR UPDATE",
            (owner, int(telegram_id)),
        ).fetchall()
        by_id = {int(item[0]): item for item in users}
        if owner not in by_id or int(telegram_id) not in by_id:
            raise ValueError("Оба аккаунта должны начать диалог с ботом")
        if str(by_id[owner][1]).lower() != 'active':
            raise ValueError("Основной аккаунт должен пройти проверку PIN-кода")
        existing = connection.execute(
            "SELECT profile_tg_id FROM telegram_profile_links WHERE telegram_id=?", (int(telegram_id),)
        ).fetchone()
        if existing:
            if int(existing[0]) != owner:
                raise ValueError("Этот аккаунт уже связан с другим профилем")
            # A new, valid invite from the same owner is safe to acknowledge
            # for an already-linked member, even when the family is full.
            # Consume only after ownership has been confirmed.
            connection.execute(
                "UPDATE telegram_profile_link_requests SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL",
                (int(time.time()), digest),
            )
            return owner
        count = connection.execute(
            "SELECT COUNT(*) FROM telegram_profile_links WHERE profile_tg_id=?", (owner,)
        ).fetchone()
        if int(count[0] or 0) >= 4:
            raise ValueError("В одном профиле может быть не более 4 Telegram аккаунтов")
        alias = by_id[int(telegram_id)]
        if _has_own_vpn_identity(alias):
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
        # Keep pending PIN state intact. Once it becomes active, the alias is
        # eligible for broadcasts and shared subscription access.
        connection.execute(
            "UPDATE telegram_profile_link_requests SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL",
            (int(time.time()), digest),
        )
        return owner


def create_profile_link_request_sync(owner_tg_id: int, db_path: str | None = None, ttl_seconds: int = 1800) -> str:
    """Create a one-use family invitation for the profile owner."""
    import hashlib
    import secrets
    import time
    owner = profile_owner(int(owner_tg_id), db_path)
    if owner != int(owner_tg_id):
        raise ValueError("Создать ссылку может только владелец профиля")
    with db.connect(db_path) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(495, hashtext(?))", (str(owner),))
        user = connection.execute(
            "SELECT COALESCE(registration_status,'active') FROM users WHERE tg_id=?", (owner,)
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) FROM telegram_profile_links WHERE profile_tg_id=?", (owner,)
        ).fetchone()
        if not user or str(user[0]).lower() != 'active':
            raise ValueError("Основной аккаунт должен пройти проверку PIN-кода")
        if int(count[0] or 0) >= 4:
            raise ValueError("Достигнут лимит: максимум 4 аккаунта вместе с основным")
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        connection.execute(
            "DELETE FROM telegram_profile_link_requests WHERE expires_at<? OR consumed_at IS NOT NULL", (now,)
        )
        connection.execute(
            "INSERT INTO telegram_profile_link_requests(token_hash,profile_tg_id,created_at,expires_at,created_by) VALUES(?,?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), owner, now, now + max(60, int(ttl_seconds)), owner),
        )
    return token


def unlink_profile_member_sync(owner_tg_id: int, telegram_id: int, db_path: str | None = None) -> None:
    owner = profile_owner(int(owner_tg_id), db_path)
    if owner != int(owner_tg_id):
        raise ValueError("Отвязать участника может только владелец профиля")
    if int(telegram_id) == owner:
        raise ValueError("Основной аккаунт нельзя отвязать от собственного профиля")
    with db.connect(db_path) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(495, hashtext(?))", (str(owner),))
        row = connection.execute(
            "SELECT profile_tg_id FROM telegram_profile_links WHERE telegram_id=?", (int(telegram_id),)
        ).fetchone()
        if not row or int(row[0]) != owner:
            raise ValueError("Аккаунт не привязан к этому профилю")
        connection.execute("DELETE FROM telegram_profile_links WHERE telegram_id=?", (int(telegram_id),))
