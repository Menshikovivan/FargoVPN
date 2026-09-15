"""Canonical personal cabinet service.

Owns cabinet URL/token handling, user identity matching, personal cabinet identity and URL/token handling. HTTP routes are intentionally kept in webapp.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, quote

import config

TOKEN_RE = re.compile(r"^(?P<tg_id>[1-9][0-9]{0,19})\.(?P<signature>[A-Za-z0-9_-]{43})$")


def _secret() -> bytes:
    value = str(getattr(config, "WEB_SECRET_KEY", "") or "").encode()
    if len(value) < 16:
        raise RuntimeError("WEB_SECRET_KEY слишком короткий")
    return value


def _sign(payload: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def make_token(tg_id: int, sub_id: str = "") -> str:
    tg_id = int(tg_id)
    if tg_id <= 0:
        raise ValueError("Неверный Telegram ID")
    return f"{tg_id}.{_sign(f'cabinet-link-v2:{tg_id}') }"


def token_user_id(token: str) -> int:
    m = TOKEN_RE.fullmatch(str(token or "").strip())
    return int(m.group("tg_id")) if m else 0


def verify_token(token: str, sub_id: str = "") -> int:
    m = TOKEN_RE.fullmatch(str(token or "").strip())
    if not m:
        return 0
    tg_id = int(m.group("tg_id"))
    supplied = m.group("signature")
    if hmac.compare_digest(supplied, _sign(f"cabinet-link-v2:{tg_id}")):
        return tg_id
    if sub_id:
        legacy = _sign(f"cabinet-link-v1:{tg_id}:{str(sub_id).strip()}")
        if hmac.compare_digest(supplied, legacy):
            return tg_id
    return 0


def public_prefix() -> str:
    raw = str(getattr(config, "WEB_PUBLIC_PREFIX", "") or "").strip().strip("/")
    parts = [p for p in raw.split("/") if p]
    if len(parts) % 2 == 0 and parts and "/".join(parts[:len(parts)//2]) == "/".join(parts[len(parts)//2:]):
        parts = parts[:len(parts)//2]
    return "/" + "/".join(parts) if parts else ""


def public_path(path: str = "/") -> str:
    prefix = public_prefix()
    raw = str(path or "/")
    if not raw.startswith("/"):
        raw = "/" + raw
    if not prefix or raw == prefix or raw.startswith(prefix + "/"):
        return raw
    return prefix + raw


def public_web_base_url() -> str:
    source = str(getattr(config, "WEB_DOMAIN", "") or getattr(config, "BOT_PANEL_URL", "") or "").strip()
    if not source:
        return ""
    if "://" not in source:
        source = "https://" + source
    parts = urlsplit(source)
    host = parts.hostname or ""
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    prefix = public_prefix().strip("/")
    return f"https://{host}{('/' + prefix) if prefix else ''}"


def personal_url(tg_id: int, sub_id: str = "") -> str:
    base = public_web_base_url().rstrip("/")
    if not base:
        return ""
    path = str(getattr(config, "CABINET_PATH", "/cabinet") or "/cabinet").strip()
    if not path.startswith("/"):
        path = "/" + path
    prefix = public_prefix()
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        path = path[len(prefix):] or "/cabinet"
    return f"{base}{path}?access={quote(make_token(tg_id, sub_id), safe='')}"


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or config.DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def identity_candidates(tg_id: int, db_path: str | Path | None = None) -> list[str]:
    candidates: list[str] = []
    with _connect(db_path) as db:
        for row in db.execute(
            "SELECT username FROM users WHERE tg_id=? UNION ALL "
            "SELECT username FROM user_events WHERE tg_id=? ORDER BY username",
            (int(tg_id), int(tg_id)),
        ):
            value = str(row[0] or "").strip().lstrip("@").strip().casefold()
            if value and value not in candidates:
                candidates.append(value)
    return candidates[:64]


def _matched_names(db: sqlite3.Connection, candidates: list[str]) -> list[str]:
    out: list[str] = []
    for name in candidates:
        users = int(db.execute(
            "SELECT COUNT(DISTINCT tg_id) FROM users "
            "WHERE replace(lower(trim(COALESCE(username,''))),'@','')=?", (name,)
        ).fetchone()[0] or 0)
        # No current user or exactly one current user with this username is safe.
        if users <= 1:
            out.append(name)
    return out


def get_user(tg_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with _connect(db_path) as db:
        row = db.execute("SELECT * FROM users WHERE tg_id=?", (int(tg_id),)).fetchone()
    return dict(row) if row else None


def resolve_user_for_cabinet(tg_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    user = get_user(tg_id, db_path)
    if user:
        return user
    # Cabinet authentication is local and fast; do not run a live 3x-ui scan here.
    return None


def resolve_token(token: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    tg_id = token_user_id(token)
    if tg_id <= 0:
        return None
    user = resolve_user_for_cabinet(tg_id, db_path)
    if not user:
        return None
    if verify_token(token, str(user.get("sub_id") or "")) != tg_id:
        return None
    return user


