"""Canonical Telegram user identity display helpers.

Display policy is deliberately centralized so bot menus, panel pages, chats and
notifications resolve the same label: real Telegram username, then a meaningful
operator comment, then the numeric Telegram ID. Telegram profile full names are
kept as metadata but are not confused with the explicit operator comment.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _clean(value: Any, limit: int = 255) -> str:
    text = str(value or "").strip()
    if text.casefold() in {"none", "null", "undefined", "n/a"}:
        return ""
    text = _CONTROL_RE.sub(" ", text)
    return " ".join(text.split())[:limit]


def normalize_username(value: Any, tg_id: Any = 0) -> str:
    """Normalize a Telegram username; reject blanks and synthetic ID labels."""
    username = _clean(value, 100).lstrip("@").strip()
    if not username or username.casefold() in {"none", "null", "undefined", "id"}:
        return ""
    # These values are generated 3x-ui/import labels, not a trustworthy
    # Telegram username. Reject all numeric ID placeholders, not only the
    # one whose suffix happens to equal the current row's TG ID.
    if re.fullmatch(r"id_?\d+", username, flags=re.IGNORECASE):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", username):
        return ""
    try:
        user_id = int(tg_id or 0)
    except (TypeError, ValueError, OverflowError):
        user_id = 0
    synthetic_id = bool(
        user_id > 0
        and re.fullmatch(rf"id_?{re.escape(str(user_id))}", username, flags=re.IGNORECASE)
    )
    if username.isdecimal() or synthetic_id:
        return ""
    return username


def normalize_comment(value: Any) -> str:
    """Normalize an operator-supplied comment used as the display fallback."""
    return _clean(value, 255)


def latest_trusted_telegram_usernames(events: Any) -> dict[int, str]:
    """Return the latest valid handle observed in an incoming Telegram update per ID.

    Usernames copied from 3x-ui client names, form fields, or operator comments
    are not Telegram identity evidence. Only incoming events explicitly recorded
    by the Telegram middleware/support handler qualify. A newer event for the
    same numeric ID wins, so a renamed handle replaces an older snapshot.
    """
    latest: dict[int, tuple[int, str]] = {}
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        if str(event.get("direction") or "").strip().casefold() != "in":
            continue
        actor = str(event.get("actor") or "").strip().casefold()
        if not (actor.startswith("telegram:") or actor == "telegram_user"):
            continue
        try:
            tg_id = int(event.get("tg_id") or 0)
            event_id = int(event.get("id") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if tg_id <= 0:
            continue
        # An empty/invalid username in a later direct Telegram update means
        # the account no longer has a usable public handle; don't resurrect an
        # older one. Non-Telegram events are filtered above and cannot clear it.
        username = normalize_username(event.get("username"), tg_id)
        if tg_id not in latest or event_id >= latest[tg_id][0]:
            latest[tg_id] = (event_id, username)
    return {tg_id: value[1] for tg_id, value in latest.items()}


def is_generated_identity_comment(value: Any, tg_id: Any, email: Any = "") -> bool:
    """Detect panel comments that are merely synthetic ID labels, not operator notes.

    FargoVPN historically used ``id_<telegram_id>`` as a fallback username and
    copied it into the 3x-ui comment. A plain ``id`` is treated as synthetic only
    when the associated client email also has the generated ID prefix.
    """
    comment = normalize_comment(value)
    if not comment:
        return False
    if re.fullmatch(r"id_?\d+", comment, flags=re.IGNORECASE):
        return True
    if comment.casefold() != "id":
        return False
    try:
        numeric_id = int(tg_id or 0)
    except (TypeError, ValueError, OverflowError):
        numeric_id = 0
    email_prefix = str(email or "").strip().split("+", 1)[0].casefold()
    return numeric_id > 0 and email_prefix in {f"id_{numeric_id}", f"id{numeric_id}"}



def build_unique_identity_index(rows: list[dict[str, Any]], field: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Build a case-insensitive identity index without silently overwriting duplicates."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _clean(row.get(field), 512).casefold()
        if key:
            buckets.setdefault(key, []).append(row)
    index = {key: values[0] for key, values in buckets.items() if len(values) == 1}
    ambiguous = {key for key, values in buckets.items() if len(values) > 1}
    return index, ambiguous


def match_local_identity(
    email: Any,
    uuid: Any,
    by_email: Mapping[str, dict[str, Any]],
    by_uuid: Mapping[str, dict[str, Any]],
    ambiguous_emails: set[str] | None = None,
    ambiguous_uuids: set[str] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Match a 3x-ui client only when its local email/UUID evidence agrees.

    Return ``(None, True)`` when an identity key is duplicated or email and UUID
    point to different Telegram records. Callers must not manufacture a clickable
    Telegram ID for that ambiguous client.
    """
    email_key = _clean(email, 512).casefold()
    uuid_key = _clean(uuid, 256).casefold()
    if (email_key and email_key in (ambiguous_emails or set())) or (
        uuid_key and uuid_key in (ambiguous_uuids or set())
    ):
        return None, True
    email_row = by_email.get(email_key) if email_key else None
    uuid_row = by_uuid.get(uuid_key) if uuid_key else None
    if email_row is not None and uuid_row is not None:
        try:
            email_id = int(email_row.get("tg_id") or 0)
            uuid_id = int(uuid_row.get("tg_id") or 0)
        except (TypeError, ValueError, OverflowError):
            return None, True
        if email_id != uuid_id:
            return None, True
        return email_row, False
    row = uuid_row or email_row
    return row, False

def get_display_name(user: Any, tg_id: Any = None) -> str:
    """Return the one canonical display label for a user-like mapping/object."""
    if isinstance(user, Mapping):
        row = user
        row_id = row.get("tg_id", row.get("telegram_id", row.get("user_id", 0)))
        if tg_id is not None:
            row_id = tg_id
        username = ""
        for key in ("username", "display_username", "telegram_username"):
            username = normalize_username(row.get(key), row_id)
            if username:
                break
        if username:
            return "@" + username
        comment = ""
        for key in ("notes", "comment", "user_comment", "operator_comment"):
            comment = normalize_comment(row.get(key))
            if comment and not is_generated_identity_comment(comment, row_id, row.get("email", "")):
                break
            comment = ""
    else:
        row_id = tg_id if tg_id is not None else 0
        username = normalize_username(user, row_id)
        if username:
            return "@" + username
        comment = ""
    try:
        numeric_id = int(row_id or 0)
    except (TypeError, ValueError, OverflowError):
        numeric_id = 0
    if comment:
        return comment
    return f"ID {numeric_id}" if numeric_id > 0 else "Пользователь"
