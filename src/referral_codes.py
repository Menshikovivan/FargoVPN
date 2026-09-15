"""Daily rotating 4-digit invitation codes."""
from __future__ import annotations

import re
import secrets
import sqlite3
import time

ROTATION_PERIOD_SECONDS = 24 * 60 * 60
CODE_PATTERN = re.compile(r"^[0-9]{4}$")

def ensure_referral_code(db_path: str, user_id: int) -> str:
    user_id = int(user_id)
    now = int(time.time())
    with sqlite3.connect(db_path, timeout=20) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT referral_code, COALESCE(referral_code_updated_at,0) AS updated_at "
            "FROM users WHERE tg_id=?", (user_id,)
        ).fetchone()
        if not row:
            raise RuntimeError("Пользователь не найден")
        current = str(row["referral_code"] or "").strip()
        updated_at = int(row["updated_at"] or 0)
        if CODE_PATTERN.fullmatch(current) and updated_at > 0 and now - updated_at < ROTATION_PERIOD_SECONDS:
            return current
        chooser = secrets.SystemRandom()
        for _ in range(64):
            candidate = f"{chooser.randrange(10000):04d}"
            if candidate == current:
                continue
            if connection.execute("SELECT 1 FROM users WHERE referral_code=? LIMIT 1", (candidate,)).fetchone() is None:
                new_code = candidate
                break
        else:
            used = {str(r[0]).strip() for r in connection.execute(
                "SELECT referral_code FROM users WHERE referral_code IS NOT NULL AND TRIM(referral_code)<>''"
            ).fetchall() if CODE_PATTERN.fullmatch(str(r[0] or '').strip())}
            available = [f"{n:04d}" for n in range(10000) if f"{n:04d}" not in used]
            if not available:
                raise RuntimeError("Не удалось создать уникальный 4-значный код приглашения")
            new_code = chooser.choice(available)
        connection.execute("UPDATE users SET referral_code=?, referral_code_updated_at=? WHERE tg_id=?", (new_code, now, user_id))
        connection.commit()
        return new_code
