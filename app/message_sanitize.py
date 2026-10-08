"""Redaction helpers for the persistent Telegram message journal.

Only the minimum conversational text is retained. Known secret-bearing URL/query
patterns are replaced before anything reaches the database.
"""
from __future__ import annotations

import re

MAX_JOURNAL_TEXT = 8_000

_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+\-/]+=*\b", re.IGNORECASE)
_BASIC_RE = re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{12,}\b", re.IGNORECASE)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(?:token|key|secret|password|passwd|api[_-]?key|access[_-]?token|"
    r"auth(?:orization)?|sub(?:scription)?[_-]?(?:token|key))\s*([=:])\s*([^\s&<>'\"]+)"
)
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|key|secret|password|passwd|api[_-]?key|access[_-]?token|"
    r"auth(?:orization)?|sub(?:scription)?[_-]?(?:token|key))=)[^&#\s]+"
)
_SUB_URL_RE = re.compile(
    r"(?i)(https?://[^\s<>\"']+?/(?:sub|subscription|subscribe|link|access)(?:/|=))([^/?#\s<>\"']+)"
)
_VPN_URI_RE = re.compile(r"(?i)\b(?:vless|vmess|trojan|ss|socks5?|hysteria2?|tuic)://[^\s<>]+")


def redact_message_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    text = _BOT_TOKEN_RE.sub("<telegram-token>", text)
    text = _BEARER_RE.sub("Bearer <secret>", text)
    text = _BASIC_RE.sub("Basic <secret>", text)
    text = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(0)[:m.group(0).find(m.group(1))]}{m.group(1)}<secret>", text)
    text = _SECRET_QUERY_RE.sub(r"\1<secret>", text)
    text = _SUB_URL_RE.sub(r"\1<subscription-secret>", text)
    text = _VPN_URI_RE.sub("<subscription-link>", text)
    return text[:MAX_JOURNAL_TEXT]
