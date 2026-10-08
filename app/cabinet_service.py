"""Canonical personal cabinet service.

Owns cabinet URL/token handling, user identity matching, personal cabinet identity and URL/token handling. HTTP routes are intentionally kept in webapp.py.
"""
from __future__ import annotations

import db as database_adapter

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, quote

import config

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
LEGACY_TOKEN_RE = re.compile(r"^(?P<tg_id>[1-9][0-9]{0,19})\.(?P<signature>[A-Za-z0-9_-]{43})$")


def _secret() -> bytes:
    value = str(getattr(config, "WEB_SECRET_KEY", "") or "").encode()
    if len(value) < 16:
        raise RuntimeError("WEB_SECRET_KEY слишком короткий")
    return value


def _sign(payload: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def make_token(tg_id: int, sub_id: str = "", db_path: str | Path | None = None) -> str:
    tg_id = int(tg_id)
    if tg_id <= 0:
        raise ValueError("Неверный Telegram ID")
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ttl = max(300, int(getattr(config, "CABINET_LINK_TTL_SECONDS", 86_400)))
    now = int(time.time())
    with _connect(db_path) as db:
        # Access tokens remain reusable for their full TTL. Only expired tokens
        # are purged; Telegram/webview clients may retry the same HTTPS URL.
        db.execute(
            "DELETE FROM cabinet_access_tokens WHERE expires_at<?",
            (now - 3600,),
        )
        db.execute(
            "INSERT INTO cabinet_access_tokens(token_hash,tg_id,expires_at) VALUES(?,?,?)",
            (token_hash, tg_id, now + ttl),
        )
        db.commit()
    return token


def token_user_id(token: str) -> int:
    m = LEGACY_TOKEN_RE.fullmatch(str(token or "").strip())
    return int(m.group("tg_id")) if m else 0


def verify_token(token: str, sub_id: str = "") -> int:
    if not bool(getattr(config, "CABINET_ALLOW_LEGACY_TOKENS", False)):
        return 0
    m = LEGACY_TOKEN_RE.fullmatch(str(token or "").strip())
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




APP_LAUNCH_TOKEN_RE = re.compile(r"^(?P<exp>[1-9][0-9]{9,12})\.(?P<tg_id>[1-9][0-9]{0,19})\.(?P<signature>[A-Za-z0-9_-]{43})$")

def make_app_launch_url(tg_id: int, app: str, sub_id: str = "", ttl: int = 600) -> str:
    """Create a short-lived HTTPS launcher for a native VPN client deep link.

    Telegram inline buttons only accept HTTP(S)/tg:// URLs, so native schemes
    are launched through a signed HTTPS endpoint rather than being placed in
    callback_data or directly into Bot API ``url``.
    """
    tg_id = int(tg_id)
    app = str(app or "").strip().lower()
    if tg_id <= 0 or app not in {"happ", "incy"}:
        raise ValueError("Неверные параметры app launcher")
    ttl = max(60, min(int(ttl), 1800))
    exp = int(time.time()) + ttl
    payload = f"app-launch-v1:{app}:{tg_id}:{exp}"
    signature = _sign(payload)
    base = public_web_base_url().rstrip("/")
    if not base:
        return ""
    return f"{base}/open-app/{app}?exp={exp}&tg_id={tg_id}&sig={quote(signature, safe='')}"

def verify_app_launch(tg_id: int, app: str, exp: int, signature: str) -> bool:
    """Verify a short-lived signed native-app launcher token."""
    try:
        tg_id = int(tg_id)
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    app = str(app or "").strip().lower()
    if tg_id <= 0 or app not in {"happ", "incy"} or exp < int(time.time()) or exp > int(time.time()) + 1800:
        return False
    expected = _sign(f"app-launch-v1:{app}:{tg_id}:{exp}")
    return bool(signature) and hmac.compare_digest(str(signature), expected)

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
    return f"{base}{path}?access={quote(make_token(tg_id, sub_id, config.DB_PATH), safe='')}"


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    conn = database_adapter.connect(str(db_path or config.DB_PATH), timeout=20)
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
    raw = str(token or "").strip()
    tg_id = 0
    if TOKEN_RE.fullmatch(raw):
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        with _connect(db_path) as db:
            # Token authentication must not depend on optional audit columns.
            # A valid token remains usable for its TTL; `consumed_at` is kept only
            # for compatibility with existing databases and is not part of auth.
            row = db.execute(
                "SELECT tg_id FROM cabinet_access_tokens WHERE token_hash=? AND expires_at>=?",
                (token_hash, int(time.time())),
            ).fetchone()
            tg_id = int(row[0] or 0) if row else 0
    elif bool(getattr(config, "CABINET_ALLOW_LEGACY_TOKENS", False)):
        tg_id = token_user_id(raw)
    if tg_id <= 0:
        return None
    user = resolve_user_for_cabinet(tg_id, db_path)
    if not user:
        return None
    if LEGACY_TOKEN_RE.fullmatch(raw) and verify_token(raw, str(user.get("sub_id") or "")) != tg_id:
        return None
    return user

