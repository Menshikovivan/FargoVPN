"""Explicit administrator-only erasure of current local user data.

Call only after successful VPN revocation/operational deletion. Historical backup
archives, Telegram messages and external provider copies are outside this API.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import re


def erase_user_data(tg_id: int, *, confirmed_tg_id: int, actor: str,
                    vpn_revoked: bool = False, identity_snapshot: dict | None = None,
                    allow_current_credentials: bool = False) -> dict:
    import config
    import db
    target = int(tg_id)
    if target != int(confirmed_tg_id) or not str(actor).strip():
        raise ValueError("Требуется явное подтверждение ID и администратор")
    if not vpn_revoked:
        raise ValueError("Сначала необходимо подтвердить удаление VPN-клиента")
    # Identifiers below are developer-owned constants, never request/backup SQL.
    predicates = {
        "payments": ("tg_id=?", (target,)),
        "referral_rewards": ("referrer_tg_id=? OR referred_tg_id=?", (target, target)),
        "user_events": ("tg_id=? OR telegram_chat_id=?", (target, target)),
        "user_message_state": ("tg_id=?", (target,)),
        "message_log": ("tg_id=?", (target,)),
        "pending_registrations": ("tg_id=?", (target,)),
        "access_attempts": ("tg_id=?", (target,)),
        "push_subscriptions": ("tg_id=?", (target,)),
        "cabinet_access_tokens": ("tg_id=?", (target,)),
        "cabinet_login_requests": ("resolved_tg_id=?", (target,)),
        "access_invites": ("owner_id=? OR used_by=?", (target, target)),
        "telegram_profile_link_requests": ("profile_tg_id=? OR created_by=?", (target, target)),
        "telegram_link_requests": ("local_tg_id=? OR resolved_tg_id=?", (target, target)),
        "telegram_update_dedup": ("chat_id=?", (target,)),
        "telegram_profile_links": ("telegram_id=? OR profile_tg_id=?", (target, target)),
    }
    identifiers = {str(target)}
    for key in ("username", "email", "uuid", "sub_id", "display_name"):
        value = str((identity_snapshot or {}).get(key) or "").strip()
        if value:
            identifiers.add(value)
    patterns = [re.compile(r"(?<![\w])" + re.escape(value) + r"(?![\w])")
                for value in sorted(identifiers, key=len, reverse=True)]
    def scrub(value):
        if not isinstance(value, str):
            return value
        for pattern in patterns:
            value = pattern.sub("[удалено]", value)
        return value
    counts = {}
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(?)", (target,))
        row = conn.execute("SELECT uuid,email,sub_id FROM users WHERE tg_id=? FOR UPDATE", (target,)).fetchone()
        if row and any(row) and not allow_current_credentials:
            raise ValueError("Сначала удалите VPN-профиль обычной операцией")
        family = conn.execute("SELECT COUNT(*) FROM telegram_profile_links WHERE profile_tg_id=? AND telegram_id<>?", (target, target)).fetchone()
        if family and int(family[0]):
            raise ValueError("Сначала отвяжите участников семейного профиля")
        for table, (where, params) in predicates.items():
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])
            conn.execute(f"DELETE FROM {table} WHERE {where}", params)
        # Structured histories have been deleted. Redact identity occurrences in
        # free-form operational audit, preserving other users' entries and dates.
        for table, columns in (("audit_log", ("actor", "details")),
                               ("identity_import_runs", ("details",)),
                               ("panel_push_logs", ("message",))):
            changed = 0
            for entry in conn.execute(f"SELECT id,{','.join(columns)} FROM {table}").fetchall():
                values = tuple(scrub(value) for value in entry[1:])
                if values != tuple(entry[1:]):
                    conn.execute(f"UPDATE {table} SET " + ','.join(f"{col}=?" for col in columns) + " WHERE id=?", values + (entry[0],))
                    changed += 1
            counts[table + "_redacted"] = changed
        # Referred accounts keep their own subscriptions; remove this identity's pointers.
        conn.execute("UPDATE users SET referred_by_tg_id=NULL,referred_by_code=NULL WHERE referred_by_tg_id=?", (target,))
        counts["users"] = int(row is not None)
        conn.execute("DELETE FROM users WHERE tg_id=?", (target,))
        conn.commit()
    cache_root = Path(getattr(config, "CHAT_MEDIA_CACHE_DIR", "/var/cache/vpn-service/chat-media")).resolve()
    cache_dir = cache_root / str(target)
    removed = 0
    if cache_dir.exists():
        if cache_dir.is_symlink() or cache_dir.resolve().parent != cache_root:
            raise RuntimeError("Небезопасный путь кэша: данные БД удалены, требуется проверка кэша")
        removed = sum(1 for path in cache_dir.rglob("*") if path.is_file())
        shutil.rmtree(cache_dir)
    return {"counts_by_table": counts, "media_files_removed": removed,
            "limits": "Старые архивы, сообщения Telegram, внешние копии и системные журналы не удаляются; свободный текст аудита очищается по известным идентификаторам, сроки хранения не изменены."}
