"""Subscription presence is independent of Telegram registration."""
from __future__ import annotations

import time


def has_subscription(user: dict | None) -> bool:
    return bool(user and any(str(user.get(key) or '').strip()
                             for key in ('uuid', 'email', 'sub_id')))


def has_active_subscription(user: dict | None, now_ms: int | None = None) -> bool:
    if not has_subscription(user) or not bool(user.get('enable', True)):
        return False
    expiry = int(user.get('expiry_time') or 0)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return expiry <= 0 or expiry > now_ms
