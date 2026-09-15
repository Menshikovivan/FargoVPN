"""Telegram identity recovery and idempotent subscription activation.

Telegram ID is the only authoritative user key. Usernames are presentation
metadata and may change. Legacy 2.x installations can be repaired from a
positive 3x-ui ``tgId`` or, only as a migration hint, the final ``_<tg_id>``
email suffix used by old releases.
"""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import config
import referral_rewards
from services.xui_api import (
    add_client_sync,
    bind_client_tg_id_sync,
    extend_client_sync,
    fetch_snapshot_sync,
    find_client_by_telegram_id_sync,
    get_client_record_sync,
    inbound_ids_sync,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubscriptionResult:
    tg_id: int
    username: str
    email: str
    uuid: str
    sub_id: str
    expiry_time: int
    created: bool
    recovered: bool = False


@dataclass(frozen=True)
class PaymentApprovalResult:
    success: bool
    message: str
    subscription: SubscriptionResult | None = None
    already_processed: bool = False
    referral: dict[str, Any] | None = None


def clean_username(username: str | None, tg_id: int) -> str:
    value = str(username or "").strip().lstrip("@")
    value = re.sub(r"[\x00-\x1f\x7f]+", "", value)[:80]
    return value or f"id_{int(tg_id)}"


def panel_email_for(username: str | None, tg_id: int) -> str:
    """Build the visible 3x-ui client name.

    Manual/local users (negative internal IDs) should appear in 3x-ui by the
    nickname entered by the administrator, not by a generated numeric ID.
    Telegram-linked users keep the legacy unique suffix for backwards
    compatibility.
    """
    name = clean_username(username, tg_id)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._-") or f"id_{int(tg_id)}"
    if tg_id < 0:
        return safe[:120]
    suffix = f"+{int(tg_id)}"
    return f"{safe[: max(1, 120 - len(suffix))]}{suffix}"


def _db_path(db_path: str | Path | None = None) -> str:
    return str(db_path or config.DB_PATH)


@contextmanager
def _connect(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(_db_path(db_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_telegram_user_shell_sync(
    tg_id: int,
    username: str | None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create a lightweight local row as soon as Telegram contacts the bot.

    A new Telegram user can legitimately have no 3x-ui client yet (for example
    immediately after /start).  The web panel still needs a durable user row so
    incoming messages, unread badges and the conversation page have a target.
    Subscription creation later fills uuid/email/sub_id/expiry without changing
    the real Telegram ID.
    """
    tg_id = int(tg_id)
    if tg_id <= 0:
        raise ValueError("Telegram ID должен быть положительным")
    display_name = clean_username(username, tg_id)
    existing = get_user_by_tg_id(tg_id, db_path)
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect(db_path) as connection:
        if existing:
            current_name = str(existing.get("username") or "")
            if display_name and display_name != current_name:
                connection.execute(
                    "UPDATE users SET username=?,last_sync_at=? WHERE tg_id=?",
                    (display_name, now_text, tg_id),
                )
        else:
            connection.execute(
                """
                INSERT INTO users(
                    tg_id,username,uuid,email,expiry_time,enable,up,down,total,sub_id,
                    last_online,last_online_ts,last_sync_at,last_reminder_days
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,-1)
                """,
                (tg_id, display_name, "", "", 0, 1, 0, 0, 0, "", "", 0, now_text),
            )
    result = get_user_by_tg_id(tg_id, db_path)
    if not result:
        raise RuntimeError("Не удалось создать локальную запись Telegram-пользователя")
    return result


def get_user_by_tg_id(tg_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with _connect(db_path) as connection:
        row = connection.execute("SELECT * FROM users WHERE tg_id=?", (int(tg_id),)).fetchone()
    return dict(row) if row else None


def _upsert_user(
    tg_id: int,
    username: str,
    panel_client: dict[str, Any],
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    email = str(panel_client.get("email") or "").strip()
    if not email:
        raise RuntimeError("3x-ui не вернула email клиента")
    uuid_value = str(panel_client.get("uuid") or panel_client.get("id") or "")
    sub_id = str(panel_client.get("sub_id") or panel_client.get("subId") or "")
    expiry = int(panel_client.get("expiry_time") or panel_client.get("expiryTime") or 0)
    enable = int(bool(panel_client.get("enable", True)))
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    username = clean_username(username, tg_id)

    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        # Remove only legacy, unbound aliases for this exact panel client.
        connection.execute(
            "DELETE FROM users WHERE tg_id<=0 AND tg_id<>? AND (LOWER(email)=LOWER(?) OR (?<>'' AND LOWER(uuid)=LOWER(?)))",
            (int(tg_id), email, uuid_value, uuid_value),
        )
        connection.execute(
            """
            INSERT INTO users(
                tg_id,username,uuid,email,expiry_time,enable,sub_id,last_online,
                last_sync_at,last_reminder_days
            ) VALUES(?,?,?,?,?,?,?,?,?,-1)
            ON CONFLICT(tg_id) DO UPDATE SET
                username=excluded.username,
                uuid=excluded.uuid,
                email=excluded.email,
                expiry_time=excluded.expiry_time,
                enable=excluded.enable,
                sub_id=excluded.sub_id,
                last_online=excluded.last_online,
                last_sync_at=excluded.last_sync_at,
                last_reminder_days=-1
            """,
            (int(tg_id), username, uuid_value, email, expiry, enable, sub_id, now_text, now_text),
        )
    result = get_user_by_tg_id(tg_id, db_path)
    if not result:
        raise RuntimeError("Не удалось сохранить пользователя в локальной базе")
    return result


def _client_from_snapshot_by_email(email: str, force: bool = True) -> dict[str, Any] | None:
    if not email:
        return None
    snapshot = fetch_snapshot_sync(force=force)
    value = snapshot.get("by_email", {}).get(email.strip().lower())
    return dict(value) if value else None


def recover_user_identity_sync(
    tg_id: int,
    username: str | None,
    db_path: str | Path | None = None,
    force: bool = True,
) -> dict[str, Any] | None:
    """Recover/refresh a local row without changing the subscription term."""
    tg_id = int(tg_id)
    if tg_id <= 0:
        return None
    display_name = clean_username(username, tg_id)
    local = get_user_by_tg_id(tg_id, db_path)
    candidate: dict[str, Any] | None = None
    if local and local.get("email"):
        candidate = _client_from_snapshot_by_email(str(local["email"]), force=force)
    if not candidate:
        candidate = find_client_by_telegram_id_sync(tg_id, force=force)

    if not candidate:
        if local and display_name != str(local.get("username") or ""):
            with _connect(db_path) as connection:
                connection.execute("UPDATE users SET username=? WHERE tg_id=?", (display_name, tg_id))
            local["username"] = display_name
        return local

    email = str(candidate.get("email") or "")
    if int(candidate.get("tg_id") or 0) != tg_id:
        try:
            candidate = bind_client_tg_id_sync(email, tg_id)
        except Exception as error:
            # Identity recovery should still make statistics usable when an older
            # panel cannot persist tgId. The email suffix remains the migration proof.
            logger.warning("Не удалось записать tgId=%s в 3x-ui для %s: %s", tg_id, email, error)
            candidate = dict(candidate)
            candidate["tg_id"] = tg_id
    _upsert_user(tg_id, display_name, candidate, db_path)
    return get_user_by_tg_id(tg_id, db_path)


def _candidate_for_subscription(
    tg_id: int,
    local: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if local and local.get("email"):
        candidate = _client_from_snapshot_by_email(str(local["email"]), force=True)
        if candidate:
            return candidate
    return find_client_by_telegram_id_sync(tg_id, force=True)


def _allocate_local_tg_id(username: str, db_path: str | Path | None = None) -> int:
    """Allocate a negative local ID for a user that has no Telegram account."""
    base_name = clean_username(username, -1).lower()
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT tg_id, username FROM users WHERE tg_id < 0 ORDER BY tg_id ASC"
        ).fetchall()
        used = {int(row["tg_id"]) for row in rows}
        # Reuse an existing local row with the same nickname when possible.
        for row in rows:
            if str(row["username"] or "").strip().lower() == base_name:
                return int(row["tg_id"])
        candidate = -1
        while candidate in used:
            candidate -= 1
        return candidate


def ensure_subscription_sync(
    tg_id: int,
    username: str | None,
    days: int = 30,
    db_path: str | Path | None = None,
) -> SubscriptionResult:
    """Extend an existing client or create one.

    Telegram users use their real positive Telegram ID. Users without Telegram
    use a generated negative local ID; the local ID never becomes a 3x-ui
    display name and is not sent to Telegram.
    """
    tg_id = int(tg_id)
    days = int(days)
    if days <= 0:
        raise ValueError("Количество дней должно быть положительным")
    if tg_id == 0:
        if not str(username or "").strip():
            raise ValueError("Для ручной выдачи без Telegram необходимо указать имя пользователя")
        tg_id = _allocate_local_tg_id(str(username), db_path)
    display_name = clean_username(username, tg_id)
    local = get_user_by_tg_id(tg_id, db_path)
    candidate = _candidate_for_subscription(tg_id, local)

    if candidate:
        email = str(candidate.get("email") or "")
        updated = extend_client_sync(email, days, tg_id=tg_id)
        user = _upsert_user(tg_id, display_name, updated, db_path)
        return SubscriptionResult(
            tg_id=tg_id,
            username=str(user.get("username") or display_name),
            email=str(user.get("email") or email),
            uuid=str(user.get("uuid") or updated.get("uuid") or ""),
            sub_id=str(user.get("sub_id") or updated.get("sub_id") or ""),
            expiry_time=int(user.get("expiry_time") or updated.get("expiry_time") or 0),
            created=False,
            recovered=local is None or str(local.get("email") or "").lower() != email.lower(),
        )

    email = str((local or {}).get("email") or panel_email_for(display_name, tg_id))
    if tg_id < 0 and not local:
        # Keep the exact nickname in 3x-ui whenever possible. If somebody
        # already used the same nickname, add the smallest numeric suffix.
        base_email = email
        existing = _client_from_snapshot_by_email(base_email, force=True)
        suffix = 2
        while existing:
            email = f"{base_email}_{suffix}"
            existing = _client_from_snapshot_by_email(email, force=True)
            suffix += 1
            if suffix > 1000:
                break
    client_uuid = str((local or {}).get("uuid") or uuid.uuid4())
    sub_id = str((local or {}).get("sub_id") or secrets.token_hex(8))
    expiry = int(time.time() * 1000) + days * 86_400_000
    client = {
        "email": email,
        "id": client_uuid,
        "uuid": client_uuid,
        "subId": sub_id,
        "expiryTime": expiry,
        "totalGB": 0,
        "limitIp": 0,
        "enable": True,
        "tgId": tg_id if tg_id > 0 else 0,
        "reset": 0,
        "comment": display_name,
        "flow": "xtls-rprx-vision",
        "security": "auto",
    }
    try:
        add_client_sync(client, inbound_ids_sync())
        panel_client = {
            "email": email,
            "uuid": client_uuid,
            "sub_id": sub_id,
            "expiry_time": expiry,
            "enable": True,
            "tg_id": tg_id,
        }
        created = True
        recovered = False
    except Exception as error:
        # The common upgrade failure is an existing 3x-ui client whose local row
        # was lost. Resolve the duplicate and extend it rather than approving a
        # payment without access.
        duplicate_hint = "already" in str(error).lower() or "use" in str(error).lower()
        recovered_client = _client_from_snapshot_by_email(email, force=True)
        if not recovered_client:
            try:
                recovered_client = get_client_record_sync(email)["normalized"]
            except Exception:
                recovered_client = find_client_by_telegram_id_sync(tg_id, force=True)
        if not recovered_client:
            raise RuntimeError(f"3x-ui не создала и не нашла клиента {email}: {error}") from error
        if not duplicate_hint:
            logger.warning("Создание клиента завершилось ошибкой, но существующий клиент найден: %s", error)
        email = str(recovered_client.get("email") or email)
        panel_client = extend_client_sync(email, days, tg_id=tg_id)
        created = False
        recovered = True

    user = _upsert_user(tg_id, display_name, panel_client, db_path)
    return SubscriptionResult(
        tg_id=tg_id,
        username=str(user.get("username") or display_name),
        email=str(user.get("email") or email),
        uuid=str(user.get("uuid") or client_uuid),
        sub_id=str(user.get("sub_id") or sub_id),
        expiry_time=int(user.get("expiry_time") or expiry),
        created=created,
        recovered=recovered,
    )



def repair_panel_client_identities_sync(db_path: str | Path | None = None) -> dict[str, Any]:
    """Repair visible 3x-ui client names/emails for registered Telegram users.

    The bot DB remains authoritative for Telegram identity.  This function only
    changes client rows through the 3x-ui API and never edits x-ui's SQLite schema.
    Legacy ``name_123``/numeric records are matched by tgId or old email and are
    moved to the current ``username+TelegramID`` format.
    """
    from services.xui_api import fetch_snapshot_sync, update_client_sync
    with _connect(db_path) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT tg_id,username,email FROM users WHERE tg_id>0 ORDER BY tg_id"
        ).fetchall()]
    snapshot = fetch_snapshot_sync(force=True)
    clients = [dict(item) for item in snapshot.get("clients", []) if isinstance(item, dict)]
    by_email = {str(item.get("email") or "").strip().casefold(): item for item in clients if str(item.get("email") or "").strip()}
    by_tg = {int(item.get("tg_id") or 0): item for item in clients if int(item.get("tg_id") or 0) > 0}
    result: dict[str, Any] = {"checked": 0, "updated": 0, "unchanged": 0, "missing": 0, "errors": []}
    for row in rows:
        tg_id = int(row.get("tg_id") or 0)
        username = clean_username(row.get("username"), tg_id)
        desired = panel_email_for(username, tg_id)
        current_email = str(row.get("email") or "").strip()
        client = by_tg.get(tg_id) or by_email.get(current_email.casefold()) or by_email.get(desired.casefold())
        result["checked"] += 1
        if not client:
            result["missing"] += 1
            continue
        old_email = str(client.get("email") or current_email).strip()
        try:
            changes: dict[str, Any] = {"comment": username, "tgId": tg_id}
            if old_email != desired and desired.casefold() not in by_email:
                changes["email"] = desired
            if changes.keys() == {"comment", "tgId"} and old_email == desired:
                result["unchanged"] += 1
                continue
            record = get_client_record_sync(old_email)
            update_client_sync(old_email, changes, record=record)
            with _connect(db_path) as connection:
                connection.execute("UPDATE users SET email=?,username=?,last_sync_at=CURRENT_TIMESTAMP WHERE tg_id=?", (desired if "email" in changes else old_email, username, tg_id))
            result["updated"] += 1
            if "email" in changes:
                by_email.pop(old_email.casefold(), None)
                by_email[desired.casefold()] = {**client, "email": desired, "tg_id": tg_id}
        except Exception as error:
            result["errors"].append(f"{tg_id}: {str(error)[:500]}")
    return result


def import_local_users_to_3xui_sync(db_path: str | Path | None = None) -> dict[str, Any]:
    """Restore all registered Telegram users from the bot DB into the configured 3x-ui inbounds.

    Existing clients are reused by email/Telegram ID when possible; missing clients are
    recreated using the stored UUID/subId/expiry.  The 3x-ui database schema is not touched.
    """
    from services.xui_api import add_client_sync, inbound_ids_sync
    with _connect(db_path) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM users WHERE tg_id > 0 ORDER BY tg_id"
        ).fetchall()]
    inbounds = inbound_ids_sync()
    result = {"total": len(rows), "created": 0, "reused": 0, "updated": 0, "skipped": 0, "errors": [], "users": []}
    if not inbounds:
        raise RuntimeError("В 3x-ui не найдено ни одного inbound для добавления пользователей")
    for row in rows:
        tg_id = int(row.get("tg_id") or 0)
        username = clean_username(row.get("username"), tg_id)
        email = str(row.get("email") or "").strip() or panel_email_for(username, tg_id)
        uid = str(row.get("uuid") or "").strip() or str(uuid.uuid4())
        sub_id = str(row.get("sub_id") or "").strip() or secrets.token_hex(8)
        expiry = int(row.get("expiry_time") or 0)
        if tg_id <= 0 or not email:
            result["skipped"] += 1
            continue
        try:
            existing = None
            try:
                existing = _client_from_snapshot_by_email(email, force=True)
            except Exception:
                existing = None
            if existing:
                try:
                    updated = bind_client_tg_id_sync(email, tg_id)
                except Exception:
                    updated = existing
                _upsert_user(tg_id, username, updated, db_path)
                result["reused"] += 1
                result["updated"] += 1
                action = "восстановлен/обновлён"
            else:
                client = {
                    "email": email,
                    "id": uid,
                    "uuid": uid,
                    "subId": sub_id,
                    "expiryTime": expiry,
                    "totalGB": 0,
                    "limitIp": 0,
                    "enable": bool(row.get("enable", 1)),
                    "tgId": tg_id,
                    "reset": 0,
                    "comment": username,
                    "flow": "xtls-rprx-vision",
                    "security": "auto",
                }
                add_client_sync(client, inbounds)
                panel_client = {
                    "email": email, "uuid": uid, "sub_id": sub_id,
                    "expiry_time": expiry, "enable": bool(row.get("enable", 1)), "tg_id": tg_id,
                }
                _upsert_user(tg_id, username, panel_client, db_path)
                result["created"] += 1
                action = "создан"
            result["users"].append({"tg_id": tg_id, "username": username, "email": email, "action": action})
        except Exception as error:
            result["errors"].append({"tg_id": tg_id, "username": username, "error": str(error)[:500]})
    return result

def approve_payment_sync(
    payment_id: int,
    processed_by: str,
    days: int = 30,
    db_path: str | Path | None = None,
) -> PaymentApprovalResult:
    """Atomically claim a pending receipt, activate it, and award its one-time referral reward."""
    payment_id = int(payment_id)
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not row:
            return PaymentApprovalResult(False, "Платёж не найден")
        payment = dict(row)
        status = str(payment.get("status") or "pending")
        if status == "approved":
            already_processed = True
        elif status == "processing":
            return PaymentApprovalResult(False, "Платёж уже обрабатывается другим администратором")
        elif status == "declined":
            return PaymentApprovalResult(False, "Платёж уже отклонён; создайте новую заявку или верните статус в ожидание")
        else:
            connection.execute(
                "UPDATE payments SET status='processing',processed_at=CURRENT_TIMESTAMP,processed_by=?,last_error=NULL WHERE id=?",
                (str(processed_by), payment_id),
            )
            already_processed = False

    if already_processed:
        referral_result = referral_rewards.apply_referral_reward(
            int(payment["tg_id"]), payment_id=payment_id, db_path=db_path
        )
        return PaymentApprovalResult(
            True,
            "Платёж уже был подтверждён",
            already_processed=True,
            referral=referral_result,
        )

    try:
        result = ensure_subscription_sync(
            int(payment["tg_id"]),
            str(payment.get("username") or ""),
            days=days,
            db_path=db_path,
        )
    except Exception as error:
        logger.exception("Не удалось подтвердить платёж %s", payment_id)
        with _connect(db_path) as connection:
            connection.execute(
                "UPDATE payments SET status='pending',processed_at=NULL,processed_by=NULL,last_error=? WHERE id=?",
                (str(error)[:1000], payment_id),
            )
        return PaymentApprovalResult(False, str(error))

    with _connect(db_path) as connection:
        connection.execute(
            "UPDATE payments SET status='approved',processed_at=CURRENT_TIMESTAMP,processed_by=?,last_error=NULL,auto_approved=? WHERE id=?",
            (str(processed_by), int(str(processed_by).startswith("ocr:")), payment_id),
        )
    action = "создана" if result.created else "продлена"
    referral_result = referral_rewards.apply_referral_reward(
        int(payment["tg_id"]), payment_id=payment_id, db_path=db_path
    )
    if referral_result.get("status") == referral_rewards.GRANTED:
        logger.info(
            "Реферальный бонус +%s дней начислен: referrer=%s referred=%s payment=%s",
            referral_rewards.REWARD_DAYS, referral_result.get("referrer_tg_id"),
            referral_result.get("referred_tg_id"), payment_id,
        )
    elif referral_result.get("status") == referral_rewards.SKIPPED_UNLIMITED:
        logger.info(
            "Реферальная конверсия подтверждена для бессрочного реферера: referrer=%s referred=%s payment=%s",
            referral_result.get("referrer_tg_id"), referral_result.get("referred_tg_id"), payment_id,
        )
    elif referral_result.get("status") == "failed":
        logger.warning("Реферальный бонус оставлен в pending для повторной обработки: %s", referral_result.get("message"))
    return PaymentApprovalResult(True, f"Подписка {action}", subscription=result, referral=referral_result)


def decline_payment_sync(
    payment_id: int,
    processed_by: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM payments WHERE id=?", (int(payment_id),)).fetchone()
        if not row:
            raise LookupError("Платёж не найден")
        payment = dict(row)
        status = str(payment.get("status") or "pending")
        if status == "approved":
            raise RuntimeError("Подтверждённый платёж нельзя отклонить")
        if status == "processing":
            raise RuntimeError("Платёж уже обрабатывается другим администратором")
        if status == "declined":
            return payment
        connection.execute(
            "UPDATE payments SET status='declined',processed_at=CURRENT_TIMESTAMP,processed_by=?,last_error=NULL WHERE id=?",
            (str(processed_by), int(payment_id)),
        )
    return payment


def bind_database_user_tg_id_sync(
    old_tg_id: int,
    new_tg_id: int,
    username: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Bind an old temporary row to a real Telegram ID."""
    old_tg_id = int(old_tg_id)
    new_tg_id = int(new_tg_id)
    if new_tg_id <= 0:
        raise ValueError("Новый Telegram ID должен быть положительным")
    with _connect(db_path) as connection:
        row = connection.execute("SELECT * FROM users WHERE tg_id=?", (old_tg_id,)).fetchone()
        if not row:
            raise LookupError("Пользователь не найден")
        existing = connection.execute("SELECT * FROM users WHERE tg_id=?", (new_tg_id,)).fetchone()
        if existing and old_tg_id != new_tg_id:
            raise RuntimeError("Этот Telegram ID уже привязан к другому пользователю")
        user = dict(row)

    email = str(user.get("email") or "")
    if email:
        bind_client_tg_id_sync(email, new_tg_id)
    display_name = clean_username(username or str(user.get("username") or ""), new_tg_id)
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if old_tg_id != new_tg_id:
            connection.execute("UPDATE users SET tg_id=?,username=? WHERE tg_id=?", (new_tg_id, display_name, old_tg_id))
            connection.execute("UPDATE payments SET tg_id=? WHERE tg_id=?", (new_tg_id, old_tg_id))
        else:
            connection.execute("UPDATE users SET username=? WHERE tg_id=?", (display_name, new_tg_id))
    result = get_user_by_tg_id(new_tg_id, db_path)
    if not result:
        raise RuntimeError("Не удалось сохранить Telegram ID")
    return result
