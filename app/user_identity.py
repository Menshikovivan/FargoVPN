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
    if not username or username.casefold() in {"none", "null", "undefined"}:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", username):
        return ""
    try:
        user_id = int(tg_id or 0)
    except (TypeError, ValueError, OverflowError):
        user_id = 0
    if username.isdecimal() or (user_id > 0 and username.casefold() == f"id_{user_id}".casefold()):
        return ""
    return username


def normalize_comment(value: Any) -> str:
    """Normalize an operator-supplied comment used as the display fallback."""
    return _clean(value, 255)


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
            if comment:
                break
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
