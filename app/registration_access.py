"""Durable invitation-gated Telegram registration for FargoVPN.

The module deliberately keeps registration state in PostgreSQL (via the
project's database adapter) and never relies on aiogram FSM storage for
access control.  The Telegram middleware calls these synchronous helpers via
asyncio.to_thread().
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import re
import time
from typing import Any, Iterator

import db as database_adapter
import config

LOGGER = logging.getLogger("fargovpn-registration")

NEW = "new"
AWAITING_INVITE = "awaiting_invite"
ACTIVE = "active"
BANNED = "banned"
_VALID_STATUSES = {NEW, AWAITING_INVITE, ACTIVE, BANNED}
_CODE_RE = re.compile(r"^\d{4}$")

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_ATTEMPT_WINDOW_SECONDS = 600
DEFAULT_BLOCK_SECONDS = 900


def _config_int(name: str, default: int, minimum: int = 1, maximum: int = 86_400) -> int:
    try:
        value = int(getattr(config, name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def max_attempts() -> int:
    return _config_int("REGISTRATION_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, 1, 100)


def attempt_window_seconds() -> int:
    return _config_int("REGISTRATION_ATTEMPT_WINDOW_SECONDS", DEFAULT_ATTEMPT_WINDOW_SECONDS, 60, 86_400)


def block_seconds() -> int:
    return _config_int("REGISTRATION_BLOCK_SECONDS", DEFAULT_BLOCK_SECONDS, 60, 7 * 86_400)


def normalize_invitation_code(value: Any) -> str | None:
    """Accept exactly four ASCII digits; never silently strip other characters."""
    text = str(value or "").strip()
    return text if _CODE_RE.fullmatch(text) else None


def _clean_username(value: Any) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]+", "", str(value or "").strip().lstrip("@"))
    return value[:80] or ""


def _clean_display_name(value: Any) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "").strip())
    return value[:160].strip()


def _row_get(row: Any, key: str, index: int | None = None, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        if index is not None:
            try:
                return row[index]
            except (IndexError, TypeError):
                return default
    return default


def _row_dict(row: Any) -> dict[str, Any]:
    """Convert both sqlite3.Row and the PostgreSQL CompatRow by column name."""
    return {key: row[key] for key in row.keys()}


@contextmanager
def _connect() -> Iterator[Any]:
    connection = database_adapter.connect(config.DB_PATH, timeout=20)
    try:
        # Safe no-op on PostgreSQL; useful for legacy SQLite/manual tests.
        connection.execute("PRAGMA busy_timeout=20000")
        yield connection
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception as rollback_error:
            LOGGER.debug("Registration DB rollback failed: %s", rollback_error)
        raise
    finally:
        connection.close()


@dataclass(frozen=True)
class RegistrationState:
    tg_id: int
    status: str
    attempts: int = 0
    attempts_reset_at: int = 0
    blocked_until: int = 0
    created: bool = False

    @property
    def blocked(self) -> bool:
        return self.blocked_until > int(time.time())


@dataclass(frozen=True)
class RegistrationResult:
    status: str
    owner: dict[str, Any] | None = None
    attempt_count: int = 0
    blocked_until: int = 0

    @property
    def registered(self) -> bool:
        return self.status == "registered"


def _select_state(connection: Any, tg_id: int) -> Any:
    return connection.execute(
        """
        SELECT tg_id, COALESCE(registration_status,'') AS registration_status,
               COALESCE(registration_attempts,0) AS registration_attempts,
               COALESCE(registration_attempts_reset_at,0) AS registration_attempts_reset_at,
               COALESCE(registration_blocked_until,0) AS registration_blocked_until
        FROM users WHERE tg_id=?
        """,
        (int(tg_id),),
    ).fetchone()


def ensure_user_registration_state_sync(
    tg_id: int,
    username: str | None = None,
    display_name: str | None = None,
) -> RegistrationState:
    """Read/create a Telegram user with durable invitation-gated state."""
    tg_id = int(tg_id)
    if tg_id <= 0:
        raise ValueError("Telegram ID must be positive")
    clean_username = _clean_username(username)
    clean_display = _clean_display_name(display_name)
    with _connect() as connection:
        row = _select_state(connection, tg_id)
        created = False
        if row is None:
            connection.execute(
                """
                INSERT INTO users(
                    tg_id, username, display_name, uuid, email, expiry_time, enable,
                    up, down, total, sub_id, last_reminder_days,
                    registration_status, registration_attempts,
                    registration_attempts_reset_at, registration_blocked_until
                )
                VALUES(?,?,?,'','',0,1,0,0,0,'',-1,'awaiting_invite',0,0,0)
                ON CONFLICT(tg_id) DO NOTHING
                """,
                (tg_id, clean_username or None, clean_display),
            )
            row = _select_state(connection, tg_id)
            created = True
        else:
            # Identity refresh must never change authorization state.
            connection.execute(
                "UPDATE users SET username=CASE WHEN ?<>'' THEN ? ELSE username END, display_name=CASE WHEN ?<>'' THEN ? ELSE display_name END WHERE tg_id=?",
                (clean_username, clean_username, clean_display, clean_display, tg_id),
            )
        status = str(_row_get(row, "registration_status", 1, AWAITING_INVITE) or "").strip().lower()
        if status not in _VALID_STATUSES:
            # Default-deny on corrupted/unknown authorization states.
            status = AWAITING_INVITE
            connection.execute(
                "UPDATE users SET registration_status=?, registration_attempts=0, registration_blocked_until=0 WHERE tg_id=?",
                (status, tg_id),
            )
        # Keep the legacy pending table in sync for identity/recovery tooling.
        if status == AWAITING_INVITE:
            connection.execute(
                "INSERT INTO pending_registrations(tg_id,username) VALUES(?,?) ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username",
                (tg_id, clean_username),
            )
        elif status == ACTIVE:
            connection.execute("DELETE FROM pending_registrations WHERE tg_id=?", (tg_id,))
        return RegistrationState(
            tg_id=tg_id,
            status=status,
            attempts=int(_row_get(row, "registration_attempts", 2, 0) or 0),
            attempts_reset_at=int(_row_get(row, "registration_attempts_reset_at", 3, 0) or 0),
            blocked_until=int(_row_get(row, "registration_blocked_until", 4, 0) or 0),
            created=created,
        )


def registration_state_sync(tg_id: int) -> RegistrationState | None:
    tg_id = int(tg_id)
    if tg_id <= 0:
        return None
    with _connect() as connection:
        row = _select_state(connection, tg_id)
        if row is None:
            return None
        status = str(_row_get(row, "registration_status", 1, AWAITING_INVITE) or "").strip().lower()
        if status not in _VALID_STATUSES:
            status = AWAITING_INVITE
        return RegistrationState(
            tg_id=tg_id,
            status=status,
            attempts=int(_row_get(row, "registration_attempts", 2, 0) or 0),
            attempts_reset_at=int(_row_get(row, "registration_attempts_reset_at", 3, 0) or 0),
            blocked_until=int(_row_get(row, "registration_blocked_until", 4, 0) or 0),
        )


def lookup_active_referral_owner_sync(code: str, exclude_tg_id: int = 0) -> dict[str, Any] | None:
    normalized = normalize_invitation_code(code)
    if normalized is None:
        return None
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM users
            WHERE tg_id>0 AND registration_status='active'
              AND UPPER(referral_code)=? AND tg_id<>?
            LIMIT 1
            """,
            (normalized, int(exclude_tg_id)),
        ).fetchone()
        return _row_dict(row) if row else None


def register_with_invitation_sync(
    tg_id: int,
    username: str | None,
    code: str,
    display_name: str | None = None,
) -> RegistrationResult:
    """Atomically transition one awaiting user to active using an active owner's code."""
    normalized = normalize_invitation_code(code)
    if normalized is None:
        return RegistrationResult("invalid_format")
    tg_id = int(tg_id)
    clean_username = _clean_username(username) or f"id_{tg_id}"
    clean_display = _clean_display_name(display_name)
    now = int(time.time())
    with _connect() as connection:
        row = _select_state(connection, tg_id)
        if row is None:
            connection.execute(
                """
                INSERT INTO users(tg_id,username,display_name,uuid,email,expiry_time,enable,up,down,total,sub_id,last_reminder_days,registration_status,registration_attempts,registration_attempts_reset_at,registration_blocked_until)
                VALUES(?,?,?,'','',0,1,0,0,0,'',-1,'awaiting_invite',0,0,0)
                ON CONFLICT(tg_id) DO NOTHING
                """,
                (tg_id, clean_username, clean_display),
            )
            row = _select_state(connection, tg_id)
        status = str(_row_get(row, "registration_status", 1, AWAITING_INVITE) or AWAITING_INVITE).strip().lower()
        blocked_until = int(_row_get(row, "registration_blocked_until", 4, 0) or 0)
        if status == BANNED:
            return RegistrationResult(BANNED, blocked_until=blocked_until)
        if status == ACTIVE:
            return RegistrationResult("already_active")
        if blocked_until > now:
            return RegistrationResult("blocked", blocked_until=blocked_until)

        owner_row = connection.execute(
            """
            SELECT * FROM users
            WHERE tg_id>0 AND tg_id<>? AND registration_status='active'
              AND UPPER(referral_code)=?
            LIMIT 1
            """,
            (tg_id, normalized),
        ).fetchone()
        if owner_row is None:
            return RegistrationResult("invalid_code")
        owner = _row_dict(owner_row)

        updated = connection.execute(
            """
            UPDATE users
            SET username=?,
                display_name=CASE WHEN ?<>'' THEN ? ELSE display_name END,
                referred_by_tg_id=?,
                referred_by_code=?,
                registered_at=COALESCE(registered_at,CAST(CURRENT_TIMESTAMP AS TEXT)),
                registration_status='active',
                registration_attempts=0,
                registration_attempts_reset_at=0,
                registration_blocked_until=0
            WHERE tg_id=? AND registration_status='awaiting_invite'
              AND COALESCE(registration_blocked_until,0)<=?
              AND EXISTS (
                    SELECT 1 FROM users AS owner_check
                     WHERE owner_check.tg_id=?
                       AND owner_check.registration_status='active'
                       AND UPPER(owner_check.referral_code)=?
              )
            """,
            (
                clean_username,
                clean_display,
                clean_display,
                int(owner["tg_id"]),
                normalized,
                tg_id,
                now,
                int(owner["tg_id"]),
                normalized,
            ),
        )
        if int(getattr(updated, "rowcount", 0) or 0) != 1:
            latest = _select_state(connection, tg_id)
            latest_status = str(_row_get(latest, "registration_status", 1, AWAITING_INVITE) or AWAITING_INVITE).strip().lower()
            latest_block = int(_row_get(latest, "registration_blocked_until", 4, 0) or 0)
            if latest_status == ACTIVE:
                return RegistrationResult("already_active")
            if latest_block > now:
                return RegistrationResult("blocked", blocked_until=latest_block)
            return RegistrationResult("race_lost")
        connection.execute("DELETE FROM pending_registrations WHERE tg_id=?", (tg_id,))
        return RegistrationResult("registered", owner=owner)


def record_failed_invitation_attempt_sync(tg_id: int) -> RegistrationResult:
    """Atomically count a wrong 4-digit code and apply the persistent cooldown."""
    tg_id = int(tg_id)
    now = int(time.time())
    window = attempt_window_seconds()
    threshold = max_attempts()
    block_for = block_seconds()
    with _connect() as connection:
        row = _select_state(connection, tg_id)
        if row is None:
            return RegistrationResult("missing_user")
        status = str(_row_get(row, "registration_status", 1, AWAITING_INVITE) or AWAITING_INVITE).strip().lower()
        existing_block = int(_row_get(row, "registration_blocked_until", 4, 0) or 0)
        if status != AWAITING_INVITE:
            return RegistrationResult(status, attempt_count=int(_row_get(row, "registration_attempts", 2, 0) or 0), blocked_until=existing_block)
        if existing_block > now:
            return RegistrationResult("blocked", attempt_count=int(_row_get(row, "registration_attempts", 2, 0) or 0), blocked_until=existing_block)

        result = connection.execute(
            """
            UPDATE users
            SET registration_attempts = CASE
                    WHEN COALESCE(registration_attempts_reset_at,0)<=? THEN 1
                    ELSE COALESCE(registration_attempts,0)+1
                END,
                registration_attempts_reset_at = CASE
                    WHEN COALESCE(registration_attempts_reset_at,0)<=? THEN ?
                    ELSE registration_attempts_reset_at
                END
            WHERE tg_id=? AND registration_status='awaiting_invite'
              AND COALESCE(registration_blocked_until,0)<=?
            RETURNING registration_attempts, registration_attempts_reset_at
            """,
            (now, now, now + window, tg_id, now),
        ).fetchone()
        if result is None:
            latest = _select_state(connection, tg_id)
            latest_block = int(_row_get(latest, "registration_blocked_until", 4, 0) or 0)
            latest_attempts = int(_row_get(latest, "registration_attempts", 2, 0) or 0)
            if latest_block > now:
                return RegistrationResult("blocked", attempt_count=latest_attempts, blocked_until=latest_block)
            return RegistrationResult("race_lost", attempt_count=latest_attempts)
        attempts = int(_row_get(result, "registration_attempts", 0, 0) or 0)
        blocked_until = 0
        blocked_now = attempts >= threshold
        if blocked_now:
            blocked_until = now + block_for
            connection.execute(
                "UPDATE users SET registration_blocked_until=? WHERE tg_id=? AND registration_status='awaiting_invite' AND COALESCE(registration_blocked_until,0)<=?",
                (blocked_until, tg_id, now),
            )
            try:
                connection.execute(
                    "INSERT INTO audit_log(actor,action,details) VALUES(?,?,?)",
                    ("telegram:registration", "invite_bruteforce_block", str({"tg_id": tg_id, "attempts": attempts, "blocked_until": blocked_until})),
                )
            except Exception as error:
                LOGGER.warning("Не удалось записать событие блокировки подбора кода: %s", error)
        return RegistrationResult("blocked" if blocked_now else "invalid_code", attempt_count=attempts, blocked_until=blocked_until)


def authorize_admin_user_sync(tg_id: int, username: str | None = None, display_name: str | None = None, actor: str = "web") -> bool:
    """Explicitly authorize a user created/bound by an authenticated administrator.

    This is not a Telegram self-registration path and therefore preserves the
    existing web-admin workflow without allowing unsolicited Telegram contacts
    to bypass the invitation gate.
    """
    tg_id = int(tg_id)
    if tg_id <= 0:
        return False
    clean_username = _clean_username(username)
    clean_display = _clean_display_name(display_name)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO users(
                tg_id, username, display_name, uuid, email, expiry_time, enable,
                up, down, total, sub_id, last_reminder_days,
                registration_status, registration_attempts,
                registration_attempts_reset_at, registration_blocked_until,
                registered_at
            )
            VALUES(?,?,?,'','',0,1,0,0,0,'',-1,'active',0,0,0,CURRENT_TIMESTAMP)
            ON CONFLICT(tg_id) DO UPDATE SET
                username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE users.username END,
                display_name=CASE WHEN excluded.display_name<>'' THEN excluded.display_name ELSE users.display_name END,
                registration_status='active',
                registration_attempts=0,
                registration_attempts_reset_at=0,
                registration_blocked_until=0,
                registered_at=COALESCE(users.registered_at,CAST(CURRENT_TIMESTAMP AS TEXT))
            """,
            (tg_id, clean_username, clean_display),
        )
        connection.execute("DELETE FROM pending_registrations WHERE tg_id=?", (tg_id,))
        connection.execute(
            "INSERT INTO audit_log(actor,action,details) VALUES(?,?,?)",
            (str(actor or "web"), "registration_admin_authorized", str({"tg_id": tg_id})),
        )
    return True
