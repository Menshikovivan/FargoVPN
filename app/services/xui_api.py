"""3x-ui API access, normalization and live snapshot synchronization.

Traffic, expiry and last-online values from 3x-ui are authoritative. The application
uses PostgreSQL for its own data; legacy 3x-ui SQLite is accessed read-only for discovery when needed.
"""
from __future__ import annotations

import db as database_adapter

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import psycopg

import config
from time_utils import from_timestamp as panel_from_timestamp
import user_events

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_CACHE_REFRESH_LOCK = threading.Lock()
_INBOUND_REFRESH_LOCK = threading.Lock()
_INTERNAL_BASE_LOCK = threading.Lock()
_INTERNAL_BASE_CACHE: dict[str, Any] = {"value": "", "ts": 0.0, "error": ""}
_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "clients": [],
    "by_email": {},
    "by_tg_id": {},
    "online": set(),
    "traffic_summary": {},
    "server_traffic_summary": {},
    "duplicate_records": 0,
    "error": "Снимок 3x-ui ещё не загружен",
    "stale": True,
}
_LAST_SYNCED_LOCK = threading.Lock()
_LAST_SYNCED: dict[str, float] = {}
_HTTP_CLIENTS = threading.local()
_XUI_RATE_LOCK = threading.Lock()
_XUI_LAST_REQUEST_AT = 0.0
_XUI_REQUEST_GATE = threading.BoundedSemaphore(4)


def _http_client() -> httpx.Client:
    verify = _verify_tls()
    client = getattr(_HTTP_CLIENTS, "client", None)
    if client is None or getattr(_HTTP_CLIENTS, "verify", None) != verify:
        if client is not None:
            client.close()
        client = httpx.Client(verify=verify, trust_env=False)
        _HTTP_CLIENTS.client = client
        _HTTP_CLIENTS.verify = verify
        _HTTP_CLIENTS.session_signature = None
        _HTTP_CLIENTS.cookie_base = ""
    return client


def _normalize_internal_base_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    if not value:
        return ""
    # The 3x-ui API endpoints themselves start with /panel/api/... .
    # XUI_INTERNAL_BASE_URL must therefore point at the webBasePath root,
    # not at the UI's extra /panel route.
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    if path.lower().endswith("/panel"):
        path = path[:-len("/panel")].rstrip("/") or "/"
        value = parsed._replace(path=path).geturl().rstrip("/")
    return value

def _configured_internal_base_url() -> str:
    value = _normalize_internal_base_url(getattr(config, "XUI_INTERNAL_BASE_URL", ""))
    return value


def _xui_postgres_dsn() -> str:
    override = str(getattr(config, "XUI_POSTGRES_DSN", "") or "").strip()
    if override:
        return override
    env_path = Path(str(getattr(config, "XUI_DB_ENV_FILE", "/etc/default/x-ui"))).expanduser()
    if not env_path.is_file():
        return ""
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if line.startswith("XUI_DB_DSN="):
            value = line.split("=",1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value
    return ""


def _db_xui_internal_base_url() -> str:
    """Build a local 3x-ui API base URL from its settings database.

    Modern 3x-ui installations may use PostgreSQL. Prefer its configured
    PostgreSQL settings table and retain a read-only SQLite fallback for older
    installations. This function never writes to the 3x-ui database.
    """
    keys = ("webPort", "webBasePath", "webCertFile", "webKeyFile")
    values: dict[str, str] = {}
    dsn = _xui_postgres_dsn()
    if dsn:
        try:
            normalized = dsn
            if normalized.startswith("postgresql+psycopg://"):
                normalized = "postgresql://" + normalized[len("postgresql+psycopg://"):]
            with psycopg.connect(normalized, connect_timeout=2) as connection:
                rows = connection.execute(
                    "SELECT key,value FROM settings WHERE key IN ('webPort','webBasePath','webCertFile','webKeyFile')"
                ).fetchall()
            values = {str(k): str(v or "").strip() for k,v in rows}
        except Exception as exc:
            with _INTERNAL_BASE_LOCK:
                _INTERNAL_BASE_CACHE["error"] = str(exc)
    if not values:
        db_path = str(getattr(config, "XUI_DB_PATH", "/etc/x-ui/x-ui.db") or "/etc/x-ui/x-ui.db").strip()
        if db_path and Path(db_path).is_file():
            try:
                uri = f"file:{db_path}?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=1.0)
                try:
                    rows = connection.execute(
                        "SELECT key, value FROM settings WHERE key IN ('webPort','webBasePath','webCertFile','webKeyFile')"
                    ).fetchall()
                finally:
                    connection.close()
                values = {str(k): str(v or "").strip() for k, v in rows}
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                with _INTERNAL_BASE_LOCK:
                    _INTERNAL_BASE_CACHE["error"] = str(exc)
    if not values:
        return ""
    port = _int(values.get("webPort"), 2053)
    if not (1 <= port <= 65535):
        return ""
    base_path = values.get("webBasePath", "/").strip() or "/"
    if not base_path.startswith("/"):
        base_path = "/" + base_path
    base_path = base_path.rstrip("/")
    cert = values.get("webCertFile", "")
    key = values.get("webKeyFile", "")
    scheme = "https" if cert and key else "http"
    return _normalize_internal_base_url(f"{scheme}://127.0.0.1:{port}{base_path}")


def _base_url() -> str:
    now = time.time()
    configured = _configured_internal_base_url()
    if configured:
        return configured
    with _INTERNAL_BASE_LOCK:
        cached = str(_INTERNAL_BASE_CACHE.get("value") or "")
        cached_ts = float(_INTERNAL_BASE_CACHE.get("ts") or 0.0)
        if cached and now - cached_ts < 300:
            return cached
    if bool(getattr(config, "XUI_INTERNAL_AUTO_DETECT", True)):
        detected = _db_xui_internal_base_url()
        if detected:
            with _INTERNAL_BASE_LOCK:
                _INTERNAL_BASE_CACHE.update({"value": detected, "ts": now, "error": ""})
            logger.info("3x-ui API uses local endpoint: %s", detected)
            return detected
    return str(config.BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    token = str(getattr(config, "MASTER_API_TOKEN", "") or "").strip()
    if not token and not (getattr(config, "XUI_USERNAME", "") and getattr(config, "XUI_PASSWORD", "")):
        raise RuntimeError("Настройте API-токен 3x-ui либо логин и пароль для cookie-сессии")
    return {
        **({"Authorization": f"Bearer {token}"} if token else {}),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _verify_tls() -> bool:
    """Verify 3x-ui TLS by default; explicit False preserves legacy deployments."""
    return bool(getattr(config, "XUI_VERIFY_TLS", True))


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def _client_credential_id(client: dict[str, Any] | None) -> str:
    """Return the Xray credential ID, never the numeric database row ID."""
    if not isinstance(client, dict):
        return ""
    uuid_value = client.get("uuid")
    if isinstance(uuid_value, str) and uuid_value.strip():
        return uuid_value.strip()
    id_value = client.get("id")
    if isinstance(id_value, str) and id_value.strip():
        return id_value.strip()
    return ""


def _string_list(value: Any) -> list[str]:
    """Convert ClientRecord CSV/JSON storage into Client.allowedIPs []."""
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]


def _reverse_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        tag = str(value.get("tag") or "").strip()
        return {"tag": tag} if tag else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() == "null":
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    tag = str(parsed.get("tag") or "").strip()
    return {"tag": tag} if tag else None


def _editable_client_payload(
    client: dict[str, Any] | None,
    fallback_id: str = "",
) -> dict[str, Any]:
    """Convert clients/get ClientRecord JSON to the Client update model.

    3x-ui's ClientRecord uses an integer ``id`` database primary key and a
    string ``uuid`` credential. The update endpoint decodes ``Client.id`` as a
    string and performs a full-row replacement, so credentials and every
    protocol-specific field must be preserved with the correct JSON types.
    """
    source = dict(client) if isinstance(client, dict) else {}
    payload: dict[str, Any] = {}

    credential_id = _client_credential_id(source) or str(fallback_id or "").strip()
    if credential_id:
        payload["id"] = credential_id

    string_fields = (
        "email",
        "subId",
        "security",
        "password",
        "flow",
        "auth",
        "group",
        "comment",
        "privateKey",
        "publicKey",
        "preSharedKey",
        "secret",
        "adTag",
    )
    for key in string_fields:
        if key in source and source[key] is not None:
            payload[key] = str(source[key])

    for key in ("totalGB", "expiryTime", "limitIp", "tgId", "reset", "keepAlive"):
        if key in source:
            payload[key] = _int(source.get(key))

    if "enable" in source:
        payload["enable"] = _bool(source.get("enable"), True)

    if "allowedIPs" in source:
        # 3x-ui v3.8.x returns ClientRecord.allowedIPs as a single string,
        # but POST /panel/api/clients/update/{email} decodes model.Client,
        # whose AllowedIPs field is []string. Round-trip the value using the
        # exact JSON type expected by the update endpoint.
        value = source.get("allowedIPs")
        if value is not None:
            payload["allowedIPs"] = _string_list(value)

    reverse = _reverse_object(source.get("reverse"))
    if reverse is not None:
        payload["reverse"] = reverse

    created = source.get("createdAt") or source.get("created_at")
    updated = source.get("updatedAt") or source.get("updated_at")
    if _int(created) > 0:
        payload["createdAt"] = _int(created)
    if _int(updated) > 0:
        payload["updatedAt"] = _int(updated)

    # Fields added to the current 3x-ui client model after the original
    # FargoVPN integration. Preserve them so a full-row update cannot silently
    # reset subscription/traffic-reset settings on v3.8.x.
    for key in ("resetDay", "resetMax", "trafficResetDay", "limitHwid"):
        if key in source:
            payload[key] = _int(source.get(key))
    for key in ("trafficReset", "forwardedPorts"):
        if key in source and source[key] is not None:
            payload[key] = str(source[key])

    return payload


def bytes_to_gb(value: Any, digits: int = 2) -> float:
    """Convert a byte counter to GiB without failing on empty API values."""
    try:
        number = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        number = 0.0
    return round(number / (1024 ** 3), max(0, int(digits)))


def telegram_id_from_email(email: str) -> int:
    """Recover the legacy Telegram ID stored as the final ``_<digits>`` suffix."""
    match = re.search(r"_([1-9][0-9]{4,19})$", str(email or "").strip())
    if not match:
        return 0
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return 0


def normalize_timestamp_ms(value: Any) -> int:
    """Normalize Unix seconds/milliseconds to milliseconds."""
    timestamp = _int(value)
    if timestamp <= 0:
        return 0
    if timestamp < 10_000_000_000:
        return timestamp * 1000
    return timestamp


def normalize_client(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize current and legacy 3x-ui client list response shapes.

    Current 3x-ui keeps per-client counters in ``traffic`` on clients/list and
    also exposes them as ``clientStats`` on inbounds/list.  The latter is used
    as a fallback by the snapshot builder so a panel upgrade cannot silently
    make traffic disappear.
    """
    if not isinstance(raw, dict):
        return {}
    nested = raw.get("client") if isinstance(raw.get("client"), dict) else {}
    traffic = raw.get("traffic") if isinstance(raw.get("traffic"), dict) else {}
    # Some versions put traffic under client. Keep the explicit traffic object authoritative.
    nested_traffic = nested.get("traffic") if isinstance(nested.get("traffic"), dict) else {}
    traffic = {**nested_traffic, **traffic}
    client = {**nested, **raw}

    email = str(client.get("email") or traffic.get("email") or "").strip()
    up = _int(traffic.get("up", client.get("up", 0)))
    down = _int(traffic.get("down", client.get("down", 0)))
    total = _int(
        traffic.get(
            "total",
            traffic.get("totalGB", client.get("total", client.get("totalGB", 0))),
        )
    )
    expiry = normalize_timestamp_ms(
        traffic.get("expiryTime", client.get("expiryTime", nested.get("expiryTime", 0)))
    )
    last_online = normalize_timestamp_ms(
        traffic.get("lastOnline", client.get("lastOnline", nested.get("lastOnline", 0)))
    )
    traffic_enable = traffic.get("enable")
    enable_value = client.get("enable", nested.get("enable", True))
    enable = _bool(enable_value if traffic_enable is None else traffic_enable, True)
    uuid_value = _client_credential_id(raw) or _client_credential_id(nested) or _client_credential_id(client)

    return {
        "email": email,
        "uuid": str(uuid_value or ""),
        "sub_id": str(client.get("subId") or traffic.get("subId") or ""),
        "comment": str(client.get("comment") or "").strip(),
        "expiry_time": expiry,
        "enable": enable,
        "up": max(0, up),
        "down": max(0, down),
        "total": max(0, total),
        "last_online_ts": max(0, last_online),
        "online": False,
        "inbound_ids": client.get("inboundIds") if isinstance(client.get("inboundIds"), list) else [],
        "tg_id": _int(client.get("tgId", nested.get("tgId", 0))),
        "raw": raw,
    }


_SUB_SETTINGS_LOCK = threading.Lock()
_SUB_SETTINGS_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}


def _normalize_subscription_settings(value: Any) -> dict[str, Any]:
    """Keep only public subscription settings needed to build a client URL."""
    source = value if isinstance(value, dict) else {}
    keys = (
        "subEnable", "subURI", "subDomain", "webDomain", "subPort", "subPath",
        "subCertFile", "subKeyFile", "subJsonEnable", "subJsonURI", "subJsonPath",
    )
    result = {key: source.get(key) for key in keys if key in source}
    result["subEnable"] = _bool(source.get("subEnable"), True)
    for key in ("subPort",):
        result[key] = _int(source.get(key), 2096)
    return result


def fetch_subscription_settings_sync(force: bool = False) -> dict[str, Any]:
    """Read the live subscription settings from 3x-ui, with a short cache."""
    ttl = max(5, _int(getattr(config, "XUI_SUBSCRIPTION_SETTINGS_CACHE_SECONDS", 30), 30))
    now = time.time()
    with _SUB_SETTINGS_LOCK:
        cached = _SUB_SETTINGS_CACHE.get("data")
        if not force and isinstance(cached, dict) and now - float(_SUB_SETTINGS_CACHE.get("ts", 0.0)) < ttl:
            return dict(cached)
    payload = request_json_sync("POST", "panel/api/setting/defaultSettings", timeout=6.0)
    settings = _normalize_subscription_settings(payload.get("obj"))
    with _SUB_SETTINGS_LOCK:
        _SUB_SETTINGS_CACHE["ts"] = now
        _SUB_SETTINGS_CACHE["data"] = dict(settings)
    return settings


def subscription_url_from_settings(
    settings: dict[str, Any],
    sub_id: str,
    *,
    fallback_base_url: str = "",
) -> str:
    """Build the same subscription URL format used by current 3x-ui."""
    token = str(sub_id or "").strip()
    if not token:
        return ""
    sub_uri = str(settings.get("subURI") or "").strip()
    if sub_uri:
        return sub_uri.rstrip("/") + "/" + quote(token, safe="")
    host = str(settings.get("subDomain") or settings.get("webDomain") or "").strip()
    port = _int(settings.get("subPort"), 2096)
    sub_path = str(settings.get("subPath") or "/sub/").strip() or "/sub/"
    if not host and fallback_base_url:
        try:
            parsed = urlsplit(str(fallback_base_url).strip())
            host = parsed.hostname or ""
            if not port and parsed.port:
                port = int(parsed.port)
        except ValueError:
            host = ""
    if not host:
        return ""
    tls = bool(str(settings.get("subCertFile") or "").strip() and str(settings.get("subKeyFile") or "").strip())
    scheme = "https" if tls else "http"
    if (tls and port == 443) or ((not tls) and port == 80):
        authority = host
    else:
        authority = f"{host}:{port}"
    if not sub_path.startswith("/"):
        sub_path = "/" + sub_path
    if not sub_path.endswith("/"):
        sub_path += "/"
    return f"{scheme}://{authority}{sub_path}{quote(token, safe='')}"


def current_subscription_url_sync(
    sub_id: str,
    *,
    force_settings: bool = False,
    fallback_base_url: str = "",
) -> str:
    """Resolve one subscription URL from live 3x-ui settings.

    The configured FargoVPN URL is used only as a backward-compatible fallback
    when 3x-ui cannot be queried; successful live reads always win.
    """
    try:
        settings = fetch_subscription_settings_sync(force=force_settings)
        if not bool(settings.get("subEnable", True)):
            return ""
        return subscription_url_from_settings(settings, sub_id, fallback_base_url=fallback_base_url or str(getattr(config, "BASE_URL", "")))
    except Exception:
        base = str(fallback_base_url or getattr(config, "SUB_BASE_URL", "")).strip()
        if not base or not str(sub_id or "").strip():
            return ""
        return base.rstrip("/") + "/" + quote(str(sub_id).strip(), safe="")


def _request_url_candidates(path: str) -> list[str]:
    """Return ordered API URL candidates for mixed 3x-ui base-path deployments.

    Official 3x-ui API endpoints are rooted at /panel/api/... .  The local
    webBasePath may be stored as /prefix, while some reverse-proxy setups
    expose an additional /panel UI route.  Try the canonical route first and
    then conservative fallbacks only after a 404.
    """
    clean_path = "/" + str(path or "").lstrip("/")
    base = _base_url().rstrip("/")
    candidates: list[str] = [base + clean_path]

    # If a caller supplied panel/api but the base already ends with /panel,
    # _normalize_internal_base_url removes that suffix. Keep a defensive
    # candidate for legacy installations that really serve /api directly.
    if clean_path.lower().startswith("/panel/api/"):
        candidates.append(base + clean_path[len("/panel"):])
        if clean_path.startswith("/panel/api/server/"):
            candidates.append(base + clean_path[len("/panel/api"):])
        if clean_path in {"/panel/api/clients/onlines", "/panel/api/clients/lastOnline"}:
            candidates.append(base + clean_path.replace("/clients/", "/inbounds/"))

    # The administrator's URL is an independent fallback when the detected
    # local 3x-ui endpoint is stale. Strip the UI's /panel suffix before
    # appending /panel/api, including on custom webBasePath deployments.
    public = _normalize_internal_base_url(
        str(getattr(config, "BASE_URL", "") or getattr(config, "XUI_PANEL_URL", "") or "")
    )
    if public and public != base:
        candidates.append(public + clean_path)
        if clean_path.lower().startswith("/panel/api/"):
            candidates.append(public + clean_path[len("/panel"):])

    # Deduplicate while preserving order.
    result: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _client_list_sync() -> dict[str, Any]:
    try:
        return request_json_sync("GET", "panel/api/clients/list")
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404:
            raise
    inbound_data = request_json_sync("GET", "panel/api/inbounds/list")
    records = []
    for inbound in inbound_data.get("obj") or []:
        if not isinstance(inbound, dict):
            continue
        settings = inbound.get("settings") or {}
        if isinstance(settings, str):
            settings = json.loads(settings)
        stats = {str(x.get("email")): x for x in inbound.get("clientStats") or [] if isinstance(x, dict)}
        for client in settings.get("clients") or []:
            if isinstance(client, dict):
                record = {**stats.get(str(client.get("email")), {}), **client}
                record["uuid"] = client.get("id") or client.get("uuid") or ""
                record["inboundId"] = inbound.get("id")
                records.append(record)
    return {"success": True, "obj": records}


def _session_headers(client: httpx.Client, base: str, timeout: float) -> dict[str, str]:
    """Login at webBasePath, keep cookies per thread and per origin/base path."""
    signature = hashlib.sha256((base + str(getattr(config, "XUI_USERNAME", "")) +
                                str(getattr(config, "XUI_PASSWORD", ""))).encode()).hexdigest()
    if getattr(_HTTP_CLIENTS, "session_signature", None) != signature:
        client.cookies.clear()
        # Current panels require the pre-login session CSRF token too.
        csrf_before = client.get(base + "/csrf-token", timeout=timeout)
        login_headers = {}
        if csrf_before.status_code != 404:
            csrf_before.raise_for_status()
            csrf_data = csrf_before.json()
            obj = csrf_data.get("obj") if isinstance(csrf_data, dict) else None
            if isinstance(obj, str) and obj:
                login_headers["X-CSRF-Token"] = obj
        response = client.post(base + "/login", headers=login_headers, data={
            "username": str(getattr(config, "XUI_USERNAME", "")),
            "password": str(getattr(config, "XUI_PASSWORD", ""))}, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("3x-ui: вход по логину и паролю отклонён")
        _HTTP_CLIENTS.csrf = ""
        csrf = client.get(base + "/csrf-token", timeout=timeout)
        if csrf.status_code != 404:
            csrf.raise_for_status()
            data = csrf.json()
            obj = data.get("obj") or {} if isinstance(data, dict) else {}
            _HTTP_CLIENTS.csrf = str(data.get("csrfToken") or data.get("token") or
                                      (obj.get("token") if isinstance(obj, dict) else obj) or "")
        _HTTP_CLIENTS.session_signature = signature
        _HTTP_CLIENTS.cookie_base = base
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if getattr(_HTTP_CLIENTS, "csrf", ""):
        headers["X-CSRF-Token"] = _HTTP_CLIENTS.csrf
    return headers


def request_json_sync(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Perform an authenticated 3x-ui request with bounded concurrency and safe retries."""
    method = method.upper()
    configured_timeout = max(3.0, float(getattr(config, "XUI_REQUEST_TIMEOUT_SECONDS", 12.0)))
    timeout_value = max(3.0, min(float(timeout if timeout is not None else configured_timeout), 60.0))
    headers = _headers()
    candidates = _request_url_candidates(path)
    retry_methods = {"GET", "HEAD"}
    retry_statuses = {429, 502, 503, 504}
    retry_attempts = 3 if method in retry_methods else 1
    last_response = None
    last_error = None

    acquired = _XUI_REQUEST_GATE.acquire(timeout=min(10.0, timeout_value + 1.0))
    if not acquired:
        raise RuntimeError("3x-ui временно перегружена: превышен лимит параллельных запросов")
    try:
        for index, url in enumerate(candidates):
            for attempt in range(1, retry_attempts + 1):
                global _XUI_LAST_REQUEST_AT
                min_interval = max(0.0, float(getattr(config, "XUI_MIN_REQUEST_INTERVAL_MS", 100)) / 1000.0)
                with _XUI_RATE_LOCK:
                    now = time.monotonic()
                    wait = min_interval - (now - _XUI_LAST_REQUEST_AT)
                    if wait > 0:
                        time.sleep(min(wait, 1.0))
                    _XUI_LAST_REQUEST_AT = time.monotonic()
                started = time.monotonic()
                try:
                    client = _http_client()
                    base = url.split("/panel/api/", 1)[0] if "/panel/api/" in url else (url.split("/api/", 1)[0] if "/api/" in url else url.split("/server/", 1)[0])
                    if not str(getattr(config, "MASTER_API_TOKEN", "") or "").strip() or getattr(_HTTP_CLIENTS, "cookie_base", "") == base:
                        headers = _session_headers(client, base, timeout_value)
                    response = client.request(
                        method,
                        url,
                        headers=headers,
                        json=payload,
                        timeout=httpx.Timeout(timeout_value, connect=min(4.0, timeout_value)),
                    )
                    if response.status_code in (401, 403) and getattr(config, "XUI_USERNAME", "") and getattr(config, "XUI_PASSWORD", ""):
                        # Explicit auth rejection occurs before mutation; safe to authenticate once.
                        _HTTP_CLIENTS.session_signature = None
                        headers = _session_headers(client, base, timeout_value)
                        response = client.request(method, url, headers=headers, json=payload, timeout=timeout_value)
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    if elapsed_ms >= 1000:
                        logger.warning(
                            "performance operation=xui_http method=%s path=%s duration_ms=%s status=%s attempt=%s",
                            method, path, elapsed_ms, response.status_code, attempt,
                        )
                    last_response = response
                    if response.status_code == 404 and index < len(candidates) - 1:
                        logger.warning("3x-ui returned 404 for %s; trying API fallback", url)
                        break
                    if response.status_code in retry_statuses and attempt < retry_attempts:
                        retry_after = 0.0
                        try:
                            retry_after = float(response.headers.get("Retry-After", "0"))
                        except (TypeError, ValueError):
                            retry_after = 0.0
                        delay = max(0.25, min(retry_after, 3.0), 0.5 * (2 ** (attempt - 1)))
                        logger.warning(
                            "3x-ui transient status=%s method=%s path=%s retry_in=%.2fs attempt=%s/%s",
                            response.status_code, method, path, delay, attempt, retry_attempts,
                        )
                        time.sleep(delay)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise RuntimeError("3x-ui вернула некорректный JSON")
                    if not data.get("success"):
                        raise RuntimeError(str(data.get("msg") or "3x-ui отклонила запрос"))
                    logger.debug("3x-ui API endpoint selected: %s %s", method, candidates[min(index, len(candidates)-1)])
                    return data
                except httpx.HTTPStatusError as exc:
                    # Non-transient HTTP failures must not be retried; in particular,
                    # repeated 401/403 requests only add load while the configuration is wrong.
                    last_response = exc.response
                    if method not in retry_methods:
                        raise
                    if exc.response.status_code not in retry_statuses or index == len(candidates) - 1:
                        raise
                    last_error = exc
                    if attempt < retry_attempts:
                        delay = 0.5 * (2 ** (attempt - 1))
                        logger.warning(
                            "3x-ui HTTP status error method=%s path=%s status=%s retry_in=%.2fs attempt=%s/%s",
                            method, path, exc.response.status_code, delay, attempt, retry_attempts,
                        )
                        time.sleep(delay)
                        continue
                    if index == len(candidates) - 1:
                        raise
                    break
                except httpx.RequestError as exc:
                    last_error = exc
                    # A timed-out POST may already have changed the panel.
                    # Trying another URL would repeat add/extend/delete actions.
                    if method not in retry_methods:
                        raise
                    if attempt < retry_attempts:
                        delay = 0.5 * (2 ** (attempt - 1))
                        logger.warning(
                            "3x-ui transport error method=%s path=%s retry_in=%.2fs attempt=%s/%s: %s",
                            method, path, delay, attempt, retry_attempts, exc,
                        )
                        time.sleep(delay)
                        continue
                    if index == len(candidates) - 1:
                        raise
            # 404 fallback exhausted this candidate; next URL is attempted.
        if last_response is not None:
            last_response.raise_for_status()
        if last_error:
            raise last_error
        raise RuntimeError("Не удалось выбрать endpoint 3x-ui")
    finally:
        _XUI_REQUEST_GATE.release()


def _last_online_map(value: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    if isinstance(value, dict):
        for key, timestamp in value.items():
            if isinstance(timestamp, dict):
                email = str(timestamp.get("email") or key).strip().lower()
                ts = timestamp.get("lastOnline", timestamp.get("time", 0))
            else:
                email = str(key).strip().lower()
                ts = timestamp
            if email:
                result[email] = normalize_timestamp_ms(ts)
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or item.get("clientEmail") or "").strip().lower()
            if email:
                result[email] = normalize_timestamp_ms(item.get("lastOnline", item.get("time", 0)))
    return result


def _copy_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": float(snapshot.get("ts", 0)),
        "clients": [dict(item) for item in snapshot.get("clients", [])],
        "by_email": {key: dict(value) for key, value in snapshot.get("by_email", {}).items()},
        "by_tg_id": {int(key): dict(value) for key, value in snapshot.get("by_tg_id", {}).items()},
        "online": set(snapshot.get("online", set())),
        "traffic_summary": dict(snapshot.get("traffic_summary", {})),
        "server_traffic_summary": dict(snapshot.get("server_traffic_summary", {})),
        "duplicate_records": _int(snapshot.get("duplicate_records")),
        "error": str(snapshot.get("error", "")),
        "stale": bool(snapshot.get("stale", False)),
    }


def _client_identity_key(item: dict[str, Any]) -> str:
    # 3x-ui uses email as the stable client identity across several inbound
    # protocols. A UUID may be blank or protocol-specific, so keying on it
    # would still duplicate the same user in the dashboard.
    return str(item.get("email") or "").strip().lower()


def deduplicate_clients(clients: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse the same 3x-ui client returned from several inbounds.

    Some 3x-ui builds expose one traffic row per inbound. Those counters are
    often mirrors of the same client total, therefore summing them can inflate
    dashboard traffic many times over. We keep the maximum counter for each
    direction and retain all inbound IDs for diagnostics.
    """
    merged: dict[str, dict[str, Any]] = {}
    duplicate_records = 0
    for source in clients:
        if not isinstance(source, dict) or not source.get("email"):
            continue
        key = _client_identity_key(source)
        if key not in merged:
            merged[key] = dict(source)
            merged[key]["inbound_ids"] = list(source.get("inbound_ids") or [])
            continue
        duplicate_records += 1
        target = merged[key]
        for field in ("up", "down", "total", "last_online_ts", "expiry_time"):
            target[field] = max(_int(target.get(field)), _int(source.get(field)))
        target["enable"] = bool(target.get("enable")) or bool(source.get("enable"))
        target["online"] = bool(target.get("online")) or bool(source.get("online"))
        target_uuid = str(target.get("uuid") or "").strip()
        source_uuid = str(source.get("uuid") or "").strip()
        variants = set(str(value) for value in target.get("credential_variants", []) if value)
        if target_uuid:
            variants.add(target_uuid)
        if source_uuid:
            variants.add(source_uuid)
        if not target_uuid and source_uuid:
            target["uuid"] = source_uuid
        if variants:
            target["credential_variants"] = sorted(variants)
        if not target.get("sub_id") and source.get("sub_id"):
            target["sub_id"] = str(source["sub_id"])
        if _int(target.get("tg_id")) <= 0 and _int(source.get("tg_id")) > 0:
            target["tg_id"] = _int(source.get("tg_id"))
        inbound_ids = {
            _int(value)
            for value in list(target.get("inbound_ids") or []) + list(source.get("inbound_ids") or [])
            if _int(value) > 0
        }
        target["inbound_ids"] = sorted(inbound_ids)
    return list(merged.values()), duplicate_records


_SERVER_TRAFFIC_SAMPLE = {"ts": 0.0, "up": 0, "down": 0}
_SERVER_TRAFFIC_SAMPLE_LOCK = threading.Lock()

def _sample_network_rate(sent: int, received: int) -> tuple[float, float]:
    now = time.monotonic()
    with _SERVER_TRAFFIC_SAMPLE_LOCK:
        previous = dict(_SERVER_TRAFFIC_SAMPLE)
        _SERVER_TRAFFIC_SAMPLE.update({"ts": now, "up": sent, "down": received})
    elapsed = now - float(previous.get("ts") or 0.0)
    if elapsed < 0.5 or not previous.get("ts"):
        return 0.0, 0.0
    return (
        round(max(0.0, sent - _int(previous.get("up"))) / elapsed, 2),
        round(max(0.0, received - _int(previous.get("down"))) / elapsed, 2),
    )

def _traffic_counter(obj: dict[str, Any], *names: str) -> int:
    for name in names:
        if name in obj and obj.get(name) is not None:
            return max(0, _int(obj.get(name)))
    return 0


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default

def summarize_server_traffic(value: Any) -> dict[str, Any]:
    """Normalize current and legacy 3x-ui cumulative network counters.

    Different 3x-ui generations expose these as netTraffic.sent/recv or as
    netIO up/down (and some builds use tx/rx).  FargoVPN normalizes all of them
    and derives a short-window bytes/sec value for the monitoring page.
    """
    status = value if isinstance(value, dict) else {}
    counters = status.get("netTraffic") if isinstance(status.get("netTraffic"), dict) else {}
    netio = status.get("netIO") if isinstance(status.get("netIO"), dict) else {}
    source = counters or netio or status
    sent = _traffic_counter(source, "sent", "up", "tx", "netUp")
    received = _traffic_counter(source, "recv", "received", "down", "rx", "netDown")
    if not source or (sent == 0 and received == 0 and not any(k in source for k in ("sent","recv","up","down","tx","rx","netUp","netDown"))):
        return {}
    # Speed is sampled by _normalize_server_status(), the single authoritative
    # consumer for the monitoring card. Sampling here too would overwrite the
    # previous sample during the same refresh and make the displayed speed 0.
    return {
        "up": sent,
        "down": received,
        "used": sent + received,
        "up_speed": 0.0,
        "down_speed": 0.0,
        "source": "server-status",
    }


def summarize_inbound_traffic(value: Any) -> dict[str, Any]:
    inbounds = [item for item in (value if isinstance(value, list) else []) if isinstance(item, dict)]
    enabled = [item for item in inbounds if _bool(item.get("enable"), True)]

    def totals(rows: list[dict[str, Any]]) -> tuple[int, int]:
        up = down = 0
        for item in rows:
            # Current 3x-ui stores the per-client counters in clientStats while
            # inbound.up/down are the inbound aggregate. Prefer the aggregate,
            # but fall back to clientStats for older/newer response variants.
            item_up = _int(item.get("up"))
            item_down = _int(item.get("down"))
            stats = item.get("clientStats") if isinstance(item.get("clientStats"), list) else []
            if item_up == 0 and stats:
                item_up = sum(_int(stat.get("up")) for stat in stats if isinstance(stat, dict))
            if item_down == 0 and stats:
                item_down = sum(_int(stat.get("down")) for stat in stats if isinstance(stat, dict))
            up += max(0, item_up)
            down += max(0, item_down)
        return up, down

    up, down = totals(inbounds)
    enabled_up, enabled_down = totals(enabled)
    return {
        # The 3x-ui aggregate is based on inbound counters, including disabled
        # inbounds that still contain historical usage.
        "up": up,
        "down": down,
        "used": up + down,
        "enabled_up": enabled_up,
        "enabled_down": enabled_down,
        "enabled_used": enabled_up + enabled_down,
        "inbounds": len(inbounds),
        "enabled_inbounds": len(enabled),
        "source": "inbounds",
    }


_CONTROL_LOCK = threading.Lock()
_CONTROL_REFRESH_LOCK = threading.Lock()
_CONTROL_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "status": {},
    "fail2ban": {},
    "xray_metrics": {},
    "observatory": {},
    "nodes": [],
    "error": "",
}


def _copy_control_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": float(value.get("ts", 0.0)),
        "status": dict(value.get("status") or {}),
        "fail2ban": dict(value.get("fail2ban") or {}),
        "xray_metrics": dict(value.get("xray_metrics") or {}),
        "observatory": dict(value.get("observatory") or {}),
        "nodes": [dict(item) for item in value.get("nodes", []) if isinstance(item, dict)],
        "error": str(value.get("error") or ""),
    }


def _normalize_server_status(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    mem = raw.get("mem") if isinstance(raw.get("mem"), dict) else {}
    swap = raw.get("swap") if isinstance(raw.get("swap"), dict) else {}
    disk = raw.get("disk") if isinstance(raw.get("disk"), dict) else {}
    # Legacy status responses contain both netIO (instantaneous bytes/sec)
    # and netTraffic (cumulative bytes). Never differentiate an instant rate.
    counters = raw.get("netTraffic") if isinstance(raw.get("netTraffic"), dict) else {}
    netio = raw.get("netIO") if isinstance(raw.get("netIO"), dict) else {}
    net = counters or netio
    load = raw.get("load") if isinstance(raw.get("load"), dict) else {}
    xray = raw.get("xray") if isinstance(raw.get("xray"), dict) else {}
    net_up = _traffic_counter(net, "up", "sent", "tx", "netUp")
    net_down = _traffic_counter(net, "down", "recv", "received", "rx", "netDown")
    if counters and netio:
        net_up_speed = _finite_float(netio.get("up", netio.get("tx", 0)))
        net_down_speed = _finite_float(netio.get("down", netio.get("rx", 0)))
    elif net:
        net_up_speed, net_down_speed = _sample_network_rate(net_up, net_down)
    else:
        net_up_speed = net_down_speed = 0.0
    def provided(source: dict, keys: tuple[str, ...]) -> bool:
        for key in keys:
            try:
                if source.get(key) is not None and math.isfinite(float(source[key])):
                    return True
            except (TypeError, ValueError, OverflowError):
                continue
        return False
    return {
        "_available": {
            "cpu": provided(raw, ("cpu",)),
            "tcp_count": provided(raw, ("tcpCount",)),
            "network": bool(netio or counters),
        },
        "cpu": _finite_float(raw.get("cpu")),
        "mem_current": _int(mem.get("current")),
        "mem_total": _int(mem.get("total")),
        "swap_current": _int(swap.get("current")),
        "swap_total": _int(swap.get("total")),
        "disk_current": _int(disk.get("current")),
        "disk_total": _int(disk.get("total")),
        "net_up": net_up,
        "net_down": net_down,
        "net_up_speed": net_up_speed,
        "net_down_speed": net_down_speed,
        "tcp_count": _int(raw.get("tcpCount")),
        "load1": _finite_float(load.get("load1")),
        "load5": _finite_float(load.get("load5")),
        "load15": _finite_float(load.get("load15")),
        "xray_state": str(xray.get("state") or "unknown"),
        "xray_version": str(xray.get("version") or ""),
    }


def _normalize_node_summaries(value: Any) -> list[dict[str, Any]]:
    """Keep only safe read-only node summary fields from the 3x-ui API."""
    rows = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append({
            "guid": str(row.get("guid") or ""),
            "parent_guid": str(row.get("parentGuid") or ""),
            "name": str(row.get("name") or row.get("remark") or "Без имени"),
            "address": str(row.get("address") or ""),
            "status": str(row.get("status") or "unknown"),
            "version": str(row.get("panelVersion") or row.get("version") or row.get("xrayVersion") or ""),
            "enabled": _bool(row.get("enabled", row.get("enable", True)), True),
            "cpu_pct": _finite_float(row.get("cpuPct", row.get("cpu", 0))),
            "mem_pct": _finite_float(row.get("memPct")),
            "net_up": _int(row.get("netUp")),
            "net_down": _int(row.get("netDown")),
            "latency_ms": _int(row.get("latencyMs")),
        })
    return result


def fetch_control_snapshot_sync(force: bool = False) -> dict[str, Any]:
    """Fetch a small cached set of 3x-ui control telemetry.

    3x-ui already caches its machine snapshot. We therefore avoid polling every
    history metric on every dashboard refresh; longer histories are fetched only
    by dedicated monitoring features. Unsupported optional endpoints are ignored
    without inventing replacement values.
    """
    ttl = max(15, _int(getattr(config, "XUI_CONTROL_CACHE_SECONDS", 30), 30))
    now = time.time()
    with _CONTROL_LOCK:
        if not force and now - float(_CONTROL_CACHE.get("ts", 0)) < ttl:
            return _copy_control_snapshot(_CONTROL_CACHE)
    with _CONTROL_REFRESH_LOCK:
        now = time.time()
        with _CONTROL_LOCK:
            if not force and now - float(_CONTROL_CACHE.get("ts", 0)) < ttl:
                return _copy_control_snapshot(_CONTROL_CACHE)
        result = {"ts": now, "status": {}, "fail2ban": {}, "xray_metrics": {}, "observatory": {}, "nodes": [], "error": ""}
        errors: list[str] = []
        # The dashboard only needs the cheap, authoritative server status and node list.
        # Heavier telemetry endpoints are fetched on the dedicated monitoring view.
        # Keeping this snapshot sequential avoids bursts of concurrent requests into
        # a small 3x-ui instance, which can otherwise contend for CPU/SQLite locks.
        optional = (
            ("status", "GET", "panel/api/server/status"),
            ("nodes", "GET", "panel/api/nodes/list"),
            ("fail2ban", "GET", "panel/api/server/fail2banStatus"),
        )
        for key, method, path in optional:
            try:
                data = request_json_sync(method, path, timeout=5.0)
                obj = data.get("obj")
                if key == "status":
                    if not isinstance(obj, dict) or not obj:
                        raise RuntimeError("3x-ui вернула пустой server/status")
                    result[key] = _normalize_server_status(obj)
                elif key == "nodes":
                    result[key] = _normalize_node_summaries(obj)
                elif key == "fail2ban" and isinstance(obj, dict):
                    installed = _bool(obj.get("installed"))
                    enabled = _bool(obj.get("enabled"))
                    result[key] = {
                        "installed": installed,
                        "enabled": enabled,
                        "usable": _bool(obj.get("usable")),
                        "status": "enabled" if enabled else ("disabled" if installed else "not installed"),
                    }
            except Exception as exc:
                # Optional routes do not exist on older panels. A missing
                # node/Fail2ban route must not make healthy Xray look offline.
                if key == "status":
                    errors.append(f"{key}: {exc}")
                else:
                    logger.debug("3x-ui optional telemetry %s unavailable: %s", key, exc)
        result["error"] = "; ".join(errors[:3])
        with _CONTROL_LOCK:
            _CONTROL_CACHE.clear()
            _CONTROL_CACHE.update(result)
            return _copy_control_snapshot(_CONTROL_CACHE)


def fetch_client_extra_sync(email: str) -> dict[str, Any]:
    """Fetch optional per-client details from 3x-ui on demand.

    IP history is privacy-sensitive and relatively expensive, so it is never
    included in the global snapshot. The detail page may request it explicitly.
    """
    clean_email = str(email or "").strip()
    if not clean_email:
        return {"traffic": {}, "ips": [], "error": "Нет email 3x-ui"}
    result: dict[str, Any] = {"traffic": {}, "ips": [], "error": ""}
    errors: list[str] = []
    try:
        payload = request_json_sync("GET", f"panel/api/clients/traffic/{quote(clean_email, safe='')}", timeout=6.0)
        if isinstance(payload.get("obj"), dict):
            result["traffic"] = payload["obj"]
    except Exception as exc:
        errors.append(f"traffic: {exc}")
    try:
        payload = request_json_sync("POST", f"panel/api/clients/ips/{quote(clean_email, safe='')}", timeout=6.0)
        obj = payload.get("obj")
        if isinstance(obj, list):
            result["ips"] = [str(item) for item in obj if str(item).strip()][-20:]
    except Exception as exc:
        errors.append(f"ips: {exc}")
    result["error"] = "; ".join(errors)[:800]
    return result


def fetch_snapshot_sync(force: bool = False) -> dict[str, Any]:
    """Fetch and cache current clients, traffic, online status and last-seen data."""
    ttl = max(5, _int(getattr(config, "XUI_CACHE_SECONDS", 15), 15))
    now = time.time()
    with _CACHE_LOCK:
        if not force and now - float(_CACHE.get("ts", 0)) < ttl:
            return _copy_snapshot(_CACHE)

    # Single-flight refresh: several panel requests arriving together must not
    # all query 3x-ui concurrently. This is especially important on small CPUs.
    if not _CACHE_REFRESH_LOCK.acquire(blocking=force):
        with _CACHE_LOCK:
            cached = _copy_snapshot(_CACHE)
        cached["stale"] = True
        return cached
    try:
        now = time.time()
        with _CACHE_LOCK:
            if not force and now - float(_CACHE.get("ts", 0)) < ttl:
                return _copy_snapshot(_CACHE)

        refresh_started = time.monotonic()
        try:
            list_data = _client_list_sync()
            raw_clients = list_data.get("obj") if isinstance(list_data.get("obj"), list) else []
            clients = [normalize_client(item) for item in raw_clients if isinstance(item, dict)]
            clients = [item for item in clients if item.get("email")]
            # 3x-ui current builds expose authoritative per-client counters as
            # inbound.clientStats too. Merge them only when clients/list did not
            # provide usable traffic, keeping clients/list as the primary source.
            inbound_rows = None
            try:
                inbound_probe = request_json_sync("GET", "panel/api/inbounds/list")
                inbound_rows = inbound_probe.get("obj") if isinstance(inbound_probe.get("obj"), list) else []
                stats_by_email: dict[str, dict[str, Any]] = {}
                for inbound in inbound_rows:
                    if not isinstance(inbound, dict):
                        continue
                    for stat in inbound.get("clientStats") or []:
                        if not isinstance(stat, dict):
                            continue
                        email = str(stat.get("email") or "").strip().lower()
                        if not email:
                            continue
                        current = stats_by_email.setdefault(email, {})
                        current["up"] = max(_int(current.get("up")), _int(stat.get("up")))
                        current["down"] = max(_int(current.get("down")), _int(stat.get("down")))
                        current["total"] = max(_int(current.get("total")), _int(stat.get("total")))
                        current["expiryTime"] = max(_int(current.get("expiryTime")), _int(stat.get("expiryTime")))
                        current["lastOnline"] = max(_int(current.get("lastOnline")), _int(stat.get("lastOnline")))
                        if not current.get("uuid") and stat.get("uuid"):
                            current["uuid"] = stat.get("uuid")
                for item in clients:
                    fallback = stats_by_email.get(str(item.get("email") or "").strip().lower())
                    if not fallback:
                        continue
                    if _int(item.get("up")) == 0:
                        item["up"] = _int(fallback.get("up"))
                    if _int(item.get("down")) == 0:
                        item["down"] = _int(fallback.get("down"))
                    if _int(item.get("total")) == 0:
                        item["total"] = _int(fallback.get("total"))
                    if _int(item.get("expiry_time")) == 0:
                        item["expiry_time"] = normalize_timestamp_ms(fallback.get("expiryTime"))
                    if _int(item.get("last_online_ts")) == 0:
                        item["last_online_ts"] = normalize_timestamp_ms(fallback.get("lastOnline"))
                    if not item.get("uuid") and fallback.get("uuid"):
                        item["uuid"] = str(fallback.get("uuid"))
            except Exception as error:
                logger.debug("3x-ui clientStats fallback unavailable: %s", error)
            clients, duplicate_records = deduplicate_clients(clients)
    
            traffic_summary: dict[str, Any] = {}
            try:
                if inbound_rows is None:
                    inbound_data = request_json_sync("GET", "panel/api/inbounds/list")
                    inbound_rows = inbound_data.get("obj")
                traffic_summary = summarize_inbound_traffic(inbound_rows)
            except Exception as error:
                logger.debug("3x-ui inbound traffic unavailable: %s", error)
    
            server_traffic_summary: dict[str, Any] = {}
            try:
                server_data = request_json_sync("GET", "panel/api/server/status")
                server_traffic_summary = summarize_server_traffic(server_data.get("obj"))
            except Exception as error:
                # Older 3x-ui versions may not expose this endpoint through the API
                # token. The dashboard then falls back to inbound/client counters.
                logger.debug("3x-ui server traffic unavailable: %s", error)
    
            last_online: dict[str, int] = {}
            online: set[str] = set()
            try:
                last_data = request_json_sync("POST", "panel/api/clients/lastOnline")
                last_online = _last_online_map(last_data.get("obj"))
            except Exception as error:
                logger.debug("3x-ui lastOnline unavailable: %s", error)
            try:
                online_data = request_json_sync("POST", "panel/api/clients/onlines")
                if isinstance(online_data.get("obj"), list):
                    online = {
                        str(item.get("email") if isinstance(item, dict) else item).strip().lower()
                        for item in online_data["obj"]
                        if item
                    }
            except Exception as error:
                logger.debug("3x-ui onlines unavailable: %s", error)
    
            for item in clients:
                email_key = str(item["email"]).lower()
                if last_online.get(email_key, 0) > 0:
                    item["last_online_ts"] = last_online[email_key]
                item["online"] = email_key in online
    
            snapshot = {
                "ts": time.time(),
                "clients": clients,
                "by_email": {str(item["email"]).lower(): item for item in clients},
                # Keep this index strictly authoritative. Legacy email suffixes are
                # considered only by find_client_by_telegram_id_sync() for the one
                # concrete Telegram user currently being recovered.
                "by_tg_id": {
                    int(item.get("tg_id") or 0): item
                    for item in clients
                    if int(item.get("tg_id") or 0) > 0
                },
                "online": online,
                "traffic_summary": traffic_summary,
                "server_traffic_summary": server_traffic_summary,
                "duplicate_records": duplicate_records,
                "error": "",
                "stale": False,
            }
            with _CACHE_LOCK:
                _CACHE.clear()
                _CACHE.update(snapshot)
            refresh_ms = int((time.monotonic() - refresh_started) * 1000)
            if refresh_ms >= 1000:
                logger.warning(
                    "performance operation=xui_snapshot_refresh duration_ms=%s clients=%s",
                    refresh_ms, len(clients),
                )
            return _copy_snapshot(snapshot)
        except Exception as error:
            logger.warning("Не удалось получить свежие данные 3x-ui: %s", error)
            with _CACHE_LOCK:
                cached = _copy_snapshot(_CACHE)
            cached["error"] = str(error)
            cached["stale"] = True
            # Cache failures too; queued browser polls must not serialize
            # hundreds of full network timeouts during a 3x-ui outage.
            cached["ts"] = time.time()
            with _CACHE_LOCK:
                _CACHE.update(cached)
            return cached

    finally:
        _CACHE_REFRESH_LOCK.release()


def invalidate_snapshot_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["ts"] = 0.0


def snapshot_cache_status() -> dict[str, Any]:
    """Return the current in-memory 3x-ui snapshot without starting network I/O.

    This is intentionally read-only and is used by frequent health/status polling.
    A status endpoint must not turn a 10-second browser poll into a 3x-ui refresh
    plus a SQLite write transaction every time the cache expires.
    """
    with _CACHE_LOCK:
        snapshot = _copy_snapshot(_CACHE)
    ttl = max(5, _int(getattr(config, "XUI_CACHE_SECONDS", 15), 15))
    age = time.time() - float(snapshot.get("ts") or 0) if snapshot.get("ts") else float("inf")
    snapshot["cache_age_seconds"] = max(0.0, age) if age != float("inf") else None
    snapshot["cache_fresh"] = bool(snapshot.get("ts")) and age < ttl
    if not snapshot.get("ts"):
        snapshot["stale"] = True
    elif age >= ttl:
        snapshot["stale"] = True
    return snapshot


def _stable_negative_id(email: str) -> int:
    digest = hashlib.sha256(email.lower().encode("utf-8")).digest()
    return -(int.from_bytes(digest[:7], "big") % 9_000_000_000 + 1)


def _ensure_user_columns(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(users)")}
    additions = {
        "last_online_ts": "INTEGER DEFAULT 0",
        "last_sync_at": "TEXT",
        "identity_source": "TEXT",
        "identity_updated_at": "TEXT",
        "notes": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")


def _rekey_related_rows(connection: sqlite3.Connection, old_tg_id: int, new_tg_id: int) -> None:
    """Move user-linked records when a restored placeholder ID is repaired.

    The old implementation only moved payments/messages/events. A restore can
    also leave referral, push, access-token and pending-registration rows pinned
    to the placeholder, which later makes the account look split in the panel.
    """
    old_tg_id = int(old_tg_id)
    new_tg_id = int(new_tg_id)
    if old_tg_id == new_tg_id:
        return

    def has_table(name: str) -> bool:
        return bool(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone())

    def has_column(name: str, column: str) -> bool:
        return column in {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({name})")
        }

    # These tables carry the same Telegram identity without a uniqueness
    # constraint on tg_id, so moving them is lossless.
    for table in (
        "payments", "message_log", "user_events", "push_subscriptions",
        "pending_registrations", "cabinet_access_tokens",
    ):
        if has_table(table) and has_column(table, "tg_id"):
            connection.execute(
                f"UPDATE {table} SET tg_id=? WHERE tg_id=?",
                (new_tg_id, old_tg_id),
            )

    if has_table("access_invites") and has_column("access_invites", "owner_id"):
        # owner_id is unique. If the real account already has an invite, keep
        # the existing row and retire the stale placeholder reference.
        target_exists = connection.execute(
            "SELECT 1 FROM access_invites WHERE owner_id=?", (new_tg_id,)
        ).fetchone()
        if target_exists:
            connection.execute("DELETE FROM access_invites WHERE owner_id=?", (old_tg_id,))
        else:
            connection.execute(
                "UPDATE access_invites SET owner_id=? WHERE owner_id=?",
                (new_tg_id, old_tg_id),
            )

    if has_table("access_attempts") and has_column("access_attempts", "tg_id"):
        target_exists = connection.execute(
            "SELECT 1 FROM access_attempts WHERE tg_id=?", (new_tg_id,)
        ).fetchone()
        if target_exists:
            connection.execute("DELETE FROM access_attempts WHERE tg_id=?", (old_tg_id,))
        else:
            connection.execute(
                "UPDATE access_attempts SET tg_id=? WHERE tg_id=?",
                (new_tg_id, old_tg_id),
            )

    if has_table("referral_rewards"):
        if has_column("referral_rewards", "referrer_tg_id"):
            connection.execute(
                "UPDATE referral_rewards SET referrer_tg_id=? WHERE referrer_tg_id=?",
                (new_tg_id, old_tg_id),
            )
        if has_column("referral_rewards", "referred_tg_id"):
            # referred_tg_id is UNIQUE. If a target row already exists, it is the
            # surviving account's ledger entry; discard only the stale duplicate.
            target_exists = connection.execute(
                "SELECT 1 FROM referral_rewards WHERE referred_tg_id=?", (new_tg_id,)
            ).fetchone()
            if target_exists:
                connection.execute("DELETE FROM referral_rewards WHERE referred_tg_id=?", (old_tg_id,))
            else:
                connection.execute(
                    "UPDATE referral_rewards SET referred_tg_id=? WHERE referred_tg_id=?",
                    (new_tg_id, old_tg_id),
                )

    if has_table("telegram_link_requests"):
        if has_column("telegram_link_requests", "local_tg_id"):
            connection.execute(
                "UPDATE telegram_link_requests SET local_tg_id=? WHERE local_tg_id=?",
                (new_tg_id, old_tg_id),
            )
        if has_column("telegram_link_requests", "resolved_tg_id"):
            connection.execute(
                "UPDATE telegram_link_requests SET resolved_tg_id=? WHERE resolved_tg_id=?",
                (new_tg_id, old_tg_id),
            )

    # user_events.rekey_message_state handles the primary-key state table and
    # its own collision-safe merge logic.
    user_events.rekey_message_state(connection, old_tg_id, new_tg_id)

    # Referral ownership stored on the users table is another foreign-key-like
    # reference that must follow the repaired identity.
    if has_table("users") and has_column("users", "referred_by_tg_id"):
        connection.execute(
            "UPDATE users SET referred_by_tg_id=? WHERE referred_by_tg_id=?",
            (new_tg_id, old_tg_id),
        )


def sync_snapshot_to_db(snapshot: dict[str, Any], db_path: str | Path | None = None) -> int:
    """Persist a live snapshot and repair legacy rows whose TG ID was lost.

    A positive ``tgId`` from 3x-ui is authoritative. Numeric email suffixes are
    deliberately *not* promoted here: old manual clients used Unix timestamps in
    the same position. The suffix is considered only when a concrete Telegram ID
    is being recovered by the bot/payment flow. Username alone is never identity.
    """
    clients = snapshot.get("clients") if isinstance(snapshot, dict) else []
    if not isinstance(clients, list):
        return 0
    target = str(db_path or config.DB_PATH)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = 0

    connection = database_adapter.connect(target, timeout=20)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        _ensure_user_columns(connection)
        rows = connection.execute(
            "SELECT tg_id,username,uuid,email,expiry_time,enable,last_reminder_days,notes FROM users"
        ).fetchall()
        by_email = {str(row["email"] or "").strip().lower(): row for row in rows if row["email"]}
        by_uuid = {str(row["uuid"] or "").strip().lower(): row for row in rows if row["uuid"]}
        matched_ids: set[int] = set()

        for item in clients:
            if not isinstance(item, dict) or not item.get("email"):
                continue
            email = str(item["email"]).strip()
            email_key = email.lower()
            uuid_key = str(item.get("uuid") or "").strip().lower()
            source_row = by_email.get(email_key) or (by_uuid.get(uuid_key) if uuid_key else None)
            row = dict(source_row) if source_row else None
            panel_tg_id = _int(item.get("tg_id"))
            inferred_tg_id = panel_tg_id if panel_tg_id > 0 else 0

            if row:
                current_tg_id = int(row["tg_id"])
                if current_tg_id <= 0 and inferred_tg_id > 0:
                    existing = connection.execute(
                        "SELECT tg_id,username,uuid,email,expiry_time,last_reminder_days FROM users WHERE tg_id=?",
                        (inferred_tg_id,),
                    ).fetchone()
                    if existing:
                        existing_dict = dict(existing)
                        if not existing_dict.get("username") and row.get("username"):
                            connection.execute(
                                "UPDATE users SET username=? WHERE tg_id=?",
                                (row.get("username"), inferred_tg_id),
                            )
                            existing_dict["username"] = row.get("username")
                        _rekey_related_rows(connection, current_tg_id, inferred_tg_id)
                        connection.execute("DELETE FROM users WHERE tg_id=?", (current_tg_id,))
                        row = existing_dict
                    else:
                        _rekey_related_rows(connection, current_tg_id, inferred_tg_id)
                        connection.execute(
                            "UPDATE users SET tg_id=? WHERE tg_id=?",
                            (inferred_tg_id, current_tg_id),
                        )
                        row["tg_id"] = inferred_tg_id
                    tg_id = inferred_tg_id
                    changed += 1
                else:
                    tg_id = current_tg_id
                    if panel_tg_id > 0 and current_tg_id > 0 and panel_tg_id != current_tg_id:
                        logger.warning(
                            "Конфликт TG ID для %s: локально %s, в 3x-ui %s; сохранён локальный ID",
                            email, current_tg_id, panel_tg_id,
                        )
            else:
                tg_id = inferred_tg_id or _stable_negative_id(email)

            if tg_id in matched_ids:
                logger.warning("Повторяющийся TG ID %s у клиента %s; запись оставлена без Telegram-привязки", tg_id, email)
                tg_id = _stable_negative_id(email)
            matched_ids.add(tg_id)

            username = str((row or {}).get("username") or "") or (email.rsplit("_", 1)[0] or "Веб-клиент")
            panel_comment = str(item.get("comment") or "").strip()[:255]
            last_online_ts = _int(item.get("last_online_ts"))
            last_online = (
                panel_from_timestamp(last_online_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
                if last_online_ts > 0
                else None
            )
            new_uuid = str(item.get("uuid") or "")
            new_sub_id = str(item.get("sub_id") or "")
            new_expiry = _int(item.get("expiry_time"))
            new_enable = int(bool(item.get("enable")))
            new_up = _int(item.get("up"))
            new_down = _int(item.get("down"))
            new_total = _int(item.get("total"))
            old = row or {}
            old_last_online_ts = _int(old.get("last_online_ts")) if row else 0
            merged_last_online_ts = max(last_online_ts, old_last_online_ts)
            username_changed = bool(row is None and username) or (row is not None and not str(old.get("username") or "") and bool(username))
            data_changed = (
                row is None
                or username_changed
                or str(old.get("uuid") or "") != new_uuid
                or str(old.get("email") or "") != email
                or _int(old.get("expiry_time")) != new_expiry
                or _int(old.get("enable"), 1) != new_enable
                or _int(old.get("up")) != new_up
                or _int(old.get("down")) != new_down
                or _int(old.get("total")) != new_total
                or str(old.get("sub_id") or "") != new_sub_id
                or old_last_online_ts != merged_last_online_ts
                or (bool(panel_comment) and str(old.get("notes") or "") != panel_comment)
            )
            if data_changed:
                connection.execute(
                    """
                    INSERT INTO users(
                        tg_id,username,uuid,email,expiry_time,enable,up,down,total,sub_id,notes,
                        last_online,last_online_ts,last_sync_at,last_reminder_days
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,-1)
                    ON CONFLICT(tg_id) DO UPDATE SET
                        username=COALESCE(NULLIF(users.username,''),excluded.username),
                        uuid=excluded.uuid,email=excluded.email,
                        last_reminder_days=CASE
                            WHEN COALESCE(users.expiry_time,0) <> COALESCE(excluded.expiry_time,0) THEN -1
                            ELSE users.last_reminder_days
                        END,
                        expiry_time=excluded.expiry_time,
                        enable=excluded.enable,up=excluded.up,down=excluded.down,total=excluded.total,
                        sub_id=excluded.sub_id,
                        notes=COALESCE(NULLIF(excluded.notes,''),users.notes),
                        last_online=COALESCE(excluded.last_online,users.last_online),
                        last_online_ts=GREATEST(COALESCE(excluded.last_online_ts,0),COALESCE(users.last_online_ts,0)),
                        last_sync_at=excluded.last_sync_at
                    """,
                    (
                        tg_id, username, new_uuid, email, new_expiry, new_enable,
                        new_up, new_down, new_total, new_sub_id, panel_comment, last_online,
                        last_online_ts, now_text,
                    ),
                )
                changed += 1
            if panel_tg_id > 0 and (row is None or str(old.get("identity_source") or "") != "3x-ui"):
                connection.execute(
                    "UPDATE users SET identity_source='3x-ui',identity_updated_at=? WHERE tg_id=?",
                    (now_text, tg_id),
                )
                changed += 1

        # A successful full list is authoritative. Keep metadata, but disable local
        # rows that no longer exist in 3x-ui so reminders cannot target them.
        for source_row in rows:
            tg_id = int(source_row["tg_id"])
            if source_row["email"] and tg_id not in matched_ids and _int(source_row["enable"], 1) != 0:
                connection.execute(
                    "UPDATE users SET enable=0,last_sync_at=? WHERE tg_id=?",
                    (now_text, tg_id),
                )
                changed += 1
        connection.commit()
    finally:
        connection.close()
    return changed


def fetch_and_sync(force: bool = False, db_path: str | Path | None = None) -> dict[str, Any]:
    """Return a live snapshot and persist each fresh snapshot at most once.

    Dashboard routes share the same in-memory 3x-ui cache. Without this guard,
    every page view rewrote all users to SQLite even when the cached snapshot
    had not changed.
    """
    snapshot = fetch_snapshot_sync(force=force)
    if snapshot.get("stale"):
        return snapshot
    target = str(Path(db_path or config.DB_PATH).resolve())
    snapshot_ts = float(snapshot.get("ts") or 0)
    should_sync = False
    with _LAST_SYNCED_LOCK:
        if snapshot_ts > 0 and _LAST_SYNCED.get(target) != snapshot_ts:
            _LAST_SYNCED[target] = snapshot_ts
            should_sync = True
    if should_sync:
        try:
            sync_snapshot_to_db(snapshot, db_path=target)
        except Exception:
            with _LAST_SYNCED_LOCK:
                if _LAST_SYNCED.get(target) == snapshot_ts:
                    _LAST_SYNCED.pop(target, None)
            raise
    return snapshot


def fetch_inbounds_sync(force: bool = False) -> list[dict[str, Any]]:
    """Return enabled 3x-ui inbounds with a small process-local cache."""
    ttl = max(5, int(getattr(config, "XUI_INBOUND_CACHE_SECONDS", 60)))
    now = time.time()
    with _CACHE_LOCK:
        cache = _CACHE.get("inbounds_cache")
        if isinstance(cache, dict) and not force and now - float(cache.get("ts", 0)) < ttl:
            return [dict(item) for item in cache.get("items", []) if isinstance(item, dict)]
    if not _INBOUND_REFRESH_LOCK.acquire(blocking=False):
        with _CACHE_LOCK:
            cache = _CACHE.get("inbounds_cache")
            if isinstance(cache, dict):
                return [dict(item) for item in cache.get("items", []) if isinstance(item, dict)]
        raise RuntimeError("Список inbound 3x-ui уже обновляется другим запросом")
    try:
        now = time.time()
        with _CACHE_LOCK:
            cache = _CACHE.get("inbounds_cache")
            if isinstance(cache, dict) and not force and now - float(cache.get("ts", 0)) < ttl:
                return [dict(item) for item in cache.get("items", []) if isinstance(item, dict)]
        data = request_json_sync("GET", "panel/api/inbounds/list")
        rows: list[dict[str, Any]] = []
        for item in data.get("obj", []) if isinstance(data.get("obj"), list) else []:
            if not isinstance(item, dict):
                continue
            inbound_id = _int(item.get("id"))
            if inbound_id <= 0 or not _bool(item.get("enable"), True):
                continue
            rows.append({
                "id": inbound_id,
                "remark": str(item.get("remark") or item.get("tag") or f"Inbound {inbound_id}").strip()[:200],
                "protocol": str(item.get("protocol") or "unknown").strip().lower()[:32],
                "port": _int(item.get("port")),
                "enable": True,
            })
        rows.sort(key=lambda x: (str(x.get("remark") or "").lower(), int(x.get("id") or 0)))
        with _CACHE_LOCK:
            _CACHE["inbounds_cache"] = {"ts": now, "items": [dict(item) for item in rows]}
        return rows
    finally:
        _INBOUND_REFRESH_LOCK.release()


def inbound_selection_status_sync(force: bool = False) -> dict[str, Any]:
    """Return current enabled inbounds and which ones are selected for new clients."""
    configured = sorted({_int(value) for value in getattr(config, "XUI_MANAGED_INBOUND_IDS", []) if _int(value) > 0})
    try:
        rows = fetch_inbounds_sync(force=force)
        available_ids = {int(item["id"]) for item in rows}
        selected = sorted(set(configured) & available_ids) if configured else sorted(available_ids)
        missing = sorted(set(configured) - available_ids)
        return {"ok": True, "items": rows, "selected": selected, "missing": missing, "configured": bool(configured), "error": ""}
    except Exception as exc:
        return {"ok": False, "items": [], "selected": configured, "missing": [], "configured": bool(configured), "error": str(exc)}


def inbound_ids_sync() -> list[int]:
    """Return enabled inbound IDs, honoring the configured allowlist for new clients."""
    rows = fetch_inbounds_sync(force=False)
    eligible = {int(item["id"]) for item in rows}
    configured = {_int(value) for value in getattr(config, "XUI_MANAGED_INBOUND_IDS", []) if _int(value) > 0}
    if configured:
        valid = sorted(configured & eligible)
        missing = sorted(configured - eligible)
        if not valid:
            detail = "; отсутствуют/отключены: " + ", ".join(map(str, missing)) if missing else ""
            raise RuntimeError("Все настроенные inbound 3x-ui недоступны" + detail)
        if missing:
            logger.warning("Часть настроенных inbound 3x-ui недоступна: %s", ", ".join(map(str, missing)))
        return valid
    result = sorted(eligible)
    if not result:
        raise RuntimeError("Нет активных inbound 3x-ui для выдачи клиентов")
    return result


def ensure_client_inbounds_sync(
    email: str,
    inbound_ids: list[int],
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify that a client is attached to every requested inbound and repair gaps."""
    desired = sorted({_int(value) for value in inbound_ids if _int(value) > 0})
    if not desired:
        raise RuntimeError("Не передан ни один inbound 3x-ui для клиента")
    current_record = record or get_client_record_sync(email)
    current_ids = sorted({_int(value) for value in current_record.get("inbound_ids", []) if _int(value) > 0})
    missing = sorted(set(desired) - set(current_ids))
    if missing:
        request_json_sync(
            "POST",
            f"panel/api/clients/{quote(str(email).strip(), safe='')}/attach",
            {"inboundIds": missing},
        )
        invalidate_snapshot_cache()
        current_record = get_client_record_sync(email)
        current_ids = sorted({_int(value) for value in current_record.get("inbound_ids", []) if _int(value) > 0})
    missing = sorted(set(desired) - set(current_ids))
    if missing:
        raise RuntimeError(
            "3x-ui не привязала клиента ко всем inbound; отсутствуют: "
            + ", ".join(map(str, missing))
        )
    current_record["inbound_ids"] = current_ids
    return current_record


def add_client_sync(client: dict[str, Any], inbound_ids: list[int] | None = None) -> dict[str, Any]:
    # FargoVPN-created clients must not inherit the panel's XTLS Vision default.
    # This is a create-only normalization; update_client_sync preserves existing
    # client flow values and therefore does not rewrite deployed subscriptions.
    new_client = dict(client)
    new_client["flow"] = ""
    target_inbounds = list(inbound_ids if inbound_ids is not None else inbound_ids_sync())
    result = request_json_sync(
        "POST",
        "panel/api/clients/add",
        {"client": new_client, "inboundIds": target_inbounds},
    )
    email = str(new_client.get("email") or "").strip()
    if not email:
        raise RuntimeError("Не указан email нового клиента 3x-ui")
    try:
        record = get_client_record_sync(email)
        ensure_client_inbounds_sync(email, target_inbounds, record=record)
    except Exception as verify_error:
        raise RuntimeError(
            f"Клиент создан, но не удалось подтвердить все inbound 3x-ui: {verify_error}"
        ) from verify_error
    invalidate_snapshot_cache()
    return result


def get_client_record_sync(email: str) -> dict[str, Any]:
    """Return the raw ClientRecord, typed update payload and attachments."""
    clean_email = str(email or "").strip()
    if not clean_email:
        raise ValueError("Email клиента не указан")
    data = request_json_sync("GET", f"panel/api/clients/get/{quote(clean_email, safe='')}")
    obj = data.get("obj")
    if not isinstance(obj, dict):
        raise RuntimeError(f"Клиент {clean_email} не найден в 3x-ui")

    nested = obj.get("client") if isinstance(obj.get("client"), dict) else obj
    raw_client = dict(nested)
    if not raw_client.get("email"):
        raw_client["email"] = clean_email

    inbound_ids = obj.get("inboundIds")
    if not isinstance(inbound_ids, list):
        inbound_ids = raw_client.get("inboundIds") if isinstance(raw_client.get("inboundIds"), list) else []

    normalized = normalize_client({"client": raw_client, "inboundIds": inbound_ids})
    editable = _editable_client_payload(raw_client, fallback_id=str(normalized.get("uuid") or ""))
    return {
        "client": raw_client,
        "editable_client": editable,
        "inbound_ids": [_int(value) for value in inbound_ids],
        "normalized": normalized,
        "raw": obj,
    }


def update_client_sync(
    email: str,
    changes: dict[str, Any],
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace a 3x-ui client row while preserving every editable field."""
    record = record or get_client_record_sync(email)
    fallback_id = str(record.get("normalized", {}).get("uuid") or "")
    base = record.get("editable_client")
    client = dict(base) if isinstance(base, dict) else _editable_client_payload(record.get("client"), fallback_id)
    client.update(changes)
    client = _editable_client_payload(client, fallback_id=fallback_id)
    client["email"] = str(client.get("email") or email).strip()

    defaults = {
        "comment": "VPN Service Platform",
        "enable": True,
        "expiryTime": 0,
        "limitIp": 0,
        "reset": 0,
        "security": "auto",
        "subId": "",
        "tgId": 0,
        "totalGB": 0,
    }
    for key, value in defaults.items():
        client.setdefault(key, value)

    result = request_json_sync(
        "POST",
        f"panel/api/clients/update/{quote(str(email), safe='')}",
        client,
    )
    invalidate_snapshot_cache()
    return result


def extend_client_sync(
    email: str,
    days: int,
    tg_id: int | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Extend from the later of now/current expiry and re-enable the client.

    Unlike ``bulkAdjust``, this also converts legacy unlimited/zero expiry into a
    real paid period and can repair the client's Telegram ID in the same update.
    """
    days = int(days)
    if days <= 0:
        raise ValueError("Количество дней для продления должно быть положительным")
    record = get_client_record_sync(email)
    client = record["client"]
    current = normalize_timestamp_ms(client.get("expiryTime", record["normalized"].get("expiry_time", 0)))
    now_ms = int(time.time() * 1000)
    expiry = max(now_ms, current if current > 0 else now_ms) + days * 86_400_000
    changes: dict[str, Any] = {"expiryTime": expiry, "enable": True}
    if tg_id is not None and int(tg_id) > 0:
        changes["tgId"] = int(tg_id)
    clean_display_name = str(display_name or "").strip().lstrip("@")[:80]
    if clean_display_name:
        # Keep the human name in 3x-ui even when the technical email key must
        # be ASCII-only or include a Telegram-ID suffix.
        changes["comment"] = clean_display_name
    client_key = str(changes.get("email") or email).strip()
    last_error: Exception | None = None

    # Updating a client by email is a full-row replacement in 3x-ui.  The
    # payment path must be idempotent: every retry sends the same absolute
    # expiryTime, never "another +N days".  This prevents a successful POST
    # followed by a transient GET failure from double-extending the client.
    for cycle in range(2):
        try:
            update_client_sync(client_key, changes, record=record if cycle == 0 else None)
        except Exception as error:
            last_error = error
            logger.warning(
                "Не удалось применить продление клиента %s (цикл %s/2): %s",
                client_key, cycle + 1, error,
            )
            if cycle == 0:
                time.sleep(0.35)
                record = None
                continue
            raise RuntimeError(
                f"Не удалось обновить срок клиента {client_key} в 3x-ui после 2 попыток: {error}"
            ) from error

        for attempt in range(3):
            try:
                updated = get_client_record_sync(client_key)
                normalized = dict(updated["normalized"])
                actual_client = updated.get("client") if isinstance(updated.get("client"), dict) else {}
                actual_expiry = normalize_timestamp_ms(
                    actual_client.get("expiryTime", normalized.get("expiry_time", 0))
                )
                actual_enable = _bool(actual_client.get("enable", normalized.get("enable", False)), False)

                # Accept a later expiry as success (for example, another
                # legitimate renewal raced us).  A value below the requested
                # target is never accepted as a successful payment sync.
                if actual_expiry >= expiry and actual_enable:
                    normalized["expiry_time"] = actual_expiry
                    normalized["enable"] = True
                    if tg_id is not None and int(tg_id) > 0:
                        normalized["tg_id"] = int(tg_id)
                    logger.info(
                        "3x-ui renewal verified: email=%s expected_expiry=%s actual_expiry=%s attempt=%s cycle=%s",
                        client_key, expiry, actual_expiry, attempt + 1, cycle + 1,
                    )
                    return normalized

                last_error = RuntimeError(
                    f"3x-ui не подтвердила продление {client_key}: "
                    f"ожидалось expiryTime>={expiry}, получено {actual_expiry}; "
                    f"enable={actual_enable}"
                )
                logger.warning("%s", last_error)
            except Exception as error:
                last_error = error
                logger.warning(
                    "Не удалось проверить продление клиента %s (попытка %s/3, цикл %s/2): %s",
                    client_key, attempt + 1, cycle + 1, error,
                )
            if attempt < 2:
                time.sleep(0.35)

        # Repeat the exact same absolute update once.  This is safe even when
        # the first POST actually succeeded: we never add days a second time.
        record = None

    raise RuntimeError(
        f"Продление клиента {client_key} не подтверждено 3x-ui после 2 циклов "
        f"обновления: {last_error}"
    )


def change_client_days_sync(
    email: str,
    days_delta: int,
    tg_id: int | None = None,
) -> dict[str, Any]:
    """Add or subtract days without converting an expired term to unlimited.

    Positive values behave like a renewal and start from the later of now or
    the current expiry. Negative values subtract from the exact current expiry.
    If subtraction moves the date into the past, the client is disabled and a
    small positive timestamp is retained because expiryTime=0 means unlimited
    in 3x-ui.
    """
    days_delta = int(days_delta)
    if days_delta == 0:
        raise ValueError("Изменение количества дней не может быть равно нулю")
    record = get_client_record_sync(email)
    normalized = dict(record.get("normalized") or {})
    current = normalize_timestamp_ms(
        record.get("client", {}).get("expiryTime", normalized.get("expiry_time", 0))
    )
    now_ms = int(time.time() * 1000)
    if days_delta > 0:
        base = max(now_ms, current if current > 0 else now_ms)
        expiry = base + days_delta * 86_400_000
        enable = True
    else:
        if current <= 0:
            raise ValueError("Нельзя убавить дни у бессрочной подписки: сначала задайте дату окончания")
        expiry = max(1, current + days_delta * 86_400_000)
        enable = bool(normalized.get("enable", True)) and expiry > now_ms
    changes: dict[str, Any] = {"expiryTime": expiry, "enable": enable}
    if tg_id is not None and int(tg_id) > 0:
        changes["tgId"] = int(tg_id)
    last_error: Exception | None = None
    for cycle in range(2):
        try:
            update_client_sync(email, changes, record=record if cycle == 0 else None)
        except Exception as error:
            last_error = error
            logger.warning(
                "Не удалось применить изменение срока клиента %s (цикл %s/2): %s",
                email, cycle + 1, error,
            )
            if cycle == 0:
                time.sleep(0.35)
                record = None
                continue
            raise RuntimeError(
                f"Не удалось обновить срок клиента {email} в 3x-ui после 2 попыток: {error}"
            ) from error

        for attempt in range(3):
            try:
                updated = get_client_record_sync(email)
                result = dict(updated["normalized"])
                actual_client = updated.get("client") if isinstance(updated.get("client"), dict) else {}
                actual_expiry = normalize_timestamp_ms(actual_client.get("expiryTime", result.get("expiry_time", 0)))
                actual_enable = _bool(actual_client.get("enable", result.get("enable", False)), False)
                acceptable = actual_expiry == expiry and actual_enable == enable
                if acceptable:
                    result["expiry_time"] = actual_expiry
                    result["enable"] = actual_enable
                    if tg_id is not None and int(tg_id) > 0:
                        result["tg_id"] = int(tg_id)
                    return result
                last_error = RuntimeError(
                    f"3x-ui не подтвердила изменение срока {email}: "
                    f"ожидалось expiryTime={expiry}, enable={enable}; "
                    f"получено expiryTime={actual_expiry}, enable={actual_enable}"
                )
                logger.warning("%s", last_error)
            except Exception as error:
                last_error = error
                logger.warning(
                    "Не удалось проверить изменение срока клиента %s (попытка %s/3, цикл %s/2): %s",
                    email, attempt + 1, cycle + 1, error,
                )
            if attempt < 2:
                time.sleep(0.35)
        record = None

    raise RuntimeError(
        f"Изменение срока клиента {email} не подтверждено 3x-ui после 2 циклов: {last_error}"
    )


def bind_client_tg_id_sync(email: str, tg_id: int) -> dict[str, Any]:
    tg_id = int(tg_id)
    if tg_id <= 0:
        raise ValueError("Telegram ID должен быть положительным")
    update_client_sync(email, {"tgId": tg_id})
    record = get_client_record_sync(email)
    normalized = dict(record["normalized"])
    normalized["tg_id"] = tg_id
    return normalized


def find_client_by_telegram_id_sync(tg_id: int, force: bool = True) -> dict[str, Any] | None:
    """Find a panel client by explicit tgId, then by the legacy email suffix."""
    tg_id = int(tg_id)
    if tg_id <= 0:
        return None
    try:
        data = request_json_sync("GET", f"panel/api/clients/get/tgId/{tg_id}")
        obj = data.get("obj")
        candidates = obj if isinstance(obj, list) else [obj]
        for candidate in candidates:
            if isinstance(candidate, dict):
                normalized = normalize_client(candidate)
                if normalized.get("email"):
                    return normalized
    except Exception as error:
        logger.debug("Поиск клиента по tgId через прямой endpoint недоступен: %s", error)

    snapshot = fetch_snapshot_sync(force=force)
    explicit = snapshot.get("by_tg_id", {}).get(tg_id)
    if explicit:
        return dict(explicit)
    for item in snapshot.get("clients", []):
        if telegram_id_from_email(str(item.get("email") or "")) == tg_id:
            return dict(item)
    return None


def adjust_client_sync(email: str, days: int = 0, bytes_delta: int = 0) -> dict[str, Any]:
    result = request_json_sync(
        "POST",
        "panel/api/clients/bulkAdjust",
        {"emails": [email], "addDays": int(days), "addBytes": int(bytes_delta)},
    )
    invalidate_snapshot_cache()
    return result


def set_client_status_sync(email: str, enable: bool) -> dict[str, Any]:
    endpoint = "panel/api/clients/bulkEnable" if enable else "panel/api/clients/bulkDisable"
    result = request_json_sync("POST", endpoint, {"emails": [email]})
    invalidate_snapshot_cache()
    return result


def delete_client_sync(email: str, keep_traffic: bool = False) -> dict[str, Any]:
    result = request_json_sync(
        "POST",
        "panel/api/clients/bulkDel",
        {"emails": [email], "keepTraffic": bool(keep_traffic)},
    )
    invalidate_snapshot_cache()
    return result


class ServerClient:
    """Async compatibility wrapper used by the Telegram bot."""

    def __init__(self, *_args: Any, **_kwargs: Any):
        self.base_url = _base_url()
        self.headers = _headers()

    async def _request(self, method: str, path: str, data: dict | None = None) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(request_json_sync, method, path, data, 12.0)
        except Exception as error:
            logger.warning("3x-ui request failed: %s", error)
            return {"success": False, "msg": str(error)}

    async def add_or_update_client(
        self,
        uuid: str,
        email: str,
        sub_id: str,
        expiry_ts: int = 0,
        enable: bool = True,
    ) -> bool:
        check = await self._request("GET", f"panel/api/clients/get/{quote(email, safe='')}")
        current = check.get("obj") if isinstance(check.get("obj"), dict) else {}
        raw_client = current.get("client") if isinstance(current.get("client"), dict) else current
        inbound_ids = current.get("inboundIds") if isinstance(current.get("inboundIds"), list) else []
        if not inbound_ids and isinstance(raw_client, dict) and isinstance(raw_client.get("inboundIds"), list):
            inbound_ids = list(raw_client.get("inboundIds") or [])

        base = _editable_client_payload(raw_client, fallback_id=uuid)
        existing_client = bool(check.get("success") and current)
        payload = {
            **base,
            "email": email,
            "subId": base.get("subId") or sub_id,
            "totalGB": _int(base.get("totalGB")),
            "expiryTime": int(expiry_ts),
            "enable": bool(enable),
            "limitIp": _int(base.get("limitIp")),
            "flow": str(base.get("flow") or "") if existing_client else "",
        }
        payload = _editable_client_payload(payload, fallback_id=base.get("id") or uuid)

        if existing_client:
            result = await self._request("POST", f"panel/api/clients/update/{quote(email, safe='')}", payload)
        else:
            if not inbound_ids:
                inbound_ids = await asyncio.to_thread(inbound_ids_sync)
            result = await self._request(
                "POST",
                "panel/api/clients/add",
                {"client": payload, "inboundIds": inbound_ids},
            )
            if result.get("success"):
                await asyncio.to_thread(ensure_client_inbounds_sync, email, list(inbound_ids))
        invalidate_snapshot_cache()
        return bool(result.get("success"))

    async def delete_client(self, email: str) -> bool:
        result = await self._request("POST", "panel/api/clients/bulkDel", {"emails": [email], "keepTraffic": False})
        invalidate_snapshot_cache()
        return bool(result.get("success"))

    async def set_client_status(self, email: str, enable: bool) -> bool:
        path = "panel/api/clients/bulkEnable" if enable else "panel/api/clients/bulkDisable"
        result = await self._request("POST", path, {"emails": [email]})
        invalidate_snapshot_cache()
        return bool(result.get("success"))

    async def get_client_stats(self, email: str) -> dict[str, Any]:
        result = await self._request("GET", f"panel/api/clients/traffic/{quote(email, safe='')}")
        if result.get("success") and isinstance(result.get("obj"), dict):
            item = normalize_client({**result["obj"], "traffic": result["obj"]})
            item["exists"] = True
            return item
        snapshot = await asyncio.to_thread(fetch_snapshot_sync, False)
        item = snapshot.get("by_email", {}).get(email.lower())
        return {**item, "exists": True} if item else {"exists": False, "up": 0, "down": 0}

    async def get_system_status(self) -> dict[str, str]:
        try:
            import psutil

            return {
                "cpu": f"{psutil.cpu_percent():.0f}%",
                "ram": f"{psutil.virtual_memory().percent:.0f}%",
                "disk": f"{psutil.disk_usage('/').percent:.0f}%",
            }
        except Exception:
            return {"cpu": "Н/Д", "ram": "Н/Д", "disk": "Н/Д"}

    async def run_speedtest(self) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "speedtest-cli",
                "--simple",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            text = stdout.decode(errors="replace").strip()
            return text or "⚠️ Скорость временно недоступна."
        except Exception as error:
            return f"❌ Ошибка: {error}"

    async def get_online_users_list(self) -> list[str]:
        result = await self._request("POST", "panel/api/clients/onlines")
        if result.get("success") and isinstance(result.get("obj"), list):
            return [
                str(item.get("email") if isinstance(item, dict) else item).strip()
                for item in result["obj"]
                if item
            ]
        return []
