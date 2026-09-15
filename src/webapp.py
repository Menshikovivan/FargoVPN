#!/usr/bin/env python3
"""VPN Service Platform web control panel."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
import psutil
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool

import config
import auth_security
import cabinet_service
import referral_codes
import referral_rewards
import broadcast_manager
import subscription_refresh_manager
import diagnostics as platform_diagnostics
import identity_migration
import service_audit
import update_manager
import user_events
import push_service
from time_utils import from_timestamp as panel_from_timestamp, local_date_from_timestamp, now_local, utc_sql_day_start_for_local
from backup import reconcile_pending_deliveries, test_yandex_connection, test_yandex_write
from detached_jobs import DetachedJobError, launch_detached
from init_db import migrate as migrate_database
from services.media import (
    detect_media_type,
    download_telegram_file,
    download_telegram_media_to_path,
    media_kind,
    safe_media_filename,
)
from services.subscriptions import (
    approve_payment_sync,
    decline_payment_sync,
    ensure_subscription_sync,
    import_local_users_to_3xui_sync,
    repair_panel_client_identities_sync,
)
from services.xui_api import (
    bind_client_tg_id_sync,
    bytes_to_gb,
    change_client_days_sync,
    delete_client_sync,
    fetch_and_sync,
    fetch_snapshot_sync,
    snapshot_cache_status,
    fetch_client_extra_sync,
    fetch_control_snapshot_sync,
    invalidate_snapshot_cache,
    request_json_sync,
    set_client_status_sync,
    current_subscription_url_sync,
)



def public_prefix() -> str:
    value = str(getattr(config, "WEB_PUBLIC_PREFIX", "") or "").strip().strip("/")
    if not value:
        return ""
    parts = [part for part in value.split("/") if part]
    # Collapse an accidental exact duplication: /prefix/prefix -> /prefix.
    if len(parts) >= 2 and len(parts) % 2 == 0:
        mid = len(parts) // 2
        if "/".join(parts[:mid]) == "/".join(parts[mid:]):
            parts = parts[:mid]
    return "/" + "/".join(parts)


def public_path(path: str = "/") -> str:
    """Return exactly one public-prefix occurrence for an application path.

    The application owns its mounted URL namespace.  Reverse proxies must pass
    redirects/cookies through unchanged; this helper therefore canonicalises
    both already-prefixed and accidentally duplicated inputs.
    """
    prefix = public_prefix()
    raw = str(path or "/").strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    if not prefix:
        return raw
    # Collapse any number of repeated leading public prefixes.
    while raw == prefix + prefix or raw.startswith(prefix + prefix + "/"):
        raw = raw[len(prefix):] or "/"
    # Also accept the historical '/prefix/prefix/...'-style input with a
    # query/fragment attached by stripping only the path portion.
    if raw == prefix:
        return prefix
    if raw.startswith(prefix + "/"):
        return raw
    return prefix + raw


def rewrite_public_markup(markup: str) -> str:
    if not public_prefix() or not markup:
        return markup
    fs_roots = ("/mnt/", "/root/", "/etc/", "/var/", "/opt/", "/run/", "/tmp/", "/home/", "/proc/", "/sys/", "/dev/")
    prefix = public_prefix()
    def fix(value: str) -> str:
        if not value.startswith("/") or value == prefix or value.startswith(prefix + "/") or value.startswith("//"):
            return value
        if any(value.startswith(item) for item in fs_roots):
            return value
        return public_path(value)
    import re as _re
    out = _re.sub(r'((?:href|src|action|formaction)\s*=\s*[\"\'])(/[^\"\']*)([\"\'])', lambda m: m.group(1) + fix(m.group(2)) + m.group(3), markup, flags=_re.I)
    out = _re.sub(r'((?:fetch|navigate|openWindow)\(\s*[\"\'])(/[^\"\']*)([\"\'])', lambda m: m.group(1) + fix(m.group(2)) + m.group(3), out, flags=_re.I)
    return out


class ReverseProxyAssetRewriteMiddleware:
    """Rewrite root-relative browser URLs for path-mounted reverse proxy mode."""
    def __init__(self, app):
        self.app = app
        self.prefix = public_prefix()

    async def __call__(self, scope, receive, send):
        if not self.prefix or scope.get("type") != "http":
            return await self.app(scope, receive, send)
        state = {"rewrite": False, "headers": [], "body": bytearray()}

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                ctype = b""
                for key, value in headers:
                    if key.lower() == b"content-type":
                        ctype = value.lower()
                        break
                state["rewrite"] = any(x in ctype for x in (b"text/html", b"javascript", b"manifest+json"))
                if state["rewrite"]:
                    headers = [(k,v) for k,v in headers if k.lower() != b"content-length" and k.lower() != b"content-encoding"]
                # Canonicalise root-relative redirects so they contain exactly one prefix.
                adjusted = []
                prefix_text = self.prefix
                prefix_bytes = prefix_text.encode("utf-8")
                for k, v in headers:
                    if k.lower() == b"location" and v.startswith(b"/") and not v.startswith(b"//"):
                        try:
                            location = v.decode("utf-8")
                        except UnicodeDecodeError:
                            location = ""
                        if location:
                            while location == prefix_text + prefix_text or location.startswith(prefix_text + prefix_text + "/"):
                                location = location[len(prefix_text):] or "/"
                            if location == prefix_text:
                                location = prefix_text + "/"
                            elif not location.startswith(prefix_text + "/"):
                                location = prefix_text + location
                            v = location.encode("utf-8")
                    adjusted.append((k, v))
                headers = adjusted
                state["headers"] = headers
                await send({**message, "headers": headers})
                return
            if message["type"] == "http.response.body" and state["rewrite"]:
                state["body"].extend(message.get("body", b""))
                if message.get("more_body"):
                    return
                raw = bytes(state["body"])
                prefix = self.prefix.encode("utf-8")
                # Rewrite only truly root-relative browser URLs. Do not touch URLs
                # that already contain the mounted public prefix, otherwise
                # /prefix/path becomes /prefix/prefix/path.
                prefix_text = prefix.decode("utf-8").lstrip("/")
                # Rewrite root-relative browser URLs while preserving real server paths
                # such as /mnt, /root, /etc and other filesystem locations. This must
                # cover inline JS fetch('/api/...') as well as href/src/action attrs.
                fs_roots = (b"mnt/", b"root/", b"etc/", b"var/", b"opt/", b"run/", b"tmp/", b"home/", b"proc/", b"sys/", b"dev/")
                pref = b"/" + prefix_text.encode("utf-8")

                def safe_url(value: bytes) -> bytes:
                    if value.startswith(b"//") or value == pref or value.startswith(pref + b"/"):
                        return value
                    if any(value.startswith(b"/" + item) for item in fs_roots):
                        return value
                    return prefix + value

                # Rewrite only contexts that unambiguously contain a browser URL.
                # Do not scan arbitrary JavaScript for /... sequences: regex literals,
                # arithmetic, and object syntax can legitimately contain them.
                raw = re.sub(
                    rb"((?:fetch|navigate|openWindow)\(\s*[\"'`])(/[^\"'`\s<>]*)([\"'`])",
                    lambda m: m.group(1) + safe_url(m.group(2)) + m.group(3),
                    raw,
                )
                raw = re.sub(
                    rb"((?:href|src|action|formaction)\s*=\s*[\"'])(/[^\"']*)([\"'])",
                    lambda m: m.group(1) + safe_url(m.group(2)) + m.group(3),
                    raw,
                    flags=re.I,
                )
                raw = re.sub(
                    rb"((?:location(?:\.href)?|window\.location)\s*(?:=|\.replace\()\s*[\"'])(/[^\"']*)([\"'])",
                    lambda m: m.group(1) + safe_url(m.group(2)) + m.group(3),
                    raw,
                    flags=re.I,
                )
                headers = list(state["headers"]) + [(b"content-length", str(len(raw)).encode("ascii"))]
                await send({"type":"http.response.body", "body":raw, "more_body":False})
                return
            await send(message)

        await self.app(scope, receive, wrapped)


class PublicPrefixStripMiddleware:
    """Accept requests both behind and without the external FargoVPN path prefix.

    Some reverse-proxy/L4 configurations strip the mounted prefix before proxying,
    while others preserve it.  Normalising the incoming ASGI path here prevents
    FastAPI from returning a misleading 404/detail-not-found for otherwise valid
    FargoVPN routes.
    """
    def __init__(self, app):
        self.app = app
        self.prefix = public_prefix()

    async def __call__(self, scope, receive, send):
        if not self.prefix or scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        if path == self.prefix or path.startswith(self.prefix + "/"):
            # Canonicalize legacy URLs that contain the mounted prefix more than once.
            duplicate = self.prefix + self.prefix
            if path == duplicate or path.startswith(duplicate + "/"):
                # Canonicalise the browser URL before routing to the application.
                canonical_path = path
                while canonical_path == duplicate or canonical_path.startswith(duplicate + "/"):
                    canonical_path = canonical_path[len(self.prefix):] or "/"
                query = scope.get("query_string") or b""
                location = canonical_path + (("?" + query.decode("latin-1")) if query else "")
                headers = [(b"location", location.encode("utf-8")), (b"cache-control", b"no-store")]
                await send({"type":"http.response.start","status":308,"headers":headers})
                await send({"type":"http.response.body","body":b"","more_body":False})
                return
            scoped = dict(scope)
            # Strip the mounted prefix repeatedly so legacy links containing
            # /prefix/prefix/... still resolve to the same application route.
            new_path = path
            while new_path == self.prefix or new_path.startswith(self.prefix + "/"):
                new_path = new_path[len(self.prefix):] or "/"
            scoped["path"] = new_path
            raw_path = scoped.get("raw_path")
            if raw_path:
                raw_prefix = self.prefix.encode("utf-8")
                while raw_path == raw_prefix or raw_path.startswith(raw_prefix + b"/"):
                    raw_path = raw_path[len(raw_prefix):] or b"/"
                scoped["raw_path"] = raw_path
            await self.app(scoped, receive, send)
            return
        await self.app(scope, receive, send)


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(getattr(config, "__file__", APP_DIR / "config.py")).resolve()
PYTHON = APP_DIR / ".venv" / "bin" / "python"
if not PYTHON.is_file():
    PYTHON = Path(sys.executable)

migrate_database()

app = FastAPI(title=f"{config.SERVICE_NAME} Control Panel", docs_url=None, redoc_url=None)
app.add_middleware(ReverseProxyAssetRewriteMiddleware)
app.add_middleware(PublicPrefixStripMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.WEB_SECRET_KEY,
    https_only=bool(getattr(config, "WEB_COOKIE_HTTPS_ONLY", False)),
    path=(public_prefix() or "/"),
    same_site="lax",
    max_age=max(900, int(getattr(config, "WEB_SESSION_MAX_AGE_SECONDS", 28_800))),
)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
NET_CACHE = {
    "ts": time.time(),
    "in": psutil.net_io_counters().bytes_recv,
    "out": psutil.net_io_counters().bytes_sent,
}
METRICS_LAST_WRITE = 0.0
CHAT_MEDIA_CACHE_DIR = Path(
    getattr(config, "CHAT_MEDIA_CACHE_DIR", "/var/cache/vpn-service/chat-media")
).expanduser()
CHAT_MEDIA_CACHE_LAST_PRUNE = 0.0
ONLINE_METRICS_CACHE = {"ts": 0.0, "count": 0, "error": "", "stale": True}
ONLINE_METRICS_TTL = 0.0
ONLINE_HISTORY = collections.deque(maxlen=180)
ONLINE_HISTORY_LOCK = threading.Lock()
BACKUP_RECONCILE_LAST = 0.0
BACKUP_RECONCILE_INTERVAL = 60.0
LOGGER = logging.getLogger(__name__)


@app.middleware("http")
async def response_security_headers(request: Request, call_next):
    request_started = time.monotonic()
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
        origin = request.headers.get("origin", "").strip()
        host = request.headers.get("host", "").strip().lower()
        origin_host = urlsplit(origin).netloc.lower() if origin else ""
        if fetch_site == "cross-site" or (origin_host and host and origin_host != host):
            if request.url.path.startswith(("/api/", "/panel/api/")) or "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"detail": "Межсайтовый запрос отклонён"}, status_code=403)
            return PlainTextResponse("Межсайтовый запрос отклонён", status_code=403)

    response = await call_next(request)
    duration_ms = int((time.monotonic() - request_started) * 1000)
    if duration_ms >= 1000:
        LOGGER.warning(
            "performance operation=http_request method=%s path=%s duration_ms=%s status=%s",
            request.method.upper(), request.url.path, duration_ms, response.status_code,
        )
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    elif request.url.path.startswith("/api/users/") and request.url.path.endswith("/media"):
        response.headers["Cache-Control"] = "private, max-age=3600"
    else:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    response.headers["X-VPN-Platform-Version"] = update_manager.current_version()
    response.headers["X-FargoVPN-Responder"] = "application"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; form-action 'self'; "
        "img-src 'self' data: blob: https://t.me https://*.telegram.org; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://telegram.org; "
        "connect-src 'self' https://api.telegram.org https://telegram.org; frame-ancestors 'self'"
    )
    secure_request = request.url.scheme == "https"
    if secure_request:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def _secure_text_compare(left: str, right: str) -> bool:
    """Constant-time text comparison that also supports Unicode strings."""
    try:
        return hmac.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))
    except (TypeError, UnicodeEncodeError):
        return False


def verify_password(password: str) -> bool:
    stored = str(config.WEB_PASSWORD_HASH)
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt, digest = stored.split("$", 3)
            value = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
            ).hex()
            return _secure_text_compare(value, digest)
        except (ValueError, TypeError):
            return False
    return _secure_text_compare(password, stored)


def request_ip(request: Request) -> str:
    return str(request.client.host if request.client else "unknown")[:128]


def require_auth(request: Request) -> None:
    if not request.session.get("auth"):
        raise HTTPException(401)
    login_at = int(request.session.get("login_at") or 0)
    max_age = max(900, int(getattr(config, "WEB_SESSION_MAX_AGE_SECONDS", 28_800)))
    if login_at > 0 and int(time.time()) - login_at > max_age:
        request.session.clear()
        raise HTTPException(401, "Сессия истекла")


def session_csrf_token(request: Request) -> str:
    token = str(request.session.get("csrf_token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def require_csrf(request: Request, token: str) -> None:
    expected = str(request.session.get("csrf_token") or "")
    if not expected or not token or not hmac.compare_digest(expected, str(token)):
        raise HTTPException(403, "Недействительный CSRF-токен")


@contextmanager
def database():
    connection = sqlite3.connect(config.DB_PATH, timeout=20)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def audit(actor: str, action: str, details: str = "") -> None:
    try:
        with database() as connection:
            connection.execute(
                "INSERT INTO audit_log(actor,action,details) VALUES(?,?,?)",
                (actor, action, details[:4000]),
            )
    except Exception:
        pass


def panel_timezone_name() -> str:
    """Timezone used for human-facing timestamps in the administration panel.

    SQLite CURRENT_TIMESTAMP is UTC. Older installations may not have
    WEB_TIMEZONE, therefore Asia/Almaty (UTC+5) is the safe default for the
    panel's configured locale while remaining fully configurable in config.py.
    """
    return str(getattr(config, "WEB_TIMEZONE", "Asia/Almaty") or "Asia/Almaty").strip() or "Asia/Almaty"


AUDIT_ACTION_LABELS = {
    "login_blocked": "Вход заблокирован",
    "login_success": "Вход выполнен",
    "login_failure": "Неудачная попытка входа",
    "identity_import_preview": "Предпросмотр импорта привязок",
    "identity_import_apply": "Импорт привязок выполнен",
    "identity_manual_update": "Привязка Telegram ID изменена",
    "create_user": "Пользователь создан",
    "adjust_user_days": "Срок подписки изменён",
    "bind_telegram_id": "Telegram ID привязан",
    "toggle_user": "Статус пользователя изменён",
    "delete_user": "Пользователь удалён",
    "message_user": "Сообщение отправлено пользователю",
    "web_broadcast_started": "Рассылка запущена",
    "approve_payment": "Оплата подтверждена",
    "decline_payment": "Оплата отклонена",
    "backup_create": "Резервная копия создана",
    "backup_delete": "Резервная копия удалена",
    "backup_restore": "Резервная копия восстановлена",
    "settings_update": "Настройки панели изменены",
    "yandex_settings": "Настройки Яндекс Диска изменены",
    "github_settings_saved": "Настройки GitHub сохранены",
    "publish_update": "Обновление опубликовано",
    "manual_update_uploaded": "Архив обновления загружен",
    "apply_update": "Обновление установлено",
    "force_downgrade": "Запущено принудительное понижение версии",
    "rollback_update": "Выполнен откат версии",
    "fix_duplicate_services": "Исправлены дубли служб",
    "restart_service": "Служба перезапущена",
    "reminder_sent": "Напоминание отправлено",
    "reminders": "Напоминания",
    "payment_received": "Получена оплата",
    "payment_created": "Создан платёж",
    "payment_pending": "Платёж ожидает проверки",
    "payment_rejected": "Платёж отклонён",
    "reminders_started": "Напоминания запущены",
    "reminders_finished": "Напоминания завершены",
    "backup": "Резервное копирование",
    "update": "Обновление",
    "telegram": "Telegram",
    "web": "Веб-панель",
    "backup_failed": "Ошибка резервного копирования",
    "broadcast_completed": "Рассылка завершена",
    "broadcast_cancelled": "Рассылка отменена",
    "update_check": "Проверка обновлений выполнена",
}

AUDIT_DETAIL_LABELS = {
    "ip": "IP-адрес",
    "retry": "Повтор через, сек.",
    "blocked": "Заблокирован",
    "tg_id": "Telegram ID",
    "username": "Пользователь",
    "days": "Срок напоминания, дней",
    "expiry_time": "Подписка до",
    "job_id": "ID задачи",
    "version": "Версия",
    "sha256": "SHA-256",
    "path": "Путь",
    "name": "Имя",
    "target": "Служба",
    "selected_rows": "Выбрано записей",
    "payment_id": "Платёж",
    "amount": "Сумма",
    "currency": "Валюта",
    "status": "Статус",
    "reason": "Причина",
    "error": "Ошибка",
    "service": "Сервис",
    "detail": "Подробности",
    "email": "Email",
    "days_added": "Добавлено дней",
    "days_removed": "Удалено дней",
    "old_expiry": "Старый срок",
    "new_expiry": "Новый срок",
    "old_state": "Старый статус",
    "new_state": "Новый статус",
    "count": "Количество",
    "total": "Всего",
    "result": "Результат",
    "mode": "Режим",
    "type": "Тип",
    "filename": "Файл",
    "size": "Размер",
    "message": "Сообщение",
    "source": "Источник",
    "operation": "Операция",
}

def _format_audit_detail_value(key: str, value: Any) -> str:
    text = str(value if value is not None else "")
    if key == "blocked":
        return "да" if text.lower() in {"1", "true", "yes"} else "нет"
    if key == "days":
        try:
            days = int(text)
            if days == 0:
                return "сегодня"
            if days == 1:
                return "1 день"
            if 2 <= abs(days) % 100 <= 4 and not 12 <= abs(days) % 100 <= 14:
                return f"{days} дня"
            return f"{days} дней"
        except ValueError:
            return text
    if key in {"status", "state", "old_state", "new_state"}:
        return {
            "active": "активен", "inactive": "неактивен", "enabled": "включено",
            "disabled": "выключено", "pending": "ожидает", "approved": "подтверждено",
            "declined": "отклонено", "completed": "завершено", "failed": "ошибка",
            "cancelled": "отменено", "blocked": "заблокирован",
        }.get(text.strip().lower(), text)
    if key == "amount":
        try:
            amount = float(text.replace(",", "."))
            return f"{amount:,.2f} ₽".replace(",", " ").replace(".00", "")
        except ValueError:
            return text
    if key == "expiry_time":
        try:
            value_int = int(float(text))
            if value_int > 10_000_000_000:
                value_int //= 1000
            return panel_from_timestamp(value_int).strftime("%d.%m.%Y %H:%M")
        except (ValueError, OSError, OverflowError, ZoneInfoNotFoundError):
            return text
    return text

def format_audit_details(details: Any) -> str:
    """Backward-compatible audit detail formatter used by dashboard activity rows."""
    return audit_details_label(details)


def audit_details_label(details: Any, action: Any = "") -> str:
    raw = str(details or "").strip()
    if not raw:
        return "—"
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parts=[]
            for key, value in parsed.items():
                label = AUDIT_DETAIL_LABELS.get(str(key), str(key).replace("_", " ").capitalize())
                parts.append(f"{label}: {_format_audit_detail_value(str(key), value)}")
            return " · ".join(parts)
    except Exception:
        pass
    # Common legacy key=value details are rendered without technical noise.
    parts=[]
    for chunk in raw.split("; "):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            label = AUDIT_DETAIL_LABELS.get(key.strip(), key.strip().replace("_", " ").capitalize())
            parts.append(f"{label}: {_format_audit_detail_value(key.strip(), value.strip())}")
        else:
            parts.append(chunk.replace("_", " ").replace("->", "→"))
    return " · ".join(parts)

def audit_action_label(action: Any) -> str:
    key = str(action or "").strip()
    return AUDIT_ACTION_LABELS.get(key, key.replace("_", " ").capitalize())


def fmt_audit_timestamp(value: Any) -> str:
    """Convert stored UTC audit timestamps to the panel's local timezone."""
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        candidate = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            target = ZoneInfo(panel_timezone_name())
        except ZoneInfoNotFoundError:
            target = timezone(timedelta(hours=5))
        return parsed.astimezone(target).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return raw


def fmt_event_timestamp(value: Any, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format conversation timestamps without applying the timezone twice.

    Historical ``user_events`` rows were written with ``datetime.now()`` and
    are therefore already local, despite not carrying an offset.  Offset-aware
    rows are still converted to the configured panel timezone.  Keeping this
    logic separate from the UTC audit-log formatter preserves old histories
    and gives the dashboard and the messages page identical times.
    """
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            try:
                parsed = parsed.astimezone(ZoneInfo(panel_timezone_name()))
            except ZoneInfoNotFoundError:
                parsed = parsed.astimezone(timezone(timedelta(hours=5)))
        return parsed.strftime(fmt)
    except (TypeError, ValueError, OverflowError):
        return raw


def set_flash(request: Request, message: str, kind: str = "good") -> None:
    request.session["flash"] = {"message": message[:1000], "kind": kind}


def pop_flash(request: Request) -> str:
    data = request.session.pop("flash", None)
    if not isinstance(data, dict) or not data.get("message"):
        return ""
    kind = str(data.get("kind") or "good")
    cls = "toast bad-toast" if kind == "bad" else "toast"
    return f'<div class="{cls}">{html.escape(str(data["message"]))}</div>'


def shell(args: list[str], timeout: int = 20) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return ((result.stdout or "") + (result.stderr or ""))[-40000:]
    except Exception as error:
        return str(error)


_SERVICE_STATE_CACHE: dict[str, tuple[float, str]] = {}
_SERVICE_STATE_LOCK = threading.Lock()

def service_state(unit: str) -> str:
    now = time.time()
    with _SERVICE_STATE_LOCK:
        cached = _SERVICE_STATE_CACHE.get(unit)
        if cached and now - cached[0] < 5.0:
            return cached[1]
    value = shell(["systemctl", "is-active", unit], timeout=4).strip() or "unknown"
    with _SERVICE_STATE_LOCK:
        _SERVICE_STATE_CACHE[unit] = (now, value)
    return value


def fmt_bytes(value: float | int) -> str:
    number = float(value or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"):
        if abs(number) < 1024 or unit == "ПБ":
            return f"{number:.1f} {unit}"
        number /= 1024
    return "0 Б"


def fmt_date(milliseconds: int | None) -> str:
    if not milliseconds:
        return "Бессрочно"
    try:
        return panel_from_timestamp(int(milliseconds) / 1000).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "—"


def fmt_last_online(milliseconds: int | None, online: bool = False) -> str:
    if online:
        return "Сейчас онлайн"
    if not milliseconds:
        return "Нет данных"
    try:
        return panel_from_timestamp(int(milliseconds) / 1000).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "—"


def remaining_days(expiry_ms: int, now_ms: int | None = None) -> float:
    if not expiry_ms:
        return math.inf
    now_ms = now_ms or int(time.time() * 1000)
    delta = (int(expiry_ms) - now_ms) / 86_400_000
    return math.ceil(delta) if delta >= 0 else math.floor(delta)


def remaining_label(expiry_ms: int, now_ms: int | None = None) -> str:
    if not expiry_ms:
        return "∞"
    now_ms = now_ms or int(time.time() * 1000)
    if int(expiry_ms) < now_ms:
        expired_date = local_date_from_timestamp(int(expiry_ms) / 1000)
        days_ago = max(0, (local_date_from_timestamp(now_ms / 1000) - expired_date).days)
        return "Истекла сегодня" if days_ago == 0 else f"Истекла {days_ago} дн. назад"
    days = remaining_days(expiry_ms, now_ms)
    if days == 0:
        return "Сегодня"
    return f"{int(days)} дн."


def status_badge(active: bool, text_on: str = "Активен", text_off: str = "Отключён") -> str:
    css = "good" if active else "bad"
    return f'<span class="badge {css}">{html.escape(text_on if active else text_off)}</span>'


def api_source_badge(stale: bool) -> str:
    return (
        '<span class="badge warn">Локальный кэш</span>'
        if stale
        else '<span class="badge good">3x-ui LIVE</span>'
    )


def telegram_send(tg_id: int, message: str) -> tuple[bool, str]:
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
            data={"chat_id": int(tg_id), "text": message},
            timeout=20,
            trust_env=False,
        )
        data = response.json()
        if response.status_code == 200 and data.get("ok"):
            return True, "Сообщение отправлено"
        return False, str(data.get("description") or f"HTTP {response.status_code}")
    except Exception as error:
        return False, str(error)


def telegram_send_media(
    tg_id: int,
    upload: UploadFile,
    caption: str = "",
) -> tuple[bool, str, dict[str, Any], str, str]:
    """Send one photo/video from the web chat and return journal metadata."""
    filename = safe_media_filename(str(upload.filename or "media"), str(upload.content_type or ""))
    file_object = upload.file
    try:
        original_position = file_object.tell()
    except Exception:
        original_position = 0
    try:
        file_object.seek(0, os.SEEK_END)
        size = int(file_object.tell())
        file_object.seek(0)
        head = file_object.read(128)
        file_object.seek(0)
    except Exception as error:
        return False, f"Не удалось прочитать файл: {error}", {}, "admin_media", caption

    max_bytes = max(1, int(getattr(config, "CHAT_MEDIA_MAX_MB", 100))) * 1024 * 1024
    if size <= 0:
        return False, "Файл пуст", {}, "admin_media", caption
    if size > max_bytes:
        return (
            False,
            f"Файл больше разрешённого лимита {int(getattr(config, 'CHAT_MEDIA_MAX_MB', 100))} МБ",
            {},
            "admin_media",
            caption,
        )
    media_type = detect_media_type(head, filename, str(upload.content_type or ""))
    kind = media_kind(media_type)
    if kind not in {"photo", "video"} or not media_type:
        return False, "Поддерживаются только фотографии и видео", {}, "admin_media", caption

    endpoint = "sendPhoto" if kind == "photo" else "sendVideo"
    field = "photo" if kind == "photo" else "video"
    event_type = "admin_photo" if kind == "photo" else "admin_video"
    label = "Фото" if kind == "photo" else "Видео"
    event_text = caption.strip() or f"[{label}]"
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/{endpoint}",
            data={"chat_id": int(tg_id), "caption": caption[:1024]},
            files={field: (filename, file_object, media_type)},
            timeout=httpx.Timeout(300.0, connect=30.0),
            trust_env=False,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200 or not payload.get("ok"):
            return (
                False,
                str(payload.get("description") or f"Telegram HTTP {response.status_code}"),
                {},
                event_type,
                event_text,
            )
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        telegram_file: dict[str, Any] = {}
        photos = result.get("photo") if isinstance(result.get("photo"), list) else []
        if photos and isinstance(photos[-1], dict):
            telegram_file = photos[-1]
        else:
            candidate = result.get("video") or result.get("animation") or result.get("document")
            if isinstance(candidate, dict):
                telegram_file = candidate
        file_id = str(telegram_file.get("file_id") or "")
        if not file_id:
            return False, "Telegram не вернул file_id отправленного файла", {}, event_type, event_text
        media = {
            "kind": kind,
            "file_id": file_id,
            "file_unique_id": str(telegram_file.get("file_unique_id") or ""),
            "mime_type": str(telegram_file.get("mime_type") or media_type),
            "file_name": str(telegram_file.get("file_name") or filename),
            "file_size": int(telegram_file.get("file_size") or size),
            "width": int(telegram_file.get("width") or 0),
            "height": int(telegram_file.get("height") or 0),
            "duration": int(telegram_file.get("duration") or 0),
        }
        media = {key: value for key, value in media.items() if value not in ("", 0, None)}
        return True, "Файл отправлен", {"media": media, "source": "web_panel"}, event_type, event_text
    except Exception as error:
        return False, str(error), {}, event_type, event_text
    finally:
        try:
            file_object.seek(original_position)
        except Exception:
            pass


def telegram_receipt_file(payment_id: int) -> tuple[bytes, str, str]:
    with database() as connection:
        row = connection.execute(
            "SELECT telegram_file_id FROM payments WHERE id=?", (int(payment_id),)
        ).fetchone()
    if not row or not row[0]:
        raise HTTPException(404, "Чек не найден")
    try:
        content, media_type, filename = download_telegram_file(
            config.BOT_TOKEN, str(row[0]), timeout=30.0
        )
    except Exception as error:
        raise HTTPException(502, str(error)) from error
    extension = Path(filename).suffix or ".jpg"
    return content, media_type, f"receipt_{int(payment_id)}{extension}"


def _local_users() -> list[dict[str, Any]]:
    with database() as connection:
        rows = connection.execute("SELECT * FROM users").fetchall()
    return [dict(row) for row in rows]


def live_users(force: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge live 3x-ui values with local Telegram metadata.

    The Users page must remain usable when the 3x-ui API is temporarily down.
    In that case we return the local database snapshot and mark it stale instead
    of leaking an upstream exception as HTTP 500.
    """
    try:
        snapshot = fetch_and_sync(force=force, db_path=config.DB_PATH)
        if not isinstance(snapshot, dict):
            snapshot = {"stale": True, "error": "Некорректный ответ 3x-ui"}
    except Exception as error:
        LOGGER.warning("3x-ui недоступна для страницы пользователей: %s", error)
        snapshot = {"stale": True, "error": str(error)}
    local = _local_users()
    by_email = {str(row.get("email") or "").strip().lower(): row for row in local if row.get("email")}
    by_uuid = {str(row.get("uuid") or "").strip().lower(): row for row in local if row.get("uuid")}
    now_ms = int(time.time() * 1000)

    source_clients: list[dict[str, Any]]
    if not snapshot.get("stale"):
        source_clients = [dict(item) for item in snapshot.get("clients", [])]
    else:
        source_clients = [
            {
                "email": row.get("email") or "",
                "uuid": row.get("uuid") or "",
                "sub_id": row.get("sub_id") or "",
                "expiry_time": int(row.get("expiry_time") or 0),
                "enable": bool(row.get("enable")),
                "up": int(row.get("up") or 0),
                "down": int(row.get("down") or 0),
                "total": int(row.get("total") or 0),
                "last_online_ts": int(row.get("last_online_ts") or 0),
                "online": False,
                "tg_id": int(row.get("tg_id") or 0),
            }
            for row in local
        ]

    merged: list[dict[str, Any]] = []
    matched_local_ids: set[int] = set()
    for client in source_clients:
        email_key = str(client.get("email") or "").strip().lower()
        uuid_key = str(client.get("uuid") or "").strip().lower()
        row = by_email.get(email_key) or (by_uuid.get(uuid_key) if uuid_key else None) or {}
        if row.get("tg_id") is not None:
            try:
                matched_local_ids.add(int(row.get("tg_id") or 0))
            except (TypeError, ValueError):
                pass
        expiry = int(client.get("expiry_time") or 0)
        up = int(client.get("up") or 0)
        down = int(client.get("down") or 0)
        total = int(client.get("total") or 0)
        used = up + down
        quota_remaining = max(0, total - used) if total > 0 else None
        tg_id = int(row.get("tg_id") or client.get("tg_id") or 0)
        item = {
            **client,
            "tg_id": tg_id,
            "username": str(row.get("username") or "") or email_key.rsplit("_", 1)[0] or "Без имени",
            "expiry_time": expiry,
            "up": up,
            "down": down,
            "traffic_used": used,
            "total": total,
            "quota_remaining": quota_remaining,
            "last_online_ts": int(client.get("last_online_ts") or row.get("last_online_ts") or 0),
            "online": bool(client.get("online")),
            "enable": bool(client.get("enable")),
            "active": bool(client.get("enable")) and (expiry <= 0 or expiry > now_ms),
            "remaining_days": remaining_days(expiry, now_ms),
            "source_stale": bool(snapshot.get("stale")),
        }
        merged.append(item)

    # Telegram users are written to the local DB on first contact, before they
    # have purchased a subscription or received a 3x-ui client.  Include those
    # local-only shells in the Users page so admins can immediately open/read
    # the conversation and see the /start event.
    for row in local:
        try:
            local_tg_id = int(row.get("tg_id") or 0)
        except (TypeError, ValueError):
            local_tg_id = 0
        if local_tg_id <= 0 or local_tg_id in matched_local_ids:
            continue
        if str(row.get("email") or "").strip() or str(row.get("uuid") or "").strip():
            # Keep old/stale local records visible even if 3x-ui is temporarily
            # unavailable; they can still contain a useful conversation journal.
            pass
        expiry = int(row.get("expiry_time") or 0)
        remaining = remaining_days(expiry, now_ms)
        merged.append({
            "email": str(row.get("email") or ""),
            "uuid": str(row.get("uuid") or ""),
            "sub_id": str(row.get("sub_id") or ""),
            "tg_id": local_tg_id,
            "username": str(row.get("username") or f"id_{local_tg_id}"),
            "expiry_time": expiry,
            "up": int(row.get("up") or 0),
            "down": int(row.get("down") or 0),
            "traffic_used": int(row.get("up") or 0) + int(row.get("down") or 0),
            "total": int(row.get("total") or 0),
            "quota_remaining": None,
            "last_online_ts": int(row.get("last_online_ts") or 0),
            "online": False,
            "enable": bool(row.get("enable", 1)),
            "active": bool(row.get("enable", 1))
            and bool(str(row.get("email") or "").strip() or str(row.get("uuid") or "").strip() or str(row.get("sub_id") or "").strip())
            and (expiry <= 0 or expiry > now_ms),
            "remaining_days": remaining,
            "source_stale": bool(snapshot.get("stale")),
        })
    return merged, snapshot


def get_user(tg_id: int) -> dict[str, Any] | None:
    try:
        with database() as connection:
            row = connection.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error).lower():
            raise
        row = None
    if row:
        return dict(row)
    try:
        from services.subscriptions import ensure_telegram_user_shell_sync
        username = user_events.last_known_username(int(tg_id), db_path=config.DB_PATH)
        return ensure_telegram_user_shell_sync(int(tg_id), username, config.DB_PATH)
    except Exception as error:
        LOGGER.warning("Не удалось восстановить локального пользователя %s: %s", tg_id, error)
        return None


_CABINET_BOT_USERNAME = ""
_CABINET_BOT_USERNAME_TS = 0.0


def _cabinet_bot_username() -> str:
    global _CABINET_BOT_USERNAME, _CABINET_BOT_USERNAME_TS
    configured = str(getattr(config, "BOT_USERNAME", "") or "").strip().lstrip("@").strip()
    if configured:
        _CABINET_BOT_USERNAME = configured
        return configured
    if _CABINET_BOT_USERNAME and time.time() - _CABINET_BOT_USERNAME_TS < 3600:
        return _CABINET_BOT_USERNAME
    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMe",
            timeout=5.0, verify=True, trust_env=False,
        )
        response.raise_for_status()
        username = str((response.json().get("result") or {}).get("username") or "").strip()
        if username:
            _CABINET_BOT_USERNAME = username
            _CABINET_BOT_USERNAME_TS = time.time()
            return username
    except Exception as error:
        LOGGER.warning("Не удалось получить username Telegram-бота: %s", error)
    return _CABINET_BOT_USERNAME


def _cabinet_url() -> str:
    if not bool(getattr(config, "CABINET_ENABLED", True)):
        return ""
    base = cabinet_service.public_web_base_url().rstrip("/")
    path = str(getattr(config, "CABINET_PATH", "/cabinet") or "/cabinet")
    if not path.startswith("/"):
        path = "/" + path
    return (base + path) if base else ""


def _cabinet_active_user(tg_id: int) -> dict[str, Any] | None:
    try:
        return cabinet_service.resolve_user_for_cabinet(int(tg_id), config.DB_PATH)
    except Exception as error:
        LOGGER.warning("Ошибка получения пользователя кабинета %s: %s", tg_id, error)
        return None

def _cabinet_token_user(token: str) -> dict[str, Any] | None:
    return cabinet_service.resolve_token(token, config.DB_PATH)

def _cabinet_session_user(request: Request, access: str = "") -> dict[str, Any] | None:
    if str(access or "").strip():
        user = _cabinet_token_user(access)
        if not user:
            return None
        request.session["cabinet_tg_id"] = int(user.get("tg_id") or 0)
        request.session["cabinet_login_at"] = int(time.time())
        return user
    try:
        tg_id = int(request.session.get("cabinet_tg_id") or 0)
        login_at = int(request.session.get("cabinet_login_at") or 0)
    except (TypeError, ValueError):
        return None
    if tg_id <= 0 or login_at <= 0:
        return None
    max_age = max(900, int(getattr(config, "CABINET_SESSION_MAX_AGE_SECONDS", 86400)))
    if int(time.time()) - login_at > max_age:
        request.session.pop("cabinet_tg_id", None); request.session.pop("cabinet_login_at", None)
        return None
    return cabinet_service.resolve_user_for_cabinet(tg_id, config.DB_PATH)

def _cabinet_personal_url(tg_id: int, sub_id: str = "") -> str:
    return cabinet_service.personal_url(tg_id, sub_id)


def _cabinet_identity_candidates(tg_id: int) -> list[str]:
    return cabinet_service.identity_candidates(tg_id, config.DB_PATH)


def _cabinet_markup(username: str, user: dict[str, Any], sub_url: str) -> str:
    expiry = int(user.get("expiry_time") or 0)
    now_ms = int(time.time() * 1000)
    disabled = not bool(user.get("enable", True))
    if expiry <= 0:
        expiry_text, remaining = "Бессрочно", "∞"
    else:
        expiry_text = panel_from_timestamp(expiry / 1000).strftime("%d.%m.%Y %H:%M")
        remaining = remaining_label(expiry, now_ms)
    up = int(user.get("up") or 0); down = int(user.get("down") or 0); total = int(user.get("total") or 0)
    used = up + down
    quota = "Без лимита" if total <= 0 else f"{bytes_to_gb(max(0, total-used))} GB осталось"
    online = bool(user.get("online"))
    if disabled: status, status_cls = "Отключена", "bad"
    elif expiry > 0 and expiry <= now_ms: status, status_cls = "Истекла", "warn"
    else: status, status_cls = "Активна", "good"
    username_clean = html.escape(username or str(user.get("username") or "Пользователь"))
    sub_escaped = html.escape(sub_url, quote=True)
    bot_username = html.escape(_cabinet_bot_username(), quote=True)
    invite_code = html.escape(referral_codes.ensure_referral_code(config.DB_PATH, int(user.get("tg_id") or 0)))
    cabinet_logout_url = html.escape(public_path("/api/cabinet/logout"), quote=True)
    cabinet_connection_url = html.escape(public_path("/cabinet/connection"), quote=True)
    cabinet_home_url = html.escape(public_path("/cabinet"), quote=True)
    return f"""<!doctype html><html lang="ru" data-version="{html.escape(update_manager.current_version(), quote=True)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0b1220"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><link rel="manifest" href="{html.escape(public_path("/cabinet/manifest.webmanifest"), quote=True)}?v={html.escape(update_manager.current_version())}"><link rel="apple-touch-icon" href="{html.escape(public_path("/static/icons/icon-192.png"), quote=True)}"><link rel="stylesheet" href="{html.escape(public_path('/static/panel.css'), quote=True)}?v={html.escape(update_manager.current_version())}><script src="https://telegram.org/js/telegram-web-app.js"></script><title>Личный кабинет · {html.escape(str(config.SERVICE_NAME))}</title></head><body class="cabinet-page"><div class="cabinet-shell"><header class="cabinet-head"><div><div class="cabinet-brand">{html.escape(str(config.SERVICE_NAME))}</div><div class="cabinet-subtitle">Личный кабинет</div></div><button id="cabinet-logout" class="button secondary small">Выйти</button></header><main class="cabinet-main"><section class="cabinet-welcome"><div><div class="cabinet-eyebrow">Здравствуйте</div><h1>@{username_clean}</h1><p>Управляйте подпиской, копируйте ссылку подключения и код приглашения.</p></div><span class="badge {status_cls}">{status}</span></section><section class="cabinet-actions"><a class="button" href="https://t.me/{bot_username}?start=renew">🔄 Продлить подписку</a><a class="button secondary" href="{cabinet_connection_url}">📱 Как подключиться</a></section><section class="cabinet-grid"><div class="card cabinet-card cabinet-primary"><div class="cabinet-card-title"><span>📋 Моя подписка</span><span class="cabinet-online {'is-online' if online else ''}">{'● Онлайн' if online else '○ Не в сети'}</span></div><div class="cabinet-number">{remaining}</div><div class="muted">Осталось</div><div class="cabinet-facts"><div><span>До</span><b>{html.escape(expiry_text)}</b></div><div><span>Использовано</span><b>{bytes_to_gb(used)} GB</b></div><div><span>Лимит</span><b>{quota}</b></div></div></div><div class="card cabinet-card"><h2>🔗 Ссылка подписки</h2><p class="muted">Используйте её в совместимых VPN-клиентах.</p><div class="cabinet-copy"><input id="cabinet-sub-url" value="{sub_escaped}" readonly><button id="cabinet-copy" class="button">Копировать</button></div><div id="cabinet-copy-result" class="muted cabinet-result"></div></div><div class="card cabinet-card"><h2>📊 Трафик</h2><div class="cabinet-traffic"><div><span>Отдано</span><b>{bytes_to_gb(up)} GB</b></div><div><span>Скачано</span><b>{bytes_to_gb(down)} GB</b></div><div><span>Всего</span><b>{bytes_to_gb(used)} GB</b></div></div></div><div class="card cabinet-card"><h2>🎟 Код приглашения</h2><p class="muted">Приглашённый пользователь должен зарегистрироваться по коду и хотя бы один раз успешно оплатить подписку. После первой подтверждённой оплаты вы получите +10 бесплатных дней.</p><div class="cabinet-copy"><input id="cabinet-invite-code" value="{invite_code}" readonly><button id="cabinet-copy-invite" class="button">Копировать</button></div></div><div class="card cabinet-card"><h2>📱 Подключение</h2><p>Откройте ссылку подписки в VPN-клиенте или инструкции для устройства.</p><a class="button secondary" href="{cabinet_connection_url}">Инструкция по подключению</a><a class="button secondary" href="https://t.me/{bot_username}">Открыть Telegram-бота</a></div></section><section class="card cabinet-support"><h2>Нужна помощь?</h2><p class="muted">Для обращения к администратору используйте Telegram-бота.</p><a class="button" href="https://t.me/{bot_username}">💬 Открыть поддержку в Telegram</a></section></main></div><script>(function(){{const tg=window.Telegram&&window.Telegram.WebApp;try{{if(tg){{tg.ready();tg.expand();if(tg.disableVerticalSwipes)tg.disableVerticalSwipes();}}}}catch(_e){{}}const copy=document.getElementById('cabinet-copy');const input=document.getElementById('cabinet-sub-url');const result=document.getElementById('cabinet-copy-result');copy&&copy.addEventListener('click',async()=>{{try{{await navigator.clipboard.writeText(input.value);result.textContent='✅ Ссылка скопирована';}}catch(e){{input.select();document.execCommand('copy');result.textContent='✅ Ссылка скопирована';}}}});const inviteCopy=document.getElementById('cabinet-copy-invite');const inviteInput=document.getElementById('cabinet-invite-code');inviteCopy&&inviteCopy.addEventListener('click',async()=>{{try{{await navigator.clipboard.writeText(inviteInput.value);inviteCopy.textContent='✅ Скопировано';}}catch(e){{inviteInput.select();document.execCommand('copy');inviteCopy.textContent='✅ Скопировано';}}setTimeout(()=>inviteCopy.textContent='Копировать',1500);}});document.getElementById('cabinet-logout')?.addEventListener('click',async()=>{{try{{await fetch('{cabinet_logout_url}',{{method:'POST',credentials:'same-origin'}});}}finally{{location.href='{cabinet_home_url}';}}}});</script></body></html>"""


def event_for_web(event: dict[str, Any], tg_id: int) -> dict[str, Any]:
    """Remove Telegram secrets and prepare one safe browser event."""
    result = dict(event)
    legacy_text = str(result.get("text") or "").strip()
    legacy_match = re.fullmatch(
        r"Отправлено:\s*(?:ContentType\.)?([A-Za-z_]+)",
        legacy_text,
        flags=re.IGNORECASE,
    )
    if legacy_match:
        legacy_kind = legacy_match.group(1).lower()
        legacy_label = {
            "photo": "Фото",
            "video": "Видео",
            "animation": "Анимация",
            "video_note": "Видеосообщение",
            "document": "Документ",
            "voice": "Голосовое сообщение",
            "audio": "Аудио",
            "sticker": "Стикер",
        }.get(legacy_kind, legacy_kind.replace("_", " ").capitalize())
        result["text"] = (
            f"[{legacy_label}] Файл был получен старой версией и не содержит Telegram file_id; "
            "показать его повторно невозможно."
        )
        result["legacy_media_missing"] = True
    metadata = dict(result.get("metadata") or {})
    media = metadata.get("media")
    if isinstance(media, dict) and media.get("file_id") and media.get("kind") in {"photo", "video"}:
        public_media = {
            key: media.get(key)
            for key in ("kind", "mime_type", "file_name", "file_size", "width", "height", "duration")
            if media.get(key) not in (None, "", 0)
        }
        public_media["url"] = f"/api/users/{int(tg_id)}/events/{int(result.get('id') or 0)}/media"
        metadata["media"] = public_media
    else:
        metadata.pop("media", None)
    metadata.pop("telegram_file_id", None)
    result["metadata"] = metadata
    return result


def _event_media_html(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    media = metadata.get("media") if isinstance(metadata, dict) else None
    if not isinstance(media, dict):
        return ""
    url = html.escape(str(media.get("url") or ""), quote=True)
    if not url:
        return ""
    kind = str(media.get("kind") or "")
    filename = html.escape(str(media.get("file_name") or ("Фотография" if kind == "photo" else "Видео")))
    if kind == "photo":
        return (
            f'<a class="chat-media-link" href="{url}" target="_blank" rel="noopener">'
            f'<img class="chat-media-image" src="{url}" alt="{filename}" loading="lazy"></a>'
        )
    if kind == "video":
        return (
            f'<video class="chat-media-video" controls preload="metadata" playsinline src="{url}">'
            'Ваш браузер не поддерживает воспроизведение видео.</video>'
        )
    return ""


def render_user_events(events: list[dict[str, Any]], tg_id: int) -> str:
    result: list[str] = []
    for raw_event in events:
        event = event_for_web(raw_event, tg_id)
        direction = str(event.get("direction") or "system")
        if direction not in {"in", "out", "system"}:
            direction = "system"
        text = str(event.get("text") or event.get("event_type") or "Событие")
        actor = str(event.get("actor") or event.get("username") or "system")
        text_html = html.escape(text).replace("\n", "<br>")
        failed = " failed" if not bool(event.get("success", True)) else ""
        result.append(
            f'<div class="chat-message {direction}{failed}" data-event-id="{int(event.get("id") or 0)}">'
            f'{_event_media_html(event)}<div class="chat-message-text">{text_html}</div>'
            f'<small>{html.escape(str(event.get("created_at") or ""))} · {html.escape(actor)}</small></div>'
        )
    return "".join(result)


def _chat_media_cache_path(tg_id: int, event_id: int, file_id: str) -> Path:
    digest = hashlib.sha256(str(file_id).encode("utf-8")).hexdigest()[:20]
    return CHAT_MEDIA_CACHE_DIR / str(int(tg_id)) / f"{int(event_id)}-{digest}.media"


def _prune_chat_media_cache(protected: Path | None = None) -> None:
    global CHAT_MEDIA_CACHE_LAST_PRUNE
    now = time.time()
    if now - CHAT_MEDIA_CACHE_LAST_PRUNE < 3600:
        return
    CHAT_MEDIA_CACHE_LAST_PRUNE = now
    try:
        CHAT_MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        max_age = max(1, int(getattr(config, "CHAT_MEDIA_CACHE_DAYS", 30))) * 86400
        max_bytes = max(16, int(getattr(config, "CHAT_MEDIA_CACHE_MAX_MB", 512))) * 1024 * 1024
        files: list[tuple[float, int, Path]] = []
        total = 0
        for path in CHAT_MEDIA_CACHE_DIR.rglob("*.media"):
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
                if now - stat.st_mtime > max_age and path != protected:
                    path.unlink(missing_ok=True)
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
            except OSError:
                continue
        if total > max_bytes:
            for _mtime, size, path in sorted(files):
                if path == protected:
                    continue
                path.unlink(missing_ok=True)
                total -= size
                if total <= max_bytes:
                    break
    except OSError:
        pass


def cached_event_media(tg_id: int, event_id: int) -> tuple[Path, str, str]:
    event = user_events.get_event(tg_id, event_id, db_path=config.DB_PATH)
    if not event:
        raise HTTPException(404, "Событие не найдено")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    media = metadata.get("media") if isinstance(metadata, dict) else None
    if not isinstance(media, dict) or media.get("kind") not in {"photo", "video"}:
        raise HTTPException(404, "В событии нет поддерживаемого медиа")
    file_id = str(media.get("file_id") or "")
    if not file_id:
        raise HTTPException(404, "Telegram file_id отсутствует")
    cache_path = _chat_media_cache_path(tg_id, event_id, file_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    media_type = str(media.get("mime_type") or "")
    filename = safe_media_filename(str(media.get("file_name") or "media"), media_type)
    if not cache_path.is_file() or cache_path.stat().st_size <= 0:
        try:
            cache_path, media_type, downloaded_name = download_telegram_media_to_path(
                config.BOT_TOKEN,
                file_id,
                cache_path,
                timeout=300.0,
                max_bytes=max(1, int(getattr(config, "CHAT_MEDIA_MAX_MB", 100))) * 1024 * 1024,
                allowed_kinds=(str(media.get("kind")),),
            )
            filename = safe_media_filename(downloaded_name or filename, media_type)
        except Exception as error:
            raise HTTPException(502, f"Не удалось получить медиа из Telegram: {error}") from error
    elif not media_type:
        with cache_path.open("rb") as handle:
            media_type = detect_media_type(handle.read(128), filename, "") or "application/octet-stream"
    _prune_chat_media_cache(cache_path)
    return cache_path, media_type or "application/octet-stream", filename


def navigation(active: str) -> str:
    items = [
        ("dashboard", "/", "home", "Обзор"),
        ("users", "/users", "users", "Пользователи"),
        ("subscription-tools", "/subscription-tools", "refresh", "Ссылки подписок"),
        ("messages", "/messages", "message", "Сообщения"),
        ("xui", "__XUI_EXTERNAL__", "xui", "3x-ui"),
        ("broadcast", "/broadcast", "broadcast", "Рассылка"),
        ("payments", "/payments", "card", "Платежи"),
        ("monitoring", "/monitoring", "activity", "Мониторинг"),
        ("backups", "/backups", "backup", "Бэкапы"),
        ("yandex", "/settings/yandex", "cloud", "Яндекс.Диск"),
        ("updates", "/updates", "upload", "Обновления"),
        ("logs", "/logs", "logs", "Журналы"),
        ("audit", "/audit", "audit", "Аудит"),
        ("reminders", "/reminders", "bell", "Напоминания"),
        ("settings", "/settings", "settings", "Настройки"),
        ("diagnostics", "/diagnostics", "check", "Диагностика"),
    ]

    nav_svgs = {
        "home": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 10 9-7 9 7"/><path d="M5 9.5V21h14V9.5"/><path d="M9 21v-6h6v6"/></svg>',
        "users": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 20v-1.5a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4V20"/><circle cx="9.5" cy="7" r="3.5"/><path d="M17 4.7a3.5 3.5 0 0 1 0 6.8"/><path d="M21 20v-1.5a4 4 0 0 0-3-3.9"/></svg>',
        "refresh": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5"/><path d="M4 17v-5h5"/><path d="M6.1 9a7 7 0 0 1 11.5-2.5L20 9M4 15l2.4 2.5A7 7 0 0 0 17.9 15"/></svg>',
        "message": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 9.6 9.6 0 0 1-3.4-.6L4 20l1.6-3.6A7.4 7.4 0 0 1 4 11.5 7.5 7.5 0 0 1 12 4a7.5 7.5 0 0 1 8 7.5Z"/><path d="M8 11.5h.01M12 11.5h.01M16 11.5h.01"/></svg>',
        "xui": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 7.8 4.5v9L12 21l-7.8-4.5v-9L12 3Z"/><path d="m8 8 4-2 4 2-4 2-4-2ZM8 8v5l4 2 4-2V8"/></svg>',
        "broadcast": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 14.5a3 3 0 0 1 0-5l12-5.5v16L4 14.5Z"/><path d="M16 9.5a4.5 4.5 0 0 1 0 5M19 7a8 8 0 0 1 0 10"/><path d="M7 15 9 20"/></svg>',
        "card": '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M7 15h4"/></svg>',
        "activity": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2.2-7 4.2 14 2.2-7H21"/></svg>',
        "backup": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7a8 8 0 1 1-1.8 8"/><path d="M6 3v5h5"/></svg>',
        "cloud": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 19h10a4 4 0 0 0 .6-8A6 6 0 0 0 6 9.8 4.7 4.7 0 0 0 7 19Z"/></svg>',
        "upload": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4M8 8l4-4 4 4"/><path d="M5 14v5h14v-5"/></svg>',
        "logs": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h13M7 12h13M7 19h13"/><path d="M4 5h.01M4 12h.01M4 19h.01"/></svg>',
        "audit": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/></svg>',
        "bell": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9ZM10 21h4"/></svg>',
        "settings": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="m19.4 15 .1.1-1.7 2.9-.2-.1a2 2 0 0 0-3 1.7v.2h-3.4v-.2a2 2 0 0 0-3-1.7l-.2.1-1.7-2.9.1-.1a2 2 0 0 0 0-3l-.1-.1 1.7-2.9.2.1a2 2 0 0 0 3-1.7v-.2h3.4v.2a2 2 0 0 0 3 1.7l.2-.1 1.7 2.9-.1.1a2 2 0 0 0 0 3Z"/></svg>',
        "check": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="m8.5 12 2.3 2.3 4.7-5"/></svg>',
    }
    links: list[str] = []
    try:
        unread_total = int(user_events.unread_messages_summary(db_path=config.DB_PATH).get("total") or 0)
    except Exception:
        unread_total = 0
    xui_panel_url = str(getattr(config, "XUI_PANEL_URL", "") or getattr(config, "BASE_URL", "") or "").strip().rstrip("/")
    for key, url, icon, label in items:
        if key == "xui":
            if not xui_panel_url:
                continue
            url = xui_panel_url
        if url.startswith('/'):
            url = public_path(url)
        links.append(
            f'<a class="{"active" if key == active else ""}" href="{url}">'
            f'<i class="nav-icon">{nav_svgs.get(icon, nav_svgs["settings"])}</i><span class="nav-label">{label}</span>'
            f'{("<span class=\"nav-unread-badge\" data-unread-total aria-label=\"Непрочитанные сообщения\">" + str(unread_total) + "</span>") if key == "messages" and unread_total else ""}'
            f'</a>'
        )

    return (
        '<aside id="panel-sidebar" aria-label="Основная навигация"><div class="brand"><div class="logo">V</div><div>'
        f'<strong>{html.escape(str(config.SERVICE_NAME))}</strong><small>Control Panel</small></div></div>'
        f'<nav>{"".join(links)}</nav><div class="aside-foot"><a href="{html.escape(public_path("/logout"), quote=True)}">⇥ Выйти</a></div></aside>'
    )

def update_banner() -> str:
    try:
        info = update_manager.cached_update_info()
        if info.get("available"):
            version = html.escape(str(info.get("version") or ""))
            return (
                '<div class="update-banner" id="global-update-banner"><strong>Доступно обновление '
                f'{version}</strong><span>Новая версия готова к установке.</span>'
                f'<a class="button small" href="{html.escape(public_path("/updates"), quote=True)}">Обновить</a></div>'
            )
    except Exception:
        pass
    return ""


def page(request: Request, title: str, body: str, active: str = "", scripts: str = "") -> str:
    banner = update_banner() if active else ""
    banner_slot = banner if banner else ('<div id="global-update-slot"></div>' if active else "")
    flash = pop_flash(request) if active else ""
    navigation_html = navigation(active) if active else ""
    body_class = "panel-page" if active else "login-page"
    mobile_controls = (
        '<div class="mobile-topbar"><button type="button" class="mobile-nav-toggle" id="mobile-nav-toggle" aria-label="Открыть меню" aria-expanded="false" aria-controls="panel-sidebar">'
        '<span></span><span></span><span></span></button><div class="mobile-topbar-title">'
        f'{html.escape(str(config.SERVICE_NAME))}</div><a class="mobile-topbar-action" href="{html.escape(public_path("/messages"), quote=True)}" aria-label="Сообщения">'
        '<span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 9.6 9.6 0 0 1-3.4-.6L4 20l1.6-3.6A7.4 7.4 0 0 1 4 11.5 7.5 7.5 0 0 1 12 4a7.5 7.5 0 0 1 8 7.5Z"/></svg></span></a></div>'
        '<div class="mobile-nav-backdrop" id="mobile-nav-backdrop"></div>'
    ) if active else ""
    sw_url = public_path('/service-worker.js') + '?v=' + str(update_manager.current_version())
    sw_scope = public_path('/')
    # Keep navigation usable even when an old PWA cache delays panel.js. The
    # fallback marks the button before panel.js loads, so only one handler runs.
    mobile_nav_fallback = "" if not active else """<script>(function(){
      function bind(){
        const t=document.getElementById('mobile-nav-toggle'),a=document.querySelector('body.panel-page > aside');
        if(!t||!a||t.dataset.navBound==='1') return;
        t.dataset.navBound='1';
        const root=document.documentElement,back=document.getElementById('mobile-nav-backdrop');
        const set=open=>{document.body.classList.toggle('mobile-nav-open',open);root.classList.toggle('mobile-menu-open',open);t.classList.toggle('is-open',open);t.setAttribute('aria-expanded',open?'true':'false');t.setAttribute('aria-label',open?'Закрыть меню':'Открыть меню');a.setAttribute('aria-hidden',open?'false':'true');a.style.transform=open?'translate3d(0,0,0)':'translate3d(-110%,0,0)';a.style.visibility=open?'visible':'hidden';a.style.pointerEvents=open?'auto':'none';back&&(back.style.visibility=open?'visible':'hidden',back.style.opacity=open?'1':'0',back.style.pointerEvents=open?'auto':'none');};
        let last=0;
        const toggle=event=>{event.preventDefault();event.stopPropagation();set(!document.body.classList.contains('mobile-nav-open'));};
        t.addEventListener('click',toggle,{passive:false});
        back&&back.addEventListener('click',()=>set(false));
        a.querySelectorAll('a').forEach(x=>x.addEventListener('click',()=>set(false)));
        set(false);
      }
      if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',bind,{once:true}); else bind();
    })();</script>"""
    sw_bootstrap = f"<script>if ('serviceWorker' in navigator) {{ window.addEventListener('load', async () => {{ try {{ const swUrl = new URL('{html.escape(sw_url, quote=True)}', location.href).href; const r = await navigator.serviceWorker.register(swUrl, {{updateViaCache: 'none'}}); await r.update(); }} catch(e) {{ console.warn('Service worker registration failed', e); }} }}); }}</script>"
    if active:
        scripts = mobile_nav_fallback + scripts
    if active:
        body = re.sub(r'<header(?![^>]*\bclass=)([^>]*)>', r'<header class="page-header"\1>', body, count=1)
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · {html.escape(str(config.SERVICE_NAME))}</title>
<meta name="theme-color" content="#0b1220">
<meta name="fargovpn-sw-url" content="{html.escape(sw_url, quote=True)}">
<meta name="fargovpn-sw-scope" content="{html.escape(sw_scope, quote=True)}"><meta name="fargovpn-canonical-path" content="{html.escape(public_path('/'), quote=True)}">
<link rel="manifest" href="{html.escape(public_path("/manifest.webmanifest"), quote=True)}?v={html.escape(update_manager.current_version())}">
<link rel="apple-touch-icon" href="{html.escape(public_path('/static/icons/icon-192.png'), quote=True)}">
<link rel="icon" href="{html.escape(public_path('/static/icons/icon-192.png'), quote=True)}" type="image/png">
<link rel="stylesheet" href="{html.escape(public_path('/static/panel.css'), quote=True)}?v={html.escape(update_manager.current_version())}">
</head><body class="{body_class}">{navigation_html}{mobile_controls}<main>{banner_slot}{flash}{body}</main>
<script src="{html.escape(public_path('/static/panel.js'), quote=True)}?v={html.escape(update_manager.current_version())}" defer></script>{sw_bootstrap}{scripts}</body></html>'''


@app.exception_handler(401)
async def unauthorized(request: Request, error: Exception):
    if request.url.path.startswith(("/api/", "/panel/api/")):
        detail = getattr(error, "detail", "Требуется авторизация")
        return JSONResponse({"detail": str(detail)}, status_code=401)
    return RedirectResponse(public_path("/login"), 303)


@app.get("/favicon.ico")
def favicon():
    icon = APP_DIR / "static" / "icons" / "icon-192.png"
    if not icon.is_file():
        raise HTTPException(404, "Favicon not found")
    return FileResponse(icon, media_type="image/png")


@app.get("/manifest.webmanifest")
def manifest_webmanifest():
    version = update_manager.current_version()
    prefix = public_path("/").rstrip("/") + "/"
    payload = {
        "name": str(config.SERVICE_NAME),
        "short_name": "VPN Platform",
        "description": "Панель управления VPN Service Platform",
        "start_url": prefix,
        "scope": prefix,
        "display": "standalone",
        "background_color": "#080d18",
        "theme_color": "#0b1220",
        "lang": "ru",
        "id": prefix,
        "icons": [
            {"src": prefix + "static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": prefix + "static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
        "version": str(version),
    }
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@app.get("/cabinet/manifest.webmanifest")
def cabinet_manifest():
    cabinet_root = public_path("/cabinet").rstrip("/")
    prefix = cabinet_root + "/"
    return JSONResponse({
        "name": str(config.SERVICE_NAME) + " · Личный кабинет",
        "short_name": "FargoVPN",
        "start_url": cabinet_root,
        "scope": prefix,
        "display": "standalone",
        "background_color": "#080d18",
        "theme_color": "#0b1220",
        "lang": "ru",
        "id": prefix,
        "icons": [
            {"src": public_path("/static/icons/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": public_path("/static/icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })


@app.get("/service-worker.js")
def service_worker():
    version = update_manager.current_version()
    prefix = public_path("/").rstrip("/")
    script = (APP_DIR / "service-worker.js").read_text(encoding="utf-8")
    script = script.replace("__FARGOVPN_CACHE__", f"fargovpn-static-v{version}")
    script = script.replace("__FARGOVPN_BASE__", prefix + "/")
    script = script.replace("__FARGOVPN_VERSION__", str(version))
    response = Response(content=script, media_type="application/javascript")
    response.headers["Service-Worker-Allowed"] = prefix + "/"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    action = html.escape(public_path("/login"), quote=True)
    target = json.dumps(public_path("/"))
    service = html.escape(str(config.SERVICE_NAME))
    body = """<div class="login-wrap"><div class="card login-card"><div class="login-brand"><div class="logo">V</div><div><strong>%s</strong><span>Control Panel</span></div></div><h1>Вход в панель</h1><p class="subtitle">Адаптировано для телефона, браузера и PWA.</p><form id="login-form" method="post" action="%s" novalidate><div class="setting"><label for="login-user">Логин</label><input id="login-user" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required></div><div class="setting"><label for="login-pass">Пароль</label><input id="login-pass" name="password" type="password" autocomplete="current-password" required></div><button id="login-submit" class="button" type="submit">Войти</button><div id="login-status" class="login-status" aria-live="polite"></div></form></div></div><script>
(function(){
 const f=document.getElementById('login-form'),b=document.getElementById('login-submit'),st=document.getElementById('login-status');
 if(!f)return;
 f.addEventListener('submit',async function(e){
  e.preventDefault(); b.disabled=true; st.textContent='Проверяю данные…'; st.className='login-status';
  try{
   const r=await fetch(f.action,{method:'POST',credentials:'same-origin',headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'},body:new FormData(f),cache:'no-store'});
   let d={}; try{d=await r.json();}catch(_){}
   if(!r.ok || !d.ok) throw new Error(d.detail || 'Неверный логин или пароль');
   st.textContent='Вход выполнен'; st.className='login-status ok'; window.location.replace(%s);
  }catch(err){ st.textContent='❌ '+(err.message||'Ошибка входа'); st.className='login-status bad'; }
  finally{ b.disabled=false; }
 });
})();</script>""" % (service, action, target)
    return page(request, "Вход", body)


@app.post("/login")
def login_post(request: Request, username: str = Form(), password: str = Form()):
    ip_address = request_ip(request)
    clean_username = str(username or "").strip()[:200]
    username_state = auth_security.check_login(ip_address, clean_username, db_path=config.DB_PATH)
    ip_state = auth_security.check_login(ip_address, auth_security.GLOBAL_USERNAME_KEY, db_path=config.DB_PATH)
    blocked_states = [item for item in (username_state, ip_state) if not item.allowed]
    wants_json = "application/json" in request.headers.get("accept", "").lower() or request.headers.get("x-requested-with") == "XMLHttpRequest"
    if blocked_states:
        state = max(blocked_states, key=lambda item: item.retry_after)
        retry_after = max(1, state.retry_after)
        audit("security", "login_blocked", f"ip={ip_address}; retry={state.retry_after}")
        if wants_json:
            return JSONResponse({"ok": False, "detail": f"Слишком много неудачных попыток. Повторите через {retry_after} сек.", "retry_after": retry_after}, status_code=429, headers={"Retry-After": str(retry_after)})
        body = (
            '<div class="login-wrap"><div class="card login-card">'
            '<h2>Вход временно ограничен</h2><p>Слишком много неудачных попыток. '
            f'Повторите через {retry_after} сек.</p>'
            '<a class="button secondary" href="'+html.escape(public_path('/login'), quote=True)+'">Вернуться</a></div></div>'
        )
        return HTMLResponse(page(request, "Вход", body), 429, headers={"Retry-After": str(retry_after)})

    valid_username = _secure_text_compare(clean_username, str(config.WEB_USERNAME))
    # Always perform the password hash, even for a wrong username, so response
    # timing does not reveal the configured panel login.
    valid_password = verify_password(password)
    if valid_username and valid_password:
        auth_security.record_success(ip_address, clean_username, db_path=config.DB_PATH)
        auth_security.record_success(ip_address, auth_security.GLOBAL_USERNAME_KEY, db_path=config.DB_PATH)
        auth_security.prune(db_path=config.DB_PATH)
        if not str(config.WEB_PASSWORD_HASH).startswith("pbkdf2_sha256$"):
            save_config_values({"WEB_PASSWORD_HASH": _password_hash(password)})
        request.session.clear()
        request.session["auth"] = True
        request.session["user"] = clean_username
        request.session["login_at"] = int(time.time())
        audit(clean_username, "login_success", f"ip={ip_address}")
        if "application/json" in request.headers.get("accept", "").lower() or request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JSONResponse({"ok": True, "redirect": public_path("/")})
        return RedirectResponse(public_path("/"), 303)

    failed_username = auth_security.record_failure(ip_address, clean_username, db_path=config.DB_PATH)
    failed_ip = auth_security.record_failure(ip_address, auth_security.GLOBAL_USERNAME_KEY, db_path=config.DB_PATH)
    failed_states = [item for item in (failed_username, failed_ip) if not item.allowed]
    failed = max(failed_states, key=lambda item: item.retry_after) if failed_states else failed_ip
    audit("security", "login_failure", f"ip={ip_address}; blocked={not failed.allowed}")
    retry = max(1, failed.retry_after)
    detail = (
        f"Слишком много неудачных попыток. Повторите через {retry} сек."
        if not failed.allowed
        else "Неверный логин или пароль."
    )
    body = (
        '<div class="login-wrap"><div class="card login-card"><h2>Войти не удалось</h2>'
        f'<p>{html.escape(detail)}</p><a class="button secondary" href="/login">Повторить</a></div></div>'
    )
    status_code = 429 if not failed.allowed else 403
    headers = {"Retry-After": str(retry)} if status_code == 429 else None
    if wants_json:
        payload = {"ok": False, "detail": detail}
        if status_code == 429:
            payload["retry_after"] = retry
        return JSONResponse(payload, status_code=status_code, headers=headers)
    return HTMLResponse(page(request, "Вход", body), status_code, headers=headers)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(public_path("/login"), 303)


def dashboard_backup_status() -> dict[str, Any]:
    """Return the latest local backup plus reconciled delivery state."""
    global BACKUP_RECONCILE_LAST
    now = time.time()
    # Remote Yandex reconciliation is performed by the dedicated backup worker.
    # It must never occupy an interactive web request with external network I/O.
    result: dict[str, Any] = {
        "state": "bad",
        "label": "Нет резервной копии",
        "filename": "",
        "created_at": "",
        "telegram_ok": False,
        "yandex_ok": False,
        "pending": False,
    }
    try:
        backup_dir = Path(config.BACKUP_DIR)
        archives = [path for path in backup_dir.glob("vpn_service_full_backup_*.tar.gz") if path.is_file()]
        latest_local = max(archives, key=lambda item: item.stat().st_mtime) if archives else None
    except OSError:
        latest_local = None
    if not latest_local:
        return result

    filename = latest_local.name
    created_dt = panel_from_timestamp(latest_local.stat().st_mtime)
    result.update({
        "filename": filename,
        "created_at": created_dt.isoformat(timespec="seconds"),
        "label": f"Последний: {created_dt.strftime('%d.%m.%Y %H:%M')}",
    })
    try:
        with database() as connection:
            exists = bool(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_runs'"
            ).fetchone())
            if exists:
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(backup_runs)")}
                if {"filename", "telegram_ok", "yandex_ok", "error", "created_at"}.issubset(columns):
                    row = connection.execute(
                        "SELECT telegram_ok,yandex_ok,error,created_at FROM backup_runs WHERE filename=? ORDER BY id DESC LIMIT 1",
                        (filename,),
                    ).fetchone()
                    if row:
                        result["telegram_ok"] = bool(row["telegram_ok"])
                        result["yandex_ok"] = bool(row["yandex_ok"])
                        result["run_created_at"] = str(row["created_at"] or "")
                        result["error"] = str(row["error"] or "")
    except (OSError, sqlite3.Error) as error:
        LOGGER.warning("Не удалось прочитать статус доставки бэкапа: %s", error)

    try:
        state_path = Path(getattr(config, "BACKUP_STATE_PATH", "/var/lib/vpn-service/backup-state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        pending = [item for item in state.get("pending_deliveries", []) if isinstance(item, dict)] if isinstance(state, dict) else []
        result["pending"] = any(str(item.get("archive")) == str(latest_local) for item in pending)
    except (OSError, ValueError, TypeError):
        result["pending"] = False

    telegram_enabled = bool(getattr(config, "BACKUP_TELEGRAM", True))
    yandex_enabled = bool(getattr(config, "YANDEX_DISK_ENABLED", False))
    delivery_ok = (not telegram_enabled or result["telegram_ok"]) and (not yandex_enabled or result["yandex_ok"])
    if result["pending"]:
        result["state"] = "warn"
        result["label"] = f"Ожидает доставки · {created_dt.strftime('%d.%m.%Y %H:%M')}"
    elif delivery_ok:
        result["state"] = "ok"
    else:
        result["state"] = "warn"
        result["label"] = f"Есть недоставленные · {created_dt.strftime('%d.%m.%Y %H:%M')}"
    return result


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    require_auth(request)
    # The overview page must remain renderable even when 3x-ui or an optional
    # local table is temporarily unavailable. Monitoring data is refreshed
    # client-side after the first paint, so a transient backend failure must
    # never turn the whole dashboard into HTTP 500.
    try:
        users, snapshot = live_users()
    except Exception as error:
        LOGGER.exception("Не удалось получить live snapshot для обзора: %s", error)
        try:
            users = _local_users()
        except Exception:
            users = []
        snapshot = {"stale": True, "error": str(error), "server_traffic_summary": {}, "traffic_summary": {}}
    now_ms = int(time.time() * 1000)
    total = len(users)
    active = sum(1 for item in users if item["active"])
    online_now = sum(1 for item in users if item.get("online"))
    expiring = sum(
        1 for item in users
        if item["active"] and item["expiry_time"] > 0
        and now_ms < item["expiry_time"] <= now_ms + 7 * 86_400_000
    )
    server_traffic = snapshot.get("server_traffic_summary") if isinstance(snapshot.get("server_traffic_summary"), dict) else {}
    inbound_traffic = snapshot.get("traffic_summary") if isinstance(snapshot.get("traffic_summary"), dict) else {}
    traffic_summary = server_traffic or inbound_traffic
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    bot_status = service_state("vpn-service-bot") == "active"
    web_status = service_state("vpn-service-web") == "active"
    xui_status = not bool(snapshot.get("stale"))
    xui_control = fetch_control_snapshot_sync()
    db_status = False
    backup_status = dashboard_backup_status()

    with database() as connection:
        connection.execute("SELECT 1").fetchone()
        db_status = True
        payment_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(payments)")}
        receipt_expr = "receipt_amount" if "receipt_amount" in payment_columns else "NULL"
        pending = int(connection.execute("SELECT count(*) FROM payments WHERE status='pending'").fetchone()[0] or 0)
        month_start = utc_sql_day_start_for_local(now_local().replace(day=1))
        revenue = float(connection.execute(
            f"SELECT COALESCE(SUM(COALESCE({receipt_expr},amount)),0) FROM payments WHERE status='approved' AND created_at>=?",
            (month_start,),
        ).fetchone()[0] or 0)
        payment_count = int(connection.execute("SELECT COUNT(*) FROM payments WHERE status='approved' AND created_at>=?", (month_start,)).fetchone()[0] or 0)
        user_events_exists = bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_events'").fetchone())
        today_start = utc_sql_day_start_for_local(now_local())
        messages_today = int(connection.execute(
            "SELECT COUNT(*) FROM user_events WHERE direction IN ('in','out') AND created_at>=?", (today_start,)
        ).fetchone()[0] or 0) if user_events_exists else 0
        recent_events = connection.execute(
            "SELECT tg_id,username,direction,text,created_at FROM user_events WHERE direction IN ('in','out') ORDER BY id DESC LIMIT 6"
        ).fetchall() if user_events_exists else []

        recent_audit = []
        try:
            audit_exists = bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone())
            if audit_exists:
                recent_audit = connection.execute(
                    "SELECT actor,action,details,created_at FROM audit_log ORDER BY id DESC LIMIT 5"
                ).fetchall()
        except sqlite3.Error as error:
            LOGGER.warning("Не удалось прочитать audit_log для обзора: %s", error)

    try:
        unread_snapshot = user_events.unread_messages_summary(db_path=config.DB_PATH)
    except Exception as error:
        LOGGER.exception("Не удалось получить непрочитанные сообщения для обзора: %s", error)
        unread_snapshot = {"total": 0, "users": 0, "items": [], "last_event_id": 0}
    unread_total = int(unread_snapshot.get("total") or 0)
    version = str(update_manager.current_version())

    def initials(name: str) -> str:
        parts = [part for part in name.replace("@", " ").split() if part]
        return (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")).upper() if parts else "?"

    def compact_text(value: str, limit: int = 74) -> str:
        value = " ".join(str(value or "").split()) or "Без текста"
        return html.escape(value if len(value) <= limit else value[:limit - 1] + "…")

    def status_dot(state: str, label: str) -> str:
        state = state if state in {"ok", "warn", "bad", "neutral"} else "neutral"
        return f'<span class="system-state {state}"><i></i>{html.escape(label)}</span>'

    message_items: list[str] = []
    for row in recent_events:
        name = str(row["username"] or f"id_{int(row["tg_id"])}")
        message_items.append(
            f'<a class="dash-message" href="/messages?tg_id={int(row["tg_id"])}">'
            f'<span class="avatar avatar-{(int(row["tg_id"]) % 5) + 1}">{html.escape(initials(name))}</span>'
            f'<span class="dash-message-copy"><strong>{html.escape(name.lstrip("@"))}</strong><small>{compact_text(row["text"])}</small></span>'
            f'<span class="dash-message-time">{html.escape(fmt_event_timestamp(row["created_at"], "%H:%M"))}</span></a>'
        )
    messages_html = "".join(message_items) or '<div class="empty-mini">Новых сообщений пока нет.</div>'

    activity_items: list[str] = []
    for row in recent_audit:
        detail = format_audit_details(row["details"]) or str(row["actor"] or "система")
        activity_items.append(
            f'<div><span class="activity-icon blue">•</span><span><b>{html.escape(audit_action_label(row["action"]))}</b><small>{html.escape(detail)}</small></span><time>{html.escape(fmt_audit_timestamp(row["created_at"])[11:16])}</time></div>'
        )
    activity_html = "".join(activity_items) or '<div class="empty-mini">Записей активности пока нет.</div>'

    source_notice = ""
    if snapshot.get("stale"):
        source_notice = f'<div class="notice"><strong>3x-ui API временно недоступна.</strong> Показан локальный кэш. Причина: {html.escape(str(snapshot.get("error") or "неизвестно"))}</div>'

    configured_xui_url = str(getattr(config, "XUI_PANEL_URL", "") or "").strip().rstrip("/")
    quick_xui = f'<a class="quick-action xui" href="{html.escape(configured_xui_url, quote=True)}" target="_blank" rel="noopener noreferrer"><span class="quick-icon">◇</span><span><b>Открыть 3x-ui</b><small>Панель сервера</small></span></a>' if configured_xui_url else ''
    quick_actions = (
        '<div class="quick-actions-grid">'
        '<a class="quick-action purple" href="/users/new"><span class="quick-icon">＋</span><span><b>Добавить пользователя</b><small>Создать нового пользователя</small></span></a>'
        '<a class="quick-action blue" href="/messages"><span class="quick-icon">✉</span><span><b>Отправить сообщение</b><small>Написать пользователю</small></span></a>'
        '<a class="quick-action green" href="/broadcast"><span class="quick-icon">➤</span><span><b>Создать рассылку</b><small>Отправить сообщение всем</small></span></a>'
        '<a class="quick-action gold" href="/backups"><span class="quick-icon">□</span><span><b>Резервная копия</b><small>Создать бэкап данных</small></span></a>'
        '<a class="quick-action magenta" href="/updates"><span class="quick-icon">↥</span><span><b>Проверить обновления</b><small>Поиск новой версии</small></span></a>'
        f'{quick_xui}'
        '</div>'
    )

    nav_unread = f'<span class="header-counter">{unread_total}</span>' if unread_total else ''
    body = f"""<header class="dash-header"><div><div class="eyebrow">VPN Service Platform</div><h1>Обзор</h1><div class="subtitle">Фактические показатели сервера и пользователей · обновляется автоматически</div></div><div class="header-tools"><label class="global-search"><span>⌕</span><input type="search" placeholder="Поиск пользователей, сообщений…" autocomplete="off" aria-label="Поиск по панели" data-panel-search></label><a class="header-icon" href="/settings" aria-label="Настройки">◐</a><a class="header-icon has-counter" href="/messages" aria-label="Сообщения">♧{nav_unread}</a><a class="header-icon" href="/logout" aria-label="Выйти">↪</a></div></header>{source_notice}
<div class="dash-kpis">
<div class="dash-kpi purple"><span class="kpi-icon">♙</span><div><span class="kpi-label">Всего пользователей</span><strong>{total:,}</strong><small>Активных: {active}</small></div></div>
<div class="dash-kpi blue"><span class="kpi-icon">♕</span><div><span class="kpi-label">Активные подписки</span><strong>{active:,}</strong><small>Истекают за 7 дней: {expiring}</small></div></div>
<div class="dash-kpi green"><span class="kpi-icon">↗</span><div><span class="kpi-label">Доход (месяц)</span><strong>{revenue:,.0f} ₽</strong><small>Подтверждено платежей: {payment_count}</small></div></div>
<div class="dash-kpi gold"><span class="kpi-icon">▱</span><div><span class="kpi-label">Сообщения (сегодня)</span><strong>{messages_today}</strong><small>Непрочитанных: {unread_total}</small></div></div>
</div>
<div class="dash-layout">
<section class="dash-main-col">
<div class="card dash-card server-resources-main"><div class="dash-section-head"><div><h2>Ресурсы сервера</h2><span>Текущая нагрузка в реальном времени</span></div><a href="/monitoring">Подробный мониторинг →</a></div><div class="resources-grid"><div class="resource-box"><div class="resource-box-head"><span>CPU</span><b id="dash-cpu-main">{cpu:.0f}%</b></div><div class="resource-bar large"><span id="dash-cpu-main-bar" style="width:{cpu}%"></span></div></div><div class="resource-box"><div class="resource-box-head"><span>RAM</span><b id="dash-ram-main">{ram:.0f}%</b></div><div class="resource-bar large"><span id="dash-ram-main-bar" style="width:{ram}%"></span></div></div><div class="resource-box"><div class="resource-box-head"><span>Диск</span><b id="dash-disk-main">{disk:.0f}%</b></div><div class="resource-bar large"><span id="dash-disk-main-bar" style="width:{disk}%"></span></div></div><div class="resource-box network"><div class="resource-box-head"><span>Скорость сети</span><b><span id="dash-net-down-main">↓ —</span> · <span id="dash-net-up-main">↑ —</span></b></div><div class="network-caption"><span>Входящая</span><span>Исходящая</span></div></div></div><div class="server-resource-footer"><span>Обновляется автоматически</span><time id="dash-metrics-updated-main">Сейчас</time></div></div>
<div class="card dash-card quick-card"><div class="dash-section-head"><div><h2>Быстрые действия</h2><span>Основные операции администратора</span></div></div>{quick_actions}</div>
<div class="dash-two-col compact-messages-row">
<div class="card dash-card messages-card compact-messages-card"><div class="dash-section-head"><div><h2>Последние сообщения</h2><span>Свежая переписка пользователей</span></div><a href="/messages">Все сообщения →</a></div><div class="dash-messages">{messages_html}</div></div>
</div>
</section>
<div class="dashboard-rail">
<div class="card dash-card system-card"><div class="dash-section-head"><div><h2>Состояние системы</h2><span>Живой статус служб и резервного копирования</span></div><time id="system-status-updated">Сейчас</time></div><div class="system-list">
<div><span>Бот</span><span class="system-state {"ok" if bot_status else "bad"}" id="system-bot-state"><i></i>{"Работает" if bot_status else "Остановлен"}</span></div>
<div><span>Веб-панель</span><span class="system-state {"ok" if web_status else "bad"}" id="system-web-state"><i></i>{"Работает" if web_status else "Остановлена"}</span></div>
<div><span>3x-ui панель</span><span class="system-state {"ok" if xui_status else "bad"}" id="system-xui-state"><i></i>{"Работает" if xui_status else "Нет связи"}</span></div>
<div><span>Xray</span><span class="system-state {"ok" if str(xui_control.get("status",{}).get("xray_state")) in {"running","running (pid 0)"} else "neutral"}" id="system-xray-state"><i></i>{html.escape(str(xui_control.get("status",{}).get("xray_state") or "нет данных"))}</span></div>
<div><span>Соединения 3x-ui</span><span class="system-state neutral" id="system-tcp-state"><i></i>{int(xui_control.get("status",{}).get("tcp_count") or 0)}</span></div>
<div><span>База данных</span><span class="system-state {"ok" if db_status else "bad"}" id="system-db-state"><i></i>{"Работает" if db_status else "Ошибка"}</span></div>
<div><span>Резервные копии</span><span class="system-state {html.escape(str(backup_status["state"]))}" id="system-backup-state"><i></i>{html.escape(str(backup_status["label"]))}</span></div>
<div><span>Версия</span><span class="system-state neutral" id="system-version-state"><i></i>v{html.escape(version)}</span></div>
</div></div>
<div class="card dash-card activity-card"><div class="dash-section-head"><div><h2>Недавняя активность</h2><span>Последние действия в панели</span></div><a href="/audit">Вся активность →</a></div><div class="activity-list">{activity_html}</div></div>
</div></div>
<div class="card dash-card stats-bottom"><div class="dash-section-head"><div><h2>Текущая статистика</h2><span>Только фактические значения</span></div><span class="range-select">Сейчас</span></div><div class="stat-grid"><div><span class="activity-icon blue">♙</span><span>Всего пользователей</span><b>{total}</b></div><div><span class="activity-icon purple">♕</span><span>Активные подписки</span><b>{active}</b></div><div><span class="activity-icon gold">₽</span><span>Подтверждённые платежи</span><b>{payment_count}</b></div><div><span class="activity-icon green">▣</span><span>Выручка за месяц</span><b>{revenue:,.0f} ₽</b></div><div><span class="activity-icon blue">↗</span><span>Доля активных</span><b>{(active / total * 100) if total else 0:.0f}%</b></div></div></div>
</div>"""
    script = """<script>
(function(){
function setMetric(id,value){const node=document.getElementById(id);if(node)node.textContent=Math.round(Number(value)||0)+'%'}
function setBar(id,value){const node=document.getElementById(id);if(node)node.style.width=Math.min(100,Math.max(0,Number(value)||0))+'%'}
function setText(id,value){const node=document.getElementById(id);if(node)node.textContent=String(value??'—')}
async function dashboardTick(){try{const response=await fetch('/api/metrics',{cache:'no-store',credentials:'same-origin',headers:{Accept:'application/json'}});if(!response.ok)throw new Error('metrics');const d=await response.json();setMetric('dash-cpu-main',d.cpu);setMetric('dash-ram-main',d.ram);setMetric('dash-disk-main',d.disk);setBar('dash-cpu-main-bar',d.cpu);setBar('dash-ram-main-bar',d.ram);setBar('dash-disk-main-bar',d.disk);setText('dash-net-down-main','↓ '+(d.net_down||'0 Б/с'));setText('dash-net-up-main','↑ '+(d.net_up||'0 Б/с'));const now=new Date().toLocaleTimeString();const stamp=document.getElementById('dash-metrics-updated');if(stamp)stamp.textContent='Обновлено '+now;const stampMain=document.getElementById('dash-metrics-updated-main');if(stampMain)stampMain.textContent='Обновлено '+now;}catch(e){const stamp=document.getElementById('dash-metrics-updated');if(stamp)stamp.textContent='Нет свежих данных';const stampMain=document.getElementById('dash-metrics-updated-main');if(stampMain)stampMain.textContent='Нет свежих данных'}}
async function loop(){await dashboardTick();window.setTimeout(loop,3000)}
loop();
async function refreshSystemStatus(){try{const response=await fetch('/api/system-status',{cache:'no-store',credentials:'same-origin',headers:{Accept:'application/json'}});if(!response.ok)throw new Error('system-status');const d=await response.json();const state=(ok,on,off)=>ok?['ok',on]:['bad',off];const apply=(id,pair)=>{const node=document.getElementById(id);if(!node)return;node.className='system-state '+pair[0];node.innerHTML='<i></i>'+pair[1]};apply('system-bot-state',state(!!d.bot,'Работает','Остановлен'));apply('system-web-state',state(!!d.web,'Работает','Остановлена'));apply('system-xui-state',state(!!d.xui,'Работает','Нет связи'));const xray=document.getElementById('system-xray-state');if(xray){const running=['running','running (pid 0)'].includes(String(d.xray||''));xray.className='system-state '+(running?'ok':'neutral');xray.innerHTML='<i></i>'+(running?'Работает':String(d.xray||'Нет данных'))}const tcp=document.getElementById('system-tcp-state');if(tcp)tcp.innerHTML='<i></i>'+String(d.tcp_count??'0');apply('system-db-state',state(!!d.database,'Работает','Ошибка'));const backup=document.getElementById('system-backup-state');if(backup){backup.className='system-state '+(d.backup?.state||'neutral');backup.innerHTML='<i></i>'+String(d.backup?.label||'Нет данных')}const version=document.getElementById('system-version-state');if(version){version.className='system-state neutral';version.innerHTML='<i></i>v'+String(d.version||'—')}const stamp=document.getElementById('system-status-updated');if(stamp)stamp.textContent='Обновлено '+new Date().toLocaleTimeString()}catch(e){const stamp=document.getElementById('system-status-updated');if(stamp)stamp.textContent='Нет свежих данных'}}
refreshSystemStatus();window.setInterval(refreshSystemStatus,10000);
const search=document.querySelector('[data-panel-search]');if(search){search.addEventListener('keydown',e=>{if(e.key==='Enter'&&search.value.trim())window.location.href='/users?q='+encodeURIComponent(search.value.trim())})}
})();
</script>"""
    return page(request, "Обзор", body, "dashboard", script)


@app.get("/api/panel/update-status")
def panel_update_status(request: Request):
    require_auth(request)
    info = update_manager.check_available_update()
    return {
        "available": bool(info.get("available")),
        "version": str(info.get("version") or ""),
        "installed_version": str(info.get("installed_version") or update_manager.current_version()),
        "error": str(info.get("error") or ""),
    }


def _database_health() -> bool:
    try:
        with database() as connection:
            return connection.execute("SELECT 1").fetchone() is not None
    except Exception:
        return False


@app.get("/api/system-status")
def system_status_api(request: Request):
    require_auth(request)
    backup = dashboard_backup_status()
    # Frequent status polling is intentionally cache-only. A 10s browser poll
    # must not trigger four upstream 3x-ui calls plus a SQLite sync transaction
    # whenever the 15s client cache expires. The next page request/sync path can
    # still refresh the authoritative snapshot.
    snapshot = snapshot_cache_status()
    snapshot_stale = bool(snapshot.get("stale"))
    control = fetch_control_snapshot_sync()
    control_status = control.get("status") if isinstance(control.get("status"), dict) else {}
    fail2ban = control.get("fail2ban") if isinstance(control.get("fail2ban"), dict) else {}
    nodes = control.get("nodes") if isinstance(control.get("nodes"), list) else []
    return JSONResponse({
        "bot": service_state("vpn-service-bot") == "active",
        "web": service_state("vpn-service-web") == "active",
        "xui": not snapshot_stale,
        "xray": str(control_status.get("xray_state") or "unknown"),
        "xray_version": str(control_status.get("xray_version") or ""),
        "tcp_count": int(control_status.get("tcp_count") or 0),
        "load1": float(control_status.get("load1") or 0),
        "fail2ban": fail2ban,
        "nodes": nodes,
        "database": _database_health(),
        "backup": {"state": backup["state"], "label": backup["label"]},
        "version": str(update_manager.current_version()),
        "sampled_at": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/xui-telemetry")
def xui_telemetry_api(request: Request):
    """Expose only non-secret 3x-ui runtime telemetry to authenticated admins."""
    require_auth(request)
    data = fetch_control_snapshot_sync()
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    return JSONResponse({
        "sampled_at": datetime.fromtimestamp(float(data.get("ts") or time.time()), tz=timezone.utc).isoformat(),
        "status": status,
        "fail2ban": data.get("fail2ban") or {},
        "xray_metrics": data.get("xray_metrics") or {},
        "observatory": data.get("observatory") or {},
        "nodes": data.get("nodes") or [],
        "error": str(data.get("error") or ""),
    }, headers={"Cache-Control":"no-store"})


@app.get("/api/metrics")
def metrics_api(request: Request):
    require_auth(request)
    global NET_CACHE
    counters = psutil.net_io_counters()
    now = time.time()
    elapsed = max(now - NET_CACHE["ts"], 0.1)
    down = max(0, (counters.bytes_recv - NET_CACHE["in"]) / elapsed)
    up = max(0, (counters.bytes_sent - NET_CACHE["out"]) / elapsed)
    NET_CACHE = {"ts": now, "in": counters.bytes_recv, "out": counters.bytes_sent}
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    global METRICS_LAST_WRITE
    store_interval = max(10, int(getattr(config, "METRICS_STORE_INTERVAL_SECONDS", 60)))
    if now - METRICS_LAST_WRITE >= store_interval:
        try:
            with database() as connection:
                connection.execute(
                    "INSERT INTO metrics(cpu,ram,disk,net_in,net_out) VALUES(?,?,?,?,?)",
                    (cpu, ram, disk, int(down), int(up)),
                )
                connection.execute(
                    "DELETE FROM metrics WHERE id NOT IN (SELECT id FROM metrics ORDER BY id DESC LIMIT 5000)"
                )
            METRICS_LAST_WRITE = now
        except Exception:
            pass
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk_info = psutil.disk_usage("/")
    try:
        load = list(psutil.getloadavg() if hasattr(psutil, "getloadavg") else os.getloadavg())
    except (AttributeError, OSError):
        load = [0.0, 0.0, 0.0]
    return {
        "cpu": cpu,
        "ram": ram,
        "ram_used": vm.used,
        "ram_total": vm.total,
        "disk": disk,
        "disk_free": disk_info.free,
        "swap": swap.percent,
        "swap_used": swap.used,
        "swap_total": swap.total,
        "load1": load[0],
        "load5": load[1],
        "load15": load[2],
        "net_down": f"{fmt_bytes(down)}/с",
        "net_up": f"{fmt_bytes(up)}/с",
        "sampled_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/online-metrics")
def online_metrics_api(request: Request):
    """Return live 3x-ui online count plus a rolling short history for the dashboard graph."""
    require_auth(request)
    global ONLINE_METRICS_CACHE
    now = time.time()
    try:
        # Reuse the same authoritative 3x-ui snapshot used by Users/Overview.
        # Do not issue another onlines request every few seconds: that doubles
        # pressure on 3x-ui and can make small VPS instances unresponsive.
        snapshot = fetch_snapshot_sync(force=False)
        online = snapshot.get("online") if isinstance(snapshot, dict) else set()
        online_count = len(online) if isinstance(online, (set, list, tuple)) else 0
        online_count = max(0, int(online_count))
        ONLINE_METRICS_CACHE = {"ts": now, "count": online_count, "error": str(snapshot.get("error") or ""), "stale": bool(snapshot.get("stale"))}
        stamp = datetime.now(timezone.utc)
        with ONLINE_HISTORY_LOCK:
            if not ONLINE_HISTORY or now - float(ONLINE_HISTORY[-1]["ts"]) >= 1.0:
                ONLINE_HISTORY.append({"ts": now, "count": online_count, "time": stamp.astimezone().strftime("%H:%M:%S")})
            history = list(ONLINE_HISTORY)
    except Exception as exc:
        ONLINE_METRICS_CACHE = {"ts": now, "count": ONLINE_METRICS_CACHE["count"], "error": str(exc), "stale": True}
        with ONLINE_HISTORY_LOCK:
            history = list(ONLINE_HISTORY)
    return {
        "count": ONLINE_METRICS_CACHE["count"],
        "stale": ONLINE_METRICS_CACHE["stale"],
        "error": ONLINE_METRICS_CACHE["error"],
        "sampled_at": ONLINE_METRICS_CACHE["ts"],
        "history": history[-180:],
    }


def _telegram_bot_username() -> str:
    token = str(getattr(config, "BOT_TOKEN", "") or "").strip()
    if not token:
        return ""
    try:
        response = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=6.0, trust_env=False)
        payload = response.json()
        username = str((payload.get("result") or {}).get("username") or "").strip().lstrip("@")
        return username
    except Exception:
        return ""


def create_telegram_link_request(local_tg_id: int) -> dict[str, Any]:
    local_tg_id = int(local_tg_id)
    if local_tg_id >= 0:
        raise ValueError("Ссылка привязки доступна только для локального пользователя без Telegram")
    token = secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:32]
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=30)
    with database() as connection:
        connection.execute(
            "DELETE FROM telegram_link_requests WHERE expires_at < ?",
            (now.isoformat(timespec="seconds"),),
        )
        connection.execute(
            "INSERT INTO telegram_link_requests(token,local_tg_id,created_at,expires_at) VALUES(?,?,?,?)",
            (token, local_tg_id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
        )
    bot_username = _telegram_bot_username()
    if not bot_username:
        raise RuntimeError("Не удалось определить username Telegram-бота")
    return {"token": token, "expires_at": expires.isoformat(timespec="seconds"), "deep_link": f"https://t.me/{bot_username}?start=bind_{token}"}


def telegram_link_request_status(token: str) -> dict[str, Any]:
    with database() as connection:
        row = connection.execute(
            "SELECT token,local_tg_id,expires_at,consumed_at,resolved_tg_id FROM telegram_link_requests WHERE token=?",
            (str(token),),
        ).fetchone()
    if not row:
        return {"status": "missing"}
    if row[3] and int(row[4] or 0) > 0:
        return {"status": "resolved", "tg_id": int(row[4]), "local_tg_id": int(row[1])}
    if str(row[2]) < datetime.now(timezone.utc).isoformat(timespec="seconds"):
        return {"status": "expired", "local_tg_id": int(row[1])}
    return {"status": "pending", "local_tg_id": int(row[1]), "expires_at": str(row[2])}


def telegram_username_lookup(username: str) -> list[dict[str, Any]]:
    """Find a known Telegram identity by username across all local identity logs.

    Telegram's Bot API cannot resolve an arbitrary private user from @username alone,
    so this function deliberately searches only identities that this installation has
    already received from Telegram or stored in its own database.
    """
    clean = str(username or "").strip().lstrip("@").casefold()
    if not clean:
        return []

    with database() as connection:
        found: dict[int, dict[str, Any]] = {}

        def add_match(tg_id: Any, value_username: Any = "", email: Any = "", enable: Any = 1, source: str = "local") -> None:
            try:
                normalized_id = int(tg_id or 0)
            except (TypeError, ValueError):
                normalized_id = 0
            if normalized_id <= 0:
                return
            candidate = {
                "tg_id": normalized_id,
                "username": str(value_username or clean).strip().lstrip("@"),
                "email": str(email or ""),
                "enable": int(enable or 0),
                "source": source,
            }
            previous = found.get(normalized_id)
            if previous is None or (not previous.get("username") and candidate["username"]):
                found[normalized_id] = candidate

        tables = [
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]

        for table in tables:
            try:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = {
                    str(r[1]) for r in connection.execute(f"PRAGMA table_info({quoted})").fetchall()
                }
            except Exception:
                continue
            if "username" not in columns or "tg_id" not in columns:
                continue

            select_columns = ["tg_id", "username"]
            if "email" in columns:
                select_columns.append("email")
            if "enable" in columns:
                select_columns.append("enable")
            select_sql = ", ".join(select_columns)
            try:
                rows = connection.execute(
                    f"SELECT {select_sql} FROM {quoted} "
                    "WHERE tg_id>0 AND LOWER(REPLACE(COALESCE(username,''),'@',''))=? "
                    "ORDER BY tg_id DESC",
                    (clean,),
                ).fetchall()
            except Exception:
                continue
            for row in rows:
                add_match(
                    row["tg_id"],
                    row["username"],
                    row["email"] if "email" in columns else "",
                    row["enable"] if "enable" in columns else 1,
                    source=table,
                )

        # Some older event records stored the username only in actor=telegram:@name.
        for table in tables:
            try:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = {str(r[1]) for r in connection.execute(f"PRAGMA table_info({quoted})").fetchall()}
            except Exception:
                continue
            if "tg_id" not in columns or "actor" not in columns:
                continue
            try:
                rows = connection.execute(
                    f"SELECT tg_id, actor FROM {quoted} WHERE tg_id>0 "
                    "AND LOWER(actor)=? ORDER BY tg_id DESC",
                    (f"telegram:@{clean}",),
                ).fetchall()
            except Exception:
                continue
            for row in rows:
                add_match(row["tg_id"], clean, source=table + ":actor")

        try:
            snapshot = fetch_snapshot_sync(False)
            for client in (snapshot.get("clients") or snapshot.get("all_clients") or []):
                try:
                    candidate_id = int(client.get("tg_id") or client.get("tgId") or 0)
                except Exception:
                    candidate_id = 0
                email_value = str(client.get("email") or "").strip().lstrip("@").casefold()
                if candidate_id > 0 and (email_value == clean or email_value.startswith(clean + "_")):
                    add_match(candidate_id, clean, enable=client.get("enable", 1), source="3x-ui")
        except Exception:
            pass
        return sorted(found.values(), key=lambda item: int(item["tg_id"]))


def update_topology_public(report: dict[str, Any], publisher: bool) -> dict[str, Any]:
    redacted = dict(report)
    if not publisher:
        redacted["github_repository"] = ""
        redacted["github_release_url"] = ""
    return redacted


@app.get("/api/telegram/lookup")
def telegram_lookup_api(request: Request, username: str = ""):
    require_auth(request)
    clean = str(username or "").strip().lstrip("@")[:100]
    if not clean:
        raise HTTPException(400, "Укажите Telegram @username")
    matches = telegram_username_lookup(clean)
    return {
        "username": clean,
        "matches": [{"tg_id": int(row.get("tg_id") or 0), "username": str(row.get("username") or ""), "email": str(row.get("email") or ""), "telegram_connected": int(row.get("tg_id") or 0) > 0, "enabled": bool(row.get("enable"))} for row in matches],
        "note": "Найденные ID берутся из локальной истории Telegram-взаимодействий. Telegram Bot API не позволяет надёжно получить ID произвольного личного пользователя только по @username.",
    }


@app.post("/api/telegram/link-request")
def telegram_link_request_api(request: Request, tg_id: int = Form(...)):
    require_auth(request)
    row = get_user(int(tg_id))
    if not row:
        raise HTTPException(404, "Пользователь не найден")
    try:
        return create_telegram_link_request(int(tg_id))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(502, str(error)) from error


@app.get("/api/telegram/link-request/{token}")
def telegram_link_request_status_api(request: Request, token: str):
    require_auth(request)
    return telegram_link_request_status(token)


@app.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    q: str = "",
    status: str = "all",
    sort: str = "remaining",
    order: str = "asc",
):
    require_auth(request)
    try:
        users, snapshot = live_users()
    except Exception as error:
        LOGGER.exception("Критическая ошибка построения списка пользователей")
        users = []
        snapshot = {"stale": True, "error": str(error)}
        try:
            users = _local_users()
        except Exception:
            users = []
    # Enrich each live row with referral ownership from the bot DB.  This does not
    # alter 3x-ui data and keeps the referral chain visible to the administrator.
    try:
        with sqlite3.connect(config.DB_PATH, timeout=10) as _conn:
            _conn.row_factory = sqlite3.Row
            _ref_rows = _conn.execute("SELECT u.tg_id,u.referred_by_tg_id,COALESCE(r.username,'') AS referred_by_username FROM users u LEFT JOIN users r ON r.tg_id=u.referred_by_tg_id WHERE u.tg_id>0").fetchall()
        _ref_map = {int(r['tg_id']): dict(r) for r in _ref_rows}
        for _user in users:
            _extra = _ref_map.get(int(_user.get('tg_id') or 0), {})
            _user['referred_by_tg_id'] = int(_extra.get('referred_by_tg_id') or 0)
            _user['referred_by_username'] = str(_extra.get('referred_by_username') or '')
    except Exception as _error:
        LOGGER.debug("Referral enrichment skipped: %s", _error)
    # Filtering and sorting are intentionally performed in the browser. The page already
    # renders the same user set used by the admin, so changing controls is instant and
    # does not trigger a full navigation or lose the current scroll position. URL params
    # are still accepted and used as the initial state for deep links/bookmarks.
    sort = sort if sort in {"remaining", "last_online", "traffic", "quota", "name"} else "remaining"
    order = order if order in ("asc", "desc") else "asc"

    now_ms = int(time.time() * 1000)
    try:
        unread_snapshot = user_events.unread_messages_summary(db_path=config.DB_PATH)
        unread_map = {
            int(item.get("tg_id") or 0): item
            for item in (unread_snapshot.get("items") or [])
            if int(item.get("tg_id") or 0) > 0
        }
    except Exception as error:
        LOGGER.warning("Не удалось получить непрочитанные сообщения для списка пользователей: %s", error)
        unread_map = {}
    cards: list[str] = []
    for item in users[:2000]:
        tg_id = int(item.get("tg_id") or 0)
        unread = unread_map.get(tg_id, {})
        unread_count = max(0, int(unread.get("count") or 0))
        unread_preview = " ".join(str(unread.get("preview") or "").split())[:180]
        preview_hidden = "" if unread_count and unread_preview else "hidden"
        quota = "∞" if item["quota_remaining"] is None else fmt_bytes(item["quota_remaining"])
        display_name = str(item.get("username") or item.get("email") or "Без Telegram-привязки")
        base_button_label = "Открыть чат" if tg_id > 0 else "Открыть карточку"
        button_label = base_button_label
        message_button = (
            f'<a class="button small secondary" href="/users/id/{tg_id}" '
            f'data-chat-button data-base-label="{base_button_label}">{button_label}</a>'
        )
        row_status = "active" if item["active"] else ("blocked" if not item["enable"] else "expired")
        referred_by_tg_id = int(item.get("referred_by_tg_id") or 0)
        referred_by_username = str(item.get("referred_by_username") or "").strip()
        referral_note = ""
        if referred_by_tg_id > 0:
            ref_label = f"@{referred_by_username}" if referred_by_username else f"TG {referred_by_tg_id}"
            referral_note = f" · пригласил: {html.escape(ref_label)}"
        search_blob = " ".join([str(display_name), str(item.get("email") or ""), str(tg_id), referred_by_username, str(referred_by_tg_id)]).casefold()
        quota_value = item.get("quota_remaining")
        try:
            quota_number = float(quota_value) if quota_value is not None else None
        except (TypeError, ValueError, OverflowError):
            quota_number = None
        quota_sort = str(int(quota_number)) if quota_number is not None and math.isfinite(quota_number) else "-1"
        remaining_value = item.get("remaining_days")
        try:
            remaining_number = float(remaining_value)
        except (TypeError, ValueError, OverflowError):
            remaining_number = 0.0
        if math.isinf(remaining_number) and remaining_number > 0:
            remaining_sort = 2147483647
        elif math.isfinite(remaining_number):
            remaining_sort = int(remaining_number)
        else:
            remaining_sort = 0
        cards.append(
            f'''<article class="user-row" data-user-tg-id="{tg_id}" data-user-search="{html.escape(search_blob, quote=True)}" data-user-status="{row_status}" data-user-online="{1 if item.get('online') else 0}" data-user-remaining="{remaining_sort}" data-user-last-online="{int(item.get('last_online_ts') or 0)}" data-user-traffic="{int(item.get('traffic_used') or 0)}" data-user-quota="{html.escape(quota_sort, quote=True)}" data-user-name="{html.escape(display_name.casefold(), quote=True)}">
<div class="user-main"><div class="avatar avatar-{(tg_id % 5) + 1}">{html.escape((display_name.strip('@ ')[:2] or '?').upper())}</div><div class="user-main-copy"><div class="user-name-line"><strong>{html.escape(display_name)}</strong></div><div class="meta">TG: {tg_id if tg_id > 0 else "не привязан"} · {html.escape(str(item.get("email") or "—"))}{referral_note}</div></div></div>
<div class="user-status">{status_badge(item["active"])}<span class="muted">{html.escape(fmt_last_online(item.get("last_online_ts"), item.get("online", False)))}</span></div>
<div class="user-stat"><small>Срок</small><b>{remaining_label(item["expiry_time"], now_ms)}</b><span>{fmt_date(item["expiry_time"])}</span></div>
<div class="user-stat"><small>Трафик</small><b>{fmt_bytes(item["traffic_used"])}</b><span>↑ {fmt_bytes(item["up"])} · ↓ {fmt_bytes(item["down"])}</span></div>
<div class="user-stat"><small>Лимит</small><b>{quota}</b><span>{"безлимит" if item["quota_remaining"] is None else "осталось"}</span></div>
<div class="user-actions"><a class="button small secondary" href="/users/id/{tg_id}" data-chat-button data-base-label="{base_button_label}">{button_label}</a><details class="row-more"><summary aria-label="Дополнительные действия">•••</summary><div class="row-more-menu"><form class="inline" method="post" action="/users/{tg_id}/days"><input name="days" type="number" value="30" min="-3650" max="3650" step="1" aria-label="Изменение срока в днях" title="Изменить дни"><button class="small" title="Изменить дни">Срок</button></form><form method="post" action="/users/{tg_id}/toggle"><button class="small secondary">{"Блокировать" if item["enable"] else "Включить"}</button></form><form method="post" action="/users/{tg_id}/delete" onsubmit="return confirm('Удалить пользователя из базы и 3x-ui?')"><button class="small danger">Удалить</button></form></div></details></div>
</article>'''
        )
    source_notice = ""
    if snapshot.get("stale"):
        source_notice = f'<div class="notice">Показан локальный кэш: {html.escape(str(snapshot.get("error") or "3x-ui API недоступна"))}</div>'
    body = f'''<header><div><h1>Пользователи</h1><div class="subtitle">Актуальные сроки, трафик, Telegram-привязки и история общения · {api_source_badge(bool(snapshot.get("stale")))}</div></div><div class="actions"><form id="subscription-refresh-form" class="inline" method="post" action="/users/refresh-subscription-links"><input type="hidden" name="csrf_token" value="{html.escape(session_csrf_token(request), quote=True)}"><button id="subscription-refresh-start" class="secondary" type="submit">↻ Обновить ссылки</button></form><a class="button secondary" href="/broadcast">✉ Массовая рассылка</a><a class="button secondary" href="/users/import-identities">Импорт привязок</a><form class="inline" method="post" action="/users/import-to-3xui" onsubmit="return confirm('Синхронизировать зарегистрированных Telegram-пользователей с текущим 3x-ui?')"><button class="secondary" type="submit">⬆ Загрузить в 3x-ui</button></form><a class="button" href="/users/new">＋ Создать пользователя</a></div></header><section id="subscription-refresh-panel" class="card subscription-refresh-panel" style="display:none"><div class="section-title"><h2>Обновление ссылок подписки</h2><span id="subscription-refresh-badge" class="badge">Ожидание</span></div><div class="subscription-refresh-progress"><div class="progress large"><span id="subscription-refresh-progress-bar" style="width:0%"></span></div><div class="progress-meta"><span id="subscription-refresh-progress-text">Подготовка…</span><span id="subscription-refresh-progress-percent">0%</span></div></div><div class="subscription-refresh-stats"><span>Обработано <b id="subscription-refresh-processed">0</b></span><span>Отправлено <b id="subscription-refresh-delivered">0</b></span><span>Пропущено <b id="subscription-refresh-skipped">0</b></span><span>Ошибок <b id="subscription-refresh-failed">0</b></span></div><div class="subscription-refresh-current" id="subscription-refresh-current"></div><details class="subscription-refresh-log" open><summary>Журнал выполнения</summary><pre id="subscription-refresh-log-body">Ожидание запуска…</pre></details></section><div id="subscription-refresh-status" class="notice" style="display:none"></div>{source_notice}

<div class="toolbar"><div class="toolbar-filter" role="search" data-user-filter-form data-initial-status="{html.escape(status, quote=True)}" data-initial-sort="{html.escape(sort, quote=True)}" data-initial-order="{html.escape(order, quote=True)}" data-initial-query="{html.escape(q, quote=True)}" autocomplete="off"><label class="sr-only" for="user-filter-search">Поиск пользователя</label><input id="user-filter-search" value="{html.escape(q)}" placeholder="Имя, TG ID, @username или email" type="search" inputmode="search" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" aria-autocomplete="none" enterkeyhint="search" data-1p-ignore="true" data-lpignore="true" data-bwignore="true" data-form-type="other" data-purpose="user-search" data-testid="user-search"><select name="status" data-filter-status><option value="all">Все</option><option value="active" {'selected' if status=='active' else ''}>Активные</option><option value="expired" {'selected' if status=='expired' else ''}>Истёкшие</option><option value="blocked" {'selected' if status=='blocked' else ''}>Заблокированные</option><option value="online" {'selected' if status=='online' else ''}>Сейчас онлайн</option></select><select name="sort" data-filter-sort><option value="remaining" {'selected' if sort=='remaining' else ''}>По остатку срока</option><option value="last_online" {'selected' if sort=='last_online' else ''}>По последнему онлайн</option><option value="traffic" {'selected' if sort=='traffic' else ''}>По объёму трафика</option><option value="quota" {'selected' if sort=='quota' else ''}>По остатку лимита</option><option value="name" {'selected' if sort=='name' else ''}>По имени</option></select><select name="order" data-filter-order><option value="asc" {'selected' if order=='asc' else ''}>По возрастанию</option><option value="desc" {'selected' if order=='desc' else ''}>По убыванию</option></select></div></div><div class="users-list-summary"><span id="users-visible-count">0</span> из <span id="users-total-count">{len(cards)}</span> пользователей</div><div class="user-list">{''.join(cards) or '<div class="card">Пользователи не найдены.</div>'}</div>'''
    scripts = """<script>(function(){const panel=document.getElementById('subscription-refresh-panel'),box=document.getElementById('subscription-refresh-status'),form=document.getElementById('subscription-refresh-form'),start=document.getElementById('subscription-refresh-start');const badge=document.getElementById('subscription-refresh-badge'),bar=document.getElementById('subscription-refresh-progress-bar'),pct=document.getElementById('subscription-refresh-progress-percent'),pt=document.getElementById('subscription-refresh-progress-text'),processed=document.getElementById('subscription-refresh-processed'),delivered=document.getElementById('subscription-refresh-delivered'),skipped=document.getElementById('subscription-refresh-skipped'),failed=document.getElementById('subscription-refresh-failed'),current=document.getElementById('subscription-refresh-current'),log=document.getElementById('subscription-refresh-log-body');const setText=(el,v)=>{if(el)el.textContent=v==null?'':String(v)};const render=(d)=>{if(!d||!d.state)return;const busy=['queued','running'].includes(d.state);if(panel)panel.style.display='block';const p=Math.max(0,Math.min(100,Number(d.progress)||0));if(bar)bar.style.width=p+'%';setText(pct,p+'%');setText(pt,d.message||'');setText(processed,(d.processed||0)+' / '+(d.total||0));setText(delivered,d.delivered||0);setText(skipped,d.skipped||0);setText(failed,d.failed||0);const badgeText=d.state==='completed'?'Завершено':d.state==='failed'?'Ошибка':d.state==='queued'?'Запущено':'Выполняется';setText(badge,badgeText);if(badge)badge.className='badge '+(d.state==='failed'?'bad':(d.state==='completed'?'good':''));setText(current,d.current_user?('Текущий пользователь: '+d.current_user+(d.last_action?' · '+d.last_action:'')):'');const entries=Array.isArray(d.recent_log)?d.recent_log:[];setText(log,entries.length?entries.join('\\n'):(d.error||'Ожидание запуска…'));if(box){box.textContent=d.error&&d.state==='failed'?'Ошибка запуска: '+d.error:(d.state==='completed'?d.message:'');box.className='notice '+(d.state==='failed'?'bad':'');box.style.display=(d.state==='failed'||d.state==='completed')?'block':'none';}if(start){start.disabled=busy;start.textContent=busy?'⏳ Рассылка выполняется…':'↻ Обновить ссылки';}};let busyState=false;const poll=async()=>{try{const r=await fetch('/api/users/subscription-refresh/status',{cache:'no-store',headers:{'Accept':'application/json'}});if(r.ok){const d=await r.json();busyState=['queued','running'].includes(d.state);render(d);}}catch(_e){}setTimeout(poll,busyState?900:4000)};if(form){form.addEventListener('submit',async(e)=>{if(!window.fetch)return;if(!confirm('Разослать всем Telegram-пользователям актуальную ссылку подписки, полученную из 3x-ui?')){e.preventDefault();return;}e.preventDefault();start.disabled=true;start.textContent='⏳ Запуск…';if(panel)panel.style.display='block';try{const r=await fetch(form.action,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8','Accept':'application/json','X-Requested-With':'XMLHttpRequest'},body:new URLSearchParams(new FormData(form)),credentials:'same-origin'});const d=await r.json().catch(()=>({}));if(!r.ok||d.ok===false){throw new Error(d.detail||d.error||'Не удалось запустить задачу')}render(d);busyState=['queued','running'].includes(d.state);}catch(err){setText(box,'Не удалось запустить: '+(err&&err.message?err.message:'неизвестная ошибка'));if(box){box.className='notice bad';box.style.display='block';}start.disabled=false;start.textContent='↻ Обновить ссылки';}});}poll();const f=document.querySelector('[data-user-filter-form]');if(!f)return;const search=f.querySelector('#user-filter-search');const status=f.querySelector('[data-filter-status]');const sort=f.querySelector('[data-filter-sort]');const order=f.querySelector('[data-filter-order]');const list=document.querySelector('.user-list');const count=document.getElementById('users-visible-count');const total=document.getElementById('users-total-count');const rows=()=>Array.from(document.querySelectorAll('.user-row'));const norm=v=>String(v??'').trim().toLocaleLowerCase('ru-RU');const num=el=>Number(el?.dataset?.value??el?.dataset?.userRemaining??0)||0;const saveUrl=()=>{if(location.origin==='null')return;const params=new URLSearchParams();const qv=search?search.value.trim():'';if(qv)params.set('q',qv);if(status&&status.value!=='all')params.set('status',status.value);if(sort&&sort.value!=='remaining')params.set('sort',sort.value);if(order&&order.value!=='asc')params.set('order',order.value);history.replaceState(null,'',params.toString()?('?'+params.toString()):location.pathname);};const apply=()=>{const query=norm(search?.value||'');const st=status?.value||'all';const so=sort?.value||'remaining';const od=order?.value||'asc';const all=rows();const filtered=all.filter(row=>{const matchesQuery=!query||norm(row.dataset.userSearch||'').includes(query);const isUnread=false;const isOnline=row.dataset.userOnline==='1';const rowStatus=row.dataset.userStatus||'';const matchesStatus=st==='all'||(st==='unread'&&isUnread)||(st==='online'&&isOnline)||rowStatus===st;return matchesQuery&&matchesStatus;});const dir=od==='desc'?-1:1;const keyFn={remaining:r=>Number(r.dataset.userRemaining||0),last_online:r=>Number(r.dataset.userLastOnline||0),traffic:r=>Number(r.dataset.userTraffic||0),quota:r=>Number(r.dataset.userQuota||0),name:r=>norm(r.dataset.userName||'')};filtered.sort((a,b)=>{const av=keyFn[so](a),bv=keyFn[so](b);if(typeof av==='string')return av.localeCompare(bv,'ru')*dir;return (av-bv)*dir;});if(list){const frag=document.createDocumentFragment();filtered.forEach(row=>frag.appendChild(row));list.appendChild(frag);}all.forEach(row=>{row.hidden=!filtered.includes(row);});if(count)setText(count,filtered.length);if(total)setText(total,all.length);saveUrl();};const setInitial=()=>{if(search&&f.dataset.initialQuery!=null)search.value=f.dataset.initialQuery||'';if(status&&['all','unread','active','expired','blocked','online'].includes(f.dataset.initialStatus||''))status.value=f.dataset.initialStatus||'all';if(sort&&['remaining','last_online','traffic','quota','name'].includes(f.dataset.initialSort||''))sort.value=f.dataset.initialSort||'remaining';if(order&&['asc','desc'].includes(f.dataset.initialOrder||''))order.value=f.dataset.initialOrder||'asc';};setInitial();let timer=0;if(search)search.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(apply,120);});[status,sort,order].forEach(x=>x&&x.addEventListener('change',apply));window.addEventListener('popstate',()=>location.reload());apply();})();</script>"""
    # This operation now has a dedicated navigation section.  Strip the old
    # control and persistent completed-status card from the Users workspace.
    body = re.sub(
        r'<form id="subscription-refresh-form".*?</form>',
        '<a class="button secondary" href="/subscription-tools">↻ Ссылки подписок</a>',
        body,
        count=1,
        flags=re.S,
    )
    body = re.sub(
        r'<section id="subscription-refresh-panel".*?</section><div id="subscription-refresh-status".*?</div>',
        '',
        body,
        count=1,
        flags=re.S,
    )
    return page(request, "Пользователи", body, "users", scripts)


@app.get("/subscription-tools", response_class=HTMLResponse)
def subscription_tools_page(request: Request):
    require_auth(request)
    csrf = html.escape(session_csrf_token(request), quote=True)
    body = f'''<header><div><h1>Ссылки подписок</h1><div class="subtitle">Отдельный центр массовой отправки актуальных ссылок из 3x-ui</div></div></header>
<div class="grid compact-grid"><section class="card half"><div class="section-title"><div><h2>Массовое обновление</h2><p class="muted">Каждому привязанному Telegram-пользователю будет отправлена его текущая ссылка.</p></div><span id="subscription-refresh-badge" class="badge">Ожидание</span></div>
<form id="subscription-refresh-form" method="post" action="/users/refresh-subscription-links"><input type="hidden" name="csrf_token" value="{csrf}"><button id="subscription-refresh-start" type="submit">↻ Запустить отправку</button></form><div id="subscription-refresh-status" class="notice" hidden></div></section>
<section id="subscription-refresh-panel" class="card half subscription-refresh-panel"><div class="section-title"><h2>Ход выполнения</h2><span class="muted">Фоновая задача</span></div><div class="subscription-refresh-progress"><div class="progress large"><span id="subscription-refresh-progress-bar" style="width:0%"></span></div><div class="progress-meta"><span id="subscription-refresh-progress-text">Задача ещё не запускалась</span><span id="subscription-refresh-progress-percent">0%</span></div></div><div class="subscription-refresh-stats"><span>Обработано <b id="subscription-refresh-processed">0</b></span><span>Отправлено <b id="subscription-refresh-delivered">0</b></span><span>Пропущено <b id="subscription-refresh-skipped">0</b></span><span>Ошибок <b id="subscription-refresh-failed">0</b></span></div><div class="subscription-refresh-current" id="subscription-refresh-current"></div></section>
<section class="card full subscription-refresh-log"><div class="section-title"><h2>Журнал выполнения</h2><span class="muted">Последние события</span></div><pre id="subscription-refresh-log-body" class="log-output">Ожидание запуска…</pre></section></div>'''
    return page(request, "Ссылки подписок", body, "subscription-tools")



@app.post("/diagnostics/auto-fix")
def diagnostics_auto_fix(request: Request):
    require_auth(request)
    actor = str(request.session.get("user", "web"))
    actions: list[str] = []
    errors: list[str] = []
    try:
        # Database migrations are idempotent and are safe to run on every fix pass.
        migrate_database()
        actions.append("проверена и актуализирована структура локальной БД")
    except Exception as error:
        errors.append(f"БД: {error}")
    try:
        audit_result = service_audit.fix()
        actions.extend([str(item) for item in audit_result.get("actions", [])])
        if audit_result.get("after", {}).get("healthy"):
            actions.append("службы/cron после проверки не содержат найденных дубликатов")
        else:
            errors.append("после исправления остались проблемы в аудите служб")
    except Exception as error:
        errors.append(f"службы: {error}")
    try:
        repair = repair_panel_client_identities_sync(config.DB_PATH)
        actions.append(f"3x-ui имена: исправлено {repair.get('updated',0)}, проверено {repair.get('checked',0)}")
        errors.extend([str(item) for item in (repair.get('errors') or [])][:5])
    except Exception as error:
        errors.append(f"3x-ui имена: {error}")
    try:
        invalidate_snapshot_cache()
        snapshot = fetch_and_sync(force=True, db_path=config.DB_PATH)
        if snapshot.get("stale"):
            errors.append(f"синхронизация 3x-ui: {snapshot.get('error')}")
        else:
            actions.append(f"получены актуальные данные 3x-ui: {len(snapshot.get('clients', []))} клиентов")
    except Exception as error:
        errors.append(f"синхронизация 3x-ui: {error}")
    audit(actor, "diagnostics_auto_fix", json.dumps({"actions": actions, "errors": errors}, ensure_ascii=False)[:8000])
    if errors:
        set_flash(request, "Проверка завершена с замечаниями: " + "; ".join(errors[:3]), "bad")
    else:
        set_flash(request, "Поиск и исправление завершены: " + "; ".join(actions[:3]), "good")
    return RedirectResponse(public_path("/diagnostics"), 303)


@app.post("/users/import-to-3xui")
def import_users_to_3xui(request: Request):
    require_auth(request)
    actor = str(request.session.get("user", "web"))
    try:
        repair = repair_panel_client_identities_sync(config.DB_PATH)
        result = import_local_users_to_3xui_sync(config.DB_PATH)
        errors = list(result.get("errors") or []) + list(repair.get("errors") or [])
        msg = (
            f"3x-ui: проверено {repair.get('checked',0)}, имена исправлено {repair.get('updated',0)}. "
            f"Пользователей обработано {result.get('total',0)}: создано {result.get('created',0)}, "
            f"восстановлено {result.get('reused',0)}, пропущено {result.get('skipped',0)}"
        )
        if errors:
            msg += f", ошибок {len(errors)}"
        set_flash(request, msg, "bad" if errors else "good")
        audit(actor, "import_users_to_3xui", json.dumps({"repair": repair, "import": result}, ensure_ascii=False)[:8000])
    except Exception as error:
        set_flash(request, f"Восстановление 3x-ui не выполнено: {error}", "bad")
    return RedirectResponse(public_path("/users"), 303)

@app.post("/users/refresh-subscription-links")
def refresh_subscription_links_start(request: Request, csrf_token: str = Form("")):
    require_auth(request)
    require_csrf(request, csrf_token)
    actor = str(request.session.get("user", "web"))
    wants_json = "application/json" in str(request.headers.get("accept") or "") or str(request.headers.get("x-requested-with") or "").lower() == "xmlhttprequest"
    try:
        status = subscription_refresh_manager.start(actor=actor)
        audit(actor, "subscription_urls_refresh_started", str(status.get("job_id") or ""))
        if wants_json:
            return JSONResponse({"ok": True, **status})
        set_flash(request, "Рассылка актуальных ссылок подписки запущена", "good")
    except Exception as exc:
        audit(actor, "subscription_urls_refresh_failed", str(exc))
        if wants_json:
            return JSONResponse({"ok": False, "state": "failed", "message": "Не удалось запустить обновление ссылок", "error": str(exc)[:1000]}, status_code=409 if "уже выполняется" in str(exc) else 500)
        set_flash(request, f"Не удалось запустить обновление ссылок: {exc}", "bad")
    return RedirectResponse(public_path("/users"), 303)


@app.get("/api/users/subscription-refresh/status")
def refresh_subscription_links_status(request: Request):
    require_auth(request)
    return subscription_refresh_manager.read_status()


@app.get("/users/import-identities", response_class=HTMLResponse)
def import_identities_page(request: Request, token: str = ""):
    require_auth(request)
    preview: list[identity_migration.IdentityMatch] = []
    metadata: dict[str, Any] = {}
    error = ""
    if token:
        try:
            metadata, preview = identity_migration.preview_staged(token, config.DB_PATH)
        except Exception as exc:
            error = str(exc)
    summary = identity_migration.preview_summary(preview) if preview else {}
    rows = "".join(
        f'''<tr><td>{item.source.tg_id}</td><td>@{html.escape(item.source.username or "—")}</td><td>{html.escape(item.source.email or "—")}</td><td>{item.target_tg_id if item.target_tg_id is not None else "—"}</td><td><span class="badge {"good" if item.status in {"ready", "already"} else ("bad" if item.status == "conflict" else "warn")}">{html.escape(item.status)}</span></td><td>{html.escape(item.reason)}<br><span class="muted">{html.escape(item.detail)}</span></td></tr>'''
        for item in preview
    )
    preview_block = ""
    if preview:
        preview_block = f'''<div class="card full"><div class="section-title"><h2>Предварительная проверка</h2><span class="badge good">{summary.get('ready', 0)} готовы</span></div>
<div class="grid"><div class="card metric"><div class="label">Найдено</div><div class="value">{summary.get('total', 0)}</div></div><div class="card metric"><div class="label">Готово</div><div class="value">{summary.get('ready', 0)}</div></div><div class="card metric"><div class="label">Уже актуально</div><div class="value">{summary.get('already', 0)}</div></div><div class="card metric"><div class="label">Конфликты / без пары</div><div class="value">{summary.get('conflict', 0) + summary.get('unmatched', 0)}</div></div></div>
<div class="table-wrap"><table><thead><tr><th>Старый TG ID</th><th>Ник</th><th>Email 3x-ui</th><th>Текущий TG ID</th><th>Статус</th><th>Основание</th></tr></thead><tbody>{rows}</tbody></table></div>
<form method="post" action="/users/import-identities/apply" style="margin-top:18px"><input type="hidden" name="token" value="{html.escape(token)}"><label class="check-row"><input type="checkbox" name="sync_panel" value="1" checked> Записать восстановленные tgId также в 3x-ui</label><label class="check-row"><input type="checkbox" name="overwrite_conflicts" value="1"> Разрешить замену уже положительных TG ID (только после просмотра конфликтов)</label><button data-confirm="Применить проверенные Telegram-привязки?">Применить восстановление</button></form></div>'''
    error_html = f'<div class="notice">{html.escape(error)}</div>' if error else ""
    body = f'''<header><div><h1>Восстановление Telegram-привязок</h1><div class="subtitle">Безопасное сопоставление по UUID и email — без догадок по числам в имени</div></div><a class="button secondary" href="/users">Назад к пользователям</a></header>{error_html}
<div class="grid"><div class="card full"><h2>Загрузить старую базу или архив</h2><p class="muted">Поддерживаются SQLite и tar.gz. Импорт сначала создаёт отчёт; изменения не применяются до отдельного подтверждения. Загруженная копия удаляется после применения.</p><form method="post" action="/users/import-identities" enctype="multipart/form-data"><div class="setting"><label>Старая база/архив</label><input type="file" name="source" accept=".db,.sqlite,.sqlite3,.tar,.gz,.tgz,application/gzip" required></div><button>Проверить привязки</button></form></div>{preview_block}</div>'''
    return page(request, "Импорт Telegram-привязок", body, "users")


@app.post("/users/import-identities")
def import_identities_upload(request: Request, source: UploadFile = File(...)):
    require_auth(request)
    max_size = max(10, int(getattr(config, "IDENTITY_IMPORT_MAX_MB", 512))) * 1024 * 1024
    temp_path: Path | None = None
    try:
        suffix = "".join(Path(source.filename or "legacy.db").suffixes[-2:]) or ".db"
        with tempfile.NamedTemporaryFile(prefix="vpn_identity_upload_", suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
            total = 0
            while True:
                chunk = source.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    raise identity_migration.IdentityImportError("Файл импорта превышает допустимый размер")
                handle.write(chunk)
        metadata = identity_migration.stage_import(temp_path, source.filename or "legacy.db")
        audit(str(request.session.get("user", "web")), "identity_import_preview", str(metadata.get("selected_rows")))
        return RedirectResponse(public_path(f"/users/import-identities?token={quote(str(metadata['token']))}"), 303)
    except Exception as exc:
        set_flash(request, f"Не удалось прочитать старую базу: {exc}", "bad")
        return RedirectResponse(public_path("/users/import-identities"), 303)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        source.file.close()


@app.post("/users/import-identities/apply")
def import_identities_apply(
    request: Request,
    token: str = Form(),
    sync_panel: str | None = Form(None),
    overwrite_conflicts: str | None = Form(None),
):
    require_auth(request)
    try:
        result = identity_migration.apply_staged(
            token,
            db_path=config.DB_PATH,
            sync_panel=sync_panel == "1",
            overwrite_conflicts=overwrite_conflicts == "1",
        )
        audit(str(request.session.get("user", "web")), "identity_import_apply", json.dumps(result, ensure_ascii=False)[:3000])
        message = (
            f"Привязки обработаны: обновлено {result['updated']}, уже актуально {result['already']}, "
            f"конфликтов {result['conflicts']}, без совпадения {result['unmatched']}, в 3x-ui записано {result['panel_updated']}."
        )
        if result.get("panel_errors"):
            message += " Ошибки 3x-ui: " + "; ".join(result["panel_errors"][:3])
        set_flash(request, message, "bad" if result.get("panel_errors") else "good")
    except Exception as exc:
        set_flash(request, f"Импорт не применён: {exc}", "bad")
        return RedirectResponse(public_path(f"/users/import-identities?token={quote(token)}"), 303)
    return RedirectResponse(public_path("/users"), 303)


@app.get("/api/users/{tg_id}/xui-clients", response_class=JSONResponse)
def user_xui_clients_api(request: Request, tg_id: int, q: str = ""):
    require_auth(request)
    if not get_user(tg_id):
        raise HTTPException(404, "Пользователь не найден")
    needle = str(q or "").strip().lower()
    try:
        snapshot = fetch_snapshot_sync(force=True)
    except Exception as error:
        return JSONResponse({"ok": False, "detail": f"Не удалось получить список клиентов 3x-ui: {error}"}, status_code=502)
    clients = snapshot.get("clients") if isinstance(snapshot, dict) else []
    with database() as connection:
        local_rows = connection.execute(
            "SELECT tg_id,email,uuid,username FROM users WHERE tg_id<>?", (int(tg_id),)
        ).fetchall()
    by_email = {str(row["email"] or "").strip().lower(): dict(row) for row in local_rows if str(row["email"] or "").strip()}
    by_uuid = {str(row["uuid"] or "").strip().lower(): dict(row) for row in local_rows if str(row["uuid"] or "").strip()}
    result = []
    for item in clients if isinstance(clients, list) else []:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip()
        uuid = str(item.get("uuid") or "").strip()
        name = str(item.get("comment") or item.get("name") or "").strip()
        panel_tg = int(item.get("tg_id") or 0)
        haystack = " ".join((email, uuid, name, str(panel_tg))).lower()
        if needle and needle not in haystack:
            continue
        owner = by_email.get(email.lower()) or by_uuid.get(uuid.lower())
        result.append({
            "email": email,
            "uuid": uuid,
            "name": name,
            "panel_tg_id": panel_tg,
            "local_owner_tg_id": int(owner["tg_id"]) if owner else None,
            "local_owner_username": str(owner["username"] or "") if owner else "",
            "available": owner is None or int(owner["tg_id"]) == int(tg_id),
            "expiry_time": int(item.get("expiry_time") or 0),
            "enable": bool(item.get("enable", True)),
            "sub_id": str(item.get("sub_id") or ""),
        })
        if len(result) >= 50:
            break
    return JSONResponse({
        "ok": True,
        "items": result,
        "stale": bool(snapshot.get("stale")),
        "snapshot_error": str(snapshot.get("error") or ""),
    })


@app.post("/users/{tg_id}/bind-3xui")
def bind_3xui_user(
    request: Request,
    tg_id: int,
    new_tg_id: int = Form(...),
    email: str = Form(...),
    confirm: str = Form(""),
    username: str = Form(""),
):
    require_auth(request)
    if confirm != "1":
        raise HTTPException(400, "Требуется явное подтверждение привязки")
    try:
        result = identity_migration.bind_existing_panel_client(
            int(tg_id), int(new_tg_id), email, username or None, db_path=config.DB_PATH,
        )
    except Exception as error:
        set_flash(request, f"Не удалось привязать клиента 3x-ui: {error}", "bad")
        return RedirectResponse(public_path(f"/users/id/{tg_id}"), 303)
    actor = str(request.session.get("user", "web"))
    audit(actor, "bind_3xui_client", f"{tg_id}->{new_tg_id}:{email}")
    user_events.safe_record_event(
        int(new_tg_id),
        username=str(result.get("username") or username or ""),
        direction="system",
        event_type="identity_rebound",
        text=f"Существующий клиент 3x-ui {email} привязан к новому Telegram ID",
        actor="web",
        metadata={"old_tg_id": int(tg_id), "new_tg_id": int(new_tg_id), "email": email},
        db_path=config.DB_PATH,
    )
    set_flash(request, "Существующий клиент 3x-ui привязан. Подписка, UUID и история сохранены.", "good")
    return RedirectResponse(public_path(f"/users/id/{new_tg_id}"), 303)


@app.get("/users/id/{tg_id}", response_class=HTMLResponse)
def user_detail_page(request: Request, tg_id: int):
    require_auth(request)
    row = get_user(tg_id)
    if not row:
        raise HTTPException(404)
    try:
        with database() as connection:
            referral_owner_row = connection.execute(
                "SELECT username FROM users WHERE tg_id=?",
                (int(row.get("referred_by_tg_id") or 0),),
            ).fetchone() if int(row.get("referred_by_tg_id") or 0) > 0 else None
        row["referred_by_username"] = str(referral_owner_row[0] or "") if referral_owner_row else ""
    except Exception:
        row["referred_by_username"] = ""
    try:
        referral_stat = referral_rewards.referral_stats(int(tg_id), db_path=config.DB_PATH)
    except Exception as error:
        LOGGER.warning("Не удалось получить реферальную статистику TG ID %s: %s", tg_id, error)
        referral_stat = {"invited": 0, "paid": 0, "rewards": 0, "days": 0}
    # Opening the conversation is the read action. Clear the compact counter
    # before rendering even if an imported/older database contains a stale
    # cursor. A message committed after this transaction is counted normally.
    try:
        user_events.mark_conversation_opened(tg_id, db_path=config.DB_PATH)
    except Exception as error:
        LOGGER.warning("Не удалось сбросить непрочитанные сообщения TG ID %s: %s", tg_id, error)
    events = user_events.recent_events(tg_id, limit=250, db_path=config.DB_PATH)
    event_html = render_user_events(events, tg_id)
    initial_last_id = int(events[-1]["id"]) if events else 0
    # A second exact acknowledgement covers a message that was committed after
    # the first reset but was included in the rendered snapshot. Anything newer
    # than the snapshot boundary remains unread.
    if initial_last_id > 0:
        try:
            user_events.mark_messages_read(
                tg_id,
                through_event_id=initial_last_id,
                db_path=config.DB_PATH,
            )
        except Exception as error:
            LOGGER.warning("Не удалось подтвердить видимую историю TG ID %s: %s", tg_id, error)
    disabled = tg_id <= 0
    identity_source = str(row.get("identity_source") or "не указано")
    xui_extra = fetch_client_extra_sync(str(row.get("email") or "")) if str(row.get("email") or "").strip() else {"traffic": {}, "ips": [], "error": ""}
    xui_ips = xui_extra.get("ips") if isinstance(xui_extra.get("ips"), list) else []
    xui_traffic = xui_extra.get("traffic") if isinstance(xui_extra.get("traffic"), dict) else {}
    xui_extra_html = (
        f'<div class="setting"><label>Последние IP подключения (3x-ui)</label><div class="compact-tags">{" ".join(f"<span>{html.escape(str(ip))}</span>" for ip in xui_ips) or "<span class=\"muted\">3x-ui пока не вернула IP</span>"}</div></div>'
        f'<div class="setting"><label>Сводка трафика 3x-ui</label><div class="detail-inline-stats"><span>↑ {fmt_bytes(xui_traffic.get("up"))}</span><span>↓ {fmt_bytes(xui_traffic.get("down"))}</span><span>Всего {fmt_bytes(int(xui_traffic.get("up") or 0)+int(xui_traffic.get("down") or 0))}</span></div></div>'
    )
    if xui_extra.get("error"):
        xui_extra_html += f'<div class="muted">Дополнительные данные 3x-ui недоступны: {html.escape(str(xui_extra["error"]))}</div>'
    xui_bind_url = public_path(f"/users/{tg_id}/bind-3xui")
    xui_clients_url = public_path(f"/api/users/{tg_id}/xui-clients")
    referral_html = (
        '<div class="card" style="margin-top:14px">'
        '<div class="section-title"><h2>Реферальная информация</h2><span class="badge good">+10 дней</span></div>'
        f'<div class="detail-inline-stats"><span>Приглашено: <b>{int(referral_stat.get("invited", 0))}</b></span>'
        f'<span>Оплатили: <b>{int(referral_stat.get("paid", 0))}</b></span>'
        f'<span>Бонусов выдано: <b>{int(referral_stat.get("rewards", 0))}</b></span>'
        f'<span>Дней начислено: <b>{int(referral_stat.get("days", 0))}</b></span></div>'
        '<p class="muted">Бонус начисляется только после первой подтверждённой оплаты приглашённого пользователя и один раз за каждого приглашённого.</p>'
        '</div>'
    )
    media_limit = max(1, int(getattr(config, "CHAT_MEDIA_MAX_MB", 100)))
    body = f'''<header><div><h1>{html.escape(str(row.get('username') or 'Пользователь'))}</h1><div class="subtitle">Карточка, Telegram-привязка, переписка и журнал действий</div></div><a class="button secondary" href="/users">Назад</a></header>
<div class="chat-layout"><div class="card"><div class="section-title"><h2>Чат с пользователем</h2><span class="badge {'warn' if disabled else 'good'}">{'Нет TG ID' if disabled else 'Telegram подключён'}</span></div>
{('<div class="notice">Сначала восстановите или укажите положительный Telegram ID. Ник сам по себе не позволяет боту написать пользователю.</div>' if disabled else '')}
<div id="chat-window" class="chat-window">{event_html or '<div class="empty-state">История пока пуста. Новые сообщения и действия появятся здесь.</div>'}</div>
<form id="chat-form" class="chat-compose" method="post" action="/users/{tg_id}/message" enctype="multipart/form-data"><div class="chat-compose-main"><textarea name="message" maxlength="4096" {'disabled' if disabled else ''} placeholder="Сообщение или подпись к фото/видео"></textarea><label class="chat-attachment"><span>＋ Фото или видео</span><input name="media" type="file" accept="image/*,video/*" {'disabled' if disabled else ''}><small>До {media_limit} МБ; для медиа подпись — до 1024 символов.</small></label></div><button {'disabled' if disabled else ''}>Отправить</button></form></div>
<div class="card"><h2>Идентификация</h2><div class="setting"><label>Проверка Telegram по нику</label><div class="actions"><button type="button" class="secondary" id="telegram-lookup-button">🔎 Найти Telegram ID по @username</button>{('<button type="button" class="secondary" id="telegram-link-button">🔗 Запросить привязку через Telegram</button>' if tg_id <= 0 else '')}</div><div id="telegram-lookup-result" class="muted" style="margin-top:10px;white-space:pre-wrap"></div><div id="telegram-link-result" class="notice" style="display:none;margin-top:10px;word-break:break-all"></div></div><form method="post" action="/users/id/{tg_id}/identity"><div class="setting"><label>Telegram ID</label><input name="new_tg_id" type="number" min="1" value="{tg_id if tg_id > 0 else ''}" required></div><div class="setting"><label>Telegram @username</label><input name="username" value="{html.escape(str(row.get('username') or ''))}" maxlength="100"></div><label class="check-row"><input type="checkbox" name="overwrite_positive" value="1"> Подтверждаю замену существующего положительного ID</label><button>Сохранить и синхронизировать 3x-ui</button></form>
<hr style="border:0;border-top:1px solid var(--line);margin:14px 0">{xui_extra_html}<hr style="border:0;border-top:1px solid var(--line);margin:14px 0"><div class="setting"><label>Email 3x-ui</label><div class="code">{html.escape(str(row.get('email') or '—'))}</div></div><div class="setting"><label>UUID</label><div class="code">{html.escape(str(row.get('uuid') or '—'))}</div></div><div class="setting"><label>Источник привязки</label><div>{html.escape(identity_source)}</div></div><div class="setting"><label>Ключ регистрации</label><div class="code">{html.escape(str(row.get('referred_by_code') or '—'))}</div></div><div class="setting"><label>Зарегистрирован по приглашению</label><div>{html.escape((('@' + str(row.get('referred_by_username'))) if row.get('referred_by_username') else ('TG ID ' + str(row.get('referred_by_tg_id'))) if row.get('referred_by_tg_id') else 'Нет данных'))}</div></div><div class="setting"><label>Собственный ключ приглашения</label><div class="code">{html.escape(str(row.get('referral_code') or '—'))}</div></div><div class="setting"><label>Дата регистрации</label><div>{html.escape(str(row.get('registered_at') or '—'))}</div></div><div class="setting"><label>Последнее обновление</label><div>{html.escape(str(row.get('identity_updated_at') or '—'))}</div></div>{referral_html}<div class="card" style="margin-top:14px"><div class="section-title"><h2>Привязать существующий клиент 3x-ui</h2><span class="badge warn">Администратор</span></div><p class="muted">Выберите уже существующий клиент 3x-ui. Новый клиент не создаётся: сохраняются UUID, подписка, срок, subId и история. Операция меняет только привязку Telegram ID.</p><div class="two"><div class="setting"><label>Новый Telegram ID</label><input id="bind-new-tg-id" type="number" min="1" placeholder="Введите новый Telegram ID"></div><div class="setting"><label>Поиск клиента</label><input id="bind-xui-search" type="search" placeholder="Email, UUID, имя или TG ID"></div></div><div class="actions"><button type="button" class="secondary" id="bind-xui-search-button">Найти</button></div><div id="bind-xui-result" class="xui-bind-results muted" style="margin-top:10px">Нажмите «Найти», чтобы получить актуальный список клиентов из 3x-ui.</div><form id="bind-xui-form" method="post" action="{html.escape(xui_bind_url, quote=True)}" style="display:none;margin-top:12px"><input type="hidden" name="email" id="bind-xui-email"><input type="hidden" name="new_tg_id" id="bind-xui-target"><input type="hidden" name="username" value="{html.escape(str(row.get('username') or ''), quote=True)}"><input type="hidden" name="confirm" value="1"><div class="notice" id="bind-xui-confirm-text"></div><button type="submit">Подтвердить привязку</button></form></div></div></div>'''
    script = f'''<script>
(function(){{
const b=document.getElementById('telegram-lookup-button'),r=document.getElementById('telegram-lookup-result');
const initialChatScroll=()=>{{const chat=document.getElementById('chat-window');if(chat)chat.scrollTop=chat.scrollHeight;}};
requestAnimationFrame(initialChatScroll);setTimeout(initialChatScroll,100);
if(b)b.addEventListener('click',async()=>{{const i=document.querySelector('input[name=\"username\"]'),u=String((i&&i.value)||'').trim().replace(/^@/,'');if(!u){{r.textContent='Укажите @username';return;}}b.disabled=true;r.textContent='Поиск…';try{{const x=await fetch('/api/telegram/lookup?username='+encodeURIComponent(u));const d=await x.json();if(!x.ok)throw new Error(d.detail||'Ошибка');r.textContent=d.matches&&d.matches.length?d.matches.map(z=>'@'+(z.username||u)+' — Telegram ID '+z.tg_id+(z.telegram_connected?' · подключён':' · без привязки')).join('\\n'):'Совпадений нет. Для произвольного @username Telegram Bot API не даёт универсального способа получить ID.';}}catch(e){{r.textContent=e.message||'Ошибка';}}finally{{b.disabled=false;}}}});

const linkButton=document.getElementById('telegram-link-button'),linkResult=document.getElementById('telegram-link-result');
if(linkButton)linkButton.addEventListener('click',async()=>{{linkButton.disabled=true;linkResult.style.display='block';linkResult.textContent='Создание одноразовой ссылки…';try{{const body=new URLSearchParams();body.set('tg_id',String({tg_id}));const response=await fetch('/api/telegram/link-request',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body,credentials:'same-origin'}});const data=await response.json();if(!response.ok)throw new Error(data.detail||'Ошибка');linkResult.innerHTML='Передайте пользователю ссылку: <a href="'+data.deep_link+'" target="_blank" rel="noopener">'+data.deep_link+'</a>';const token=data.token;const poll=async()=>{{try{{const r=await fetch('/api/telegram/link-request/'+encodeURIComponent(token),{{credentials:'same-origin',cache:'no-store'}});const status=await r.json();if(status.status==='resolved'){{linkResult.textContent='✅ Telegram ID получен: '+status.tg_id;setTimeout(()=>location.reload(),600);return;}}if(status.status==='expired'){{linkResult.textContent='Ссылка истекла. Создайте новую.';return;}}setTimeout(poll,2000);}}catch(_e){{setTimeout(poll,3000);}}}};poll();}}catch(error){{linkResult.textContent=error.message||'Ошибка';}}finally{{linkButton.disabled=false;}}}});

}})();
(function(){{
let afterId={initial_last_id};const chat=document.getElementById('chat-window');const form=document.getElementById('chat-form');
function appendEvent(item){{
  const empty=chat.querySelector('.empty-state');if(empty)empty.remove();
  if(item.id && chat.querySelector('[data-event-id="'+Number(item.id)+'"]')){{afterId=Math.max(afterId,Number(item.id||0));return;}}
  const box=document.createElement('div');box.className='chat-message '+(item.direction||'system')+(item.success===false?' failed':'');box.dataset.eventId=item.id;
  const media=item.metadata&&item.metadata.media;
  if(media&&media.url){{
    if(media.kind==='photo'){{const link=document.createElement('a');link.className='chat-media-link';link.href=media.url;link.target='_blank';link.rel='noopener';const image=document.createElement('img');image.className='chat-media-image';image.src=media.url;image.alt=media.file_name||'Фотография';image.loading='lazy';link.append(image);box.append(link);}}
    if(media.kind==='video'){{const video=document.createElement('video');video.className='chat-media-video';video.src=media.url;video.controls=true;video.preload='metadata';video.playsInline=true;box.append(video);}}
  }}
  const text=document.createElement('div');text.className='chat-message-text';text.textContent=item.text||item.event_type||'Событие';
  const meta=document.createElement('small');meta.textContent=(item.created_at||'')+' · '+(item.actor||item.username||'system');box.append(text,meta);chat.append(box);afterId=Math.max(afterId,Number(item.id||0));chat.scrollTop=chat.scrollHeight;
}}
async function markVisibleRead(){{if(document.hidden)return;try{{const response=await fetch('/api/users/{tg_id}/read?through_event_id='+afterId,{{method:'POST',headers:{{Accept:'application/json'}},cache:'no-store',keepalive:true}});}}catch(_e){{}}}}
async function poll(){{try{{const r=await fetch('/api/users/{tg_id}/events?after_id='+afterId,{{cache:'no-store'}});if(r.ok){{const d=await r.json();const items=d.events||[];items.forEach(appendEvent);if(items.length)await markVisibleRead();}}}}catch(_e){{}}finally{{setTimeout(poll,3000);}}}}
if(form)form.addEventListener('submit',async(e)=>{{e.preventDefault();const button=form.querySelector('button');const text=form.querySelector('textarea');const file=form.querySelector('input[type=file]');if(!(text.value||'').trim()&&!(file.files&&file.files.length)){{alert('Введите сообщение или выберите фото/видео');return;}}button.disabled=true;try{{const r=await fetch(form.action,{{method:'POST',body:new FormData(form),headers:{{Accept:'application/json'}}}});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Ошибка отправки');if(d.event)appendEvent(d.event);form.reset();}}catch(err){{alert(err.message);}}finally{{button.disabled=false;}}}});
document.addEventListener('visibilitychange',()=>{{if(!document.hidden)markVisibleRead();}});
chat.scrollTop=chat.scrollHeight;markVisibleRead();setTimeout(poll,1200);
const bindSearchButton=document.getElementById('bind-xui-search-button');
const bindSearch=document.getElementById('bind-xui-search');
const bindNewTg=document.getElementById('bind-new-tg-id');
const bindResult=document.getElementById('bind-xui-result');
const bindForm=document.getElementById('bind-xui-form');
const bindEmail=document.getElementById('bind-xui-email');
const bindTarget=document.getElementById('bind-xui-target');
const bindConfirm=document.getElementById('bind-xui-confirm-text');
const bindEndpoint={json.dumps(xui_clients_url)};
function renderBindItems(items){{
  bindResult.textContent='';
  if(!items.length){{bindResult.textContent='Клиенты не найдены.';return;}}
  items.forEach(item=>{{
    const button=document.createElement('button');
    button.type='button'; button.className='button secondary'; button.style.textAlign='left';
    button.disabled=!item.available;
    const label=[item.name||item.email||'без имени',item.email,item.uuid?'UUID '+String(item.uuid).slice(0,12)+'…':'',item.panel_tg_id?'TG '+item.panel_tg_id:'',item.local_owner_tg_id?'занят TG '+item.local_owner_tg_id:'свободен'].filter(Boolean).join(' · ');
    button.textContent=label;
    button.title=item.available?'Выбрать клиента':'Клиент уже связан с другим пользователем';
    button.addEventListener('click',()=>{{
      const target=String(bindNewTg.value||'').trim();
      if(!/^\\d+$/.test(target)||Number(target)<=0){{bindResult.textContent='Сначала укажите новый положительный Telegram ID.';return;}}
      bindEmail.value=item.email;bindTarget.value=target;
      bindConfirm.textContent='Будет привязан клиент '+item.email+' к Telegram ID '+target+'. Существующая подписка и UUID останутся у этого клиента. Проверьте данные перед подтверждением.';
      bindForm.style.display='block';
    }});
    bindResult.appendChild(button);
  }});
}}
if(bindSearchButton)bindSearchButton.addEventListener('click',async()=>{{
  const q=String(bindSearch.value||'').trim();bindSearchButton.disabled=true;bindResult.textContent='Получаю актуальный список 3x-ui…';
  try{{const response=await fetch(bindEndpoint+(q?'?q='+encodeURIComponent(q):''),{{cache:'no-store',credentials:'same-origin',headers:{{Accept:'application/json'}}}});const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.detail||'Ошибка 3x-ui');renderBindItems(data.items||[]);if(data.stale&&data.snapshot_error)bindResult.insertAdjacentHTML('beforeend','<div class="muted" style="width:100%">Снимок помечен как устаревший: '+String(data.snapshot_error).replace(/[<>&]/g,'')+'</div>');}}catch(error){{bindResult.textContent=error.message||'Не удалось получить список клиентов';}}finally{{bindSearchButton.disabled=false;}}
}});
if(bindNewTg)bindNewTg.addEventListener('input',()=>{{bindForm.style.display='none';}});
}})();</script>'''
    return page(request, "Пользователь", body, "users", script)


@app.post("/users/id/{tg_id}/identity")
def user_identity_update(
    request: Request,
    tg_id: int,
    new_tg_id: int = Form(),
    username: str = Form(""),
    overwrite_positive: str | None = Form(None),
):
    require_auth(request)
    row = get_user(tg_id)
    if not row:
        raise HTTPException(404)
    try:
        result = identity_migration.rebind_local_identity(
            tg_id,
            new_tg_id,
            username,
            source=f"manual:{request.session.get('user', 'web')}",
            overwrite_positive=overwrite_positive == "1",
            db_path=config.DB_PATH,
        )
        panel_error = ""
        if result.get("email"):
            try:
                bind_client_tg_id_sync(str(result["email"]), int(result["tg_id"]))
            except Exception as exc:
                panel_error = str(exc)
        user_events.safe_record_event(
            int(result["tg_id"]), username=str(result.get("username") or ""), direction="system",
            event_type="identity_updated", text=f"Telegram ID обновлён: {tg_id} → {result['tg_id']}",
            actor=str(request.session.get("user", "web")), metadata={"panel_error": panel_error}, db_path=config.DB_PATH,
        )
        audit(str(request.session.get("user", "web")), "identity_manual_update", f"{tg_id}->{result['tg_id']}")
        set_flash(request, "Привязка сохранена" + (f"; 3x-ui: {panel_error}" if panel_error else " и записана в 3x-ui"), "bad" if panel_error else "good")
        return RedirectResponse(public_path(f"/users/id/{int(result['tg_id'])}"), 303)
    except Exception as exc:
        set_flash(request, f"Не удалось изменить привязку: {exc}", "bad")
        return RedirectResponse(public_path(f"/users/id/{tg_id}"), 303)


@app.post("/api/users/{tg_id}/read")
def user_messages_read_api(request: Request, tg_id: int, through_event_id: int | None = None):
    """Acknowledge the exact visible chat boundary without leaving the mounted path."""
    require_auth(request)
    if not get_user(tg_id):
        raise HTTPException(404)
    try:
        result = user_events.mark_messages_read(
            tg_id,
            through_event_id=through_event_id if through_event_id and through_event_id > 0 else None,
            db_path=config.DB_PATH,
        )
    except Exception as error:
        raise HTTPException(409, f"Не удалось обновить состояние прочтения: {error}") from error
    return {"ok": True, **result}


@app.get("/api/panel/messages/unread")
def panel_messages_unread_api(request: Request):
    """Return the canonical unread snapshot used by sidebar and messages UI."""
    require_auth(request)
    try:
        snapshot = user_events.unread_messages_summary(db_path=config.DB_PATH)
    except Exception as error:
        LOGGER.warning("Не удалось получить unread snapshot: %s", error)
        raise HTTPException(503, "Состояние сообщений временно недоступно") from error
    return snapshot


@app.get("/api/users/{tg_id}/events")
def user_events_api(request: Request, tg_id: int, after_id: int = 0):
    require_auth(request)
    if not get_user(tg_id):
        raise HTTPException(404)
    events = user_events.recent_events(
        tg_id, limit=250, after_id=after_id, db_path=config.DB_PATH
    )
    return {"events": [event_for_web(event, tg_id) for event in events]}


APP_LOG_DEFAULT = "/var/log/vpn_bot.log"

def _app_log_path() -> Path:
    raw = str(getattr(config, "APP_LOG_PATH", APP_LOG_DEFAULT) or APP_LOG_DEFAULT).strip()
    path = Path(raw).resolve()
    allowed = Path(APP_LOG_DEFAULT).resolve()
    if path != allowed:
        raise HTTPException(500, "APP_LOG_PATH отклонён: разрешён только системный лог FargoVPN")
    return path

def _read_app_log_tail(limit: int = 200) -> list[str]:
    limit = max(20, min(int(limit), 1000))
    path = _app_log_path()
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    return lines[-limit:]

@app.get("/api/panel/app-log")
def app_log_api(request: Request, limit: int = 200):
    require_auth(request)
    path = _app_log_path()
    return JSONResponse({"ok": True, "path": str(path), "lines": _read_app_log_tail(limit), "limit": max(20, min(int(limit), 1000))})

@app.get("/api/panel/push/config")
def panel_push_config(request: Request):
    require_auth(request)
    if not bool(getattr(config, "PUSH_ENABLED", True)):
        raise HTTPException(410, "Уведомления панели отключены")
    return {"ok": True, "public_key": push_service.public_key(), "scope": public_path("/")}

@app.get("/api/panel/push/logs")
def panel_push_logs(request: Request, limit: int = 80):
    require_auth(request)
    safe_limit = max(10, min(int(limit or 80), 200))
    return {"ok": True, "logs": push_service.panel_logs(config.DB_PATH, safe_limit, str(request.session.get("user") or "").strip())}

@app.get("/api/panel/push/status")
def panel_push_status(request: Request):
    require_auth(request)
    return push_service.panel_status(config.DB_PATH, str(request.session.get("user") or "").strip())

@app.post("/api/panel/push/subscribe")
async def panel_push_subscribe(request: Request):
    require_auth(request)
    username=str(request.session.get("user") or "").strip()
    if not username: raise HTTPException(401, "Сессия не содержит имени пользователя")
    data=await request.json()
    push_service.panel_subscribe(config.DB_PATH, username, dict(data.get("subscription") or {}), str(data.get("user_agent") or ""))
    return {"ok": True}

@app.post("/api/panel/push/unsubscribe")
async def panel_push_unsubscribe(request: Request):
    require_auth(request)
    username=str(request.session.get("user") or "").strip(); data=await request.json()
    push_service.panel_unsubscribe(config.DB_PATH, username, str(data.get("endpoint") or ""))
    return {"ok": True}

@app.post("/api/panel/push/test")
def panel_push_test(request: Request, background_tasks: BackgroundTasks):
    require_auth(request)
    username=str(request.session.get("user") or "").strip()
    if not bool(getattr(config, "PUSH_TEST_ENABLED", True)):
        raise HTTPException(403, "Тестовые уведомления отключены")
    subscriptions = push_service.active_subscription_count(config.DB_PATH, username)
    if subscriptions <= 0:
        raise HTTPException(400, "Нет активной Push-подписки. Сначала включите Push для этого браузера.")
    background_tasks.add_task(
        push_service.notify_panel,
        config.DB_PATH,
        username,
        "FargoVPN",
        "Тестовое уведомление панели. Push/PWA работает.",
        public_path("/"),
        "fargovpn-panel-test",
        "high",
    )
    push_service._log(config.DB_PATH, username, "test", f"Тестовая Push-доставка поставлена в очередь; subscriptions={subscriptions}", "INFO")
    return JSONResponse({"ok": True, "queued": True, "subscriptions": subscriptions, "sent": 0, "failed": 0, "removed": 0}, status_code=202)

@app.get("/cabinet", response_class=HTMLResponse)
def cabinet_page(request: Request, access: str = ""):
    """Canonical personal cabinet entry point.

    Telegram sends a signed one-time-style access token in the query string.
    A valid token creates a short-lived signed session cookie; subsequent
    cabinet pages use that session and do not require the token again.
    """
    user = _cabinet_session_user(request, access)
    if not user:
        raise HTTPException(404, "Личный кабинет не найден или ссылка недействительна")
    username = str(user.get("username") or user.get("tg_id") or "Пользователь").strip().lstrip("@")
    sub_id = str(user.get("sub_id") or "").strip()
    sub_url = (
        current_subscription_url_sync(
            sub_id,
            fallback_base_url=str(getattr(config, "SUB_BASE_URL", "")),
        )
        if sub_id
        else ""
    )
    # Prefer a live 3x-ui snapshot for the fields shown to the user. If the
    # panel is temporarily unavailable, _cabinet_markup safely falls back to
    # values already stored in the local DB.
    try:
        snapshot = fetch_snapshot_sync(False)
        email = str(user.get("email") or "").strip().lower()
        uuid_value = str(user.get("uuid") or "").strip().lower()
        panel_user = (snapshot.get("by_email") or {}).get(email) if email else None
        if not panel_user and uuid_value:
            panel_user = (snapshot.get("by_uuid") or {}).get(uuid_value)
        if panel_user:
            user = {**user, **dict(panel_user), "tg_id": user.get("tg_id"), "username": user.get("username")}
    except Exception as error:
        LOGGER.warning("Лайв-данные 3x-ui для кабинета недоступны: %s", error)
    return HTMLResponse(_cabinet_markup(username, user, sub_url))


@app.post("/api/cabinet/logout")
def cabinet_logout(request: Request):
    request.session.pop("cabinet_tg_id", None)
    request.session.pop("cabinet_login_at", None)
    return JSONResponse({"ok": True})


@app.get("/cabinet/connection", response_class=HTMLResponse)
def cabinet_connection_page(request: Request, access: str = ""):
    """Show only client-app launch options; never expose 3x-ui inbounds here.

    Accept the same signed access token as /cabinet so every cabinet link is
    independently usable even before a session cookie has been established.
    """
    user = _cabinet_session_user(request, access)
    if not user:
        return RedirectResponse(
            url=public_path(str(getattr(config, "CABINET_PATH", "/cabinet") or "/cabinet")),
            status_code=303,
        )
    sub_id = str(user.get("sub_id") or "").strip()
    sub_url = (
        current_subscription_url_sync(
            sub_id,
            fallback_base_url=str(getattr(config, "SUB_BASE_URL", "")),
        )
        if sub_id
        else ""
    )
    path = html.escape(
        str(getattr(config, "CABINET_PATH", "/cabinet") or "/cabinet"),
        quote=True,
    )
    version = html.escape(update_manager.current_version())
    if not sub_url:
        return HTMLResponse(
            f"""<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\"><link rel=\"stylesheet\" href=\"/static/panel.css?v={version}\"><title>Подключение</title></head><body class=\"cabinet-page\"><div class=\"cabinet-shell\"><header class=\"cabinet-head\"><div><div class=\"cabinet-brand\">{html.escape(str(config.SERVICE_NAME))}</div><div class=\"cabinet-subtitle\">Как подключиться</div></div><a class=\"button secondary small\" href=\"{path}\">← Кабинет</a></header><main class=\"cabinet-main\"><section class=\"card cabinet-card\"><h1>Подключение пока недоступно</h1><p class=\"muted\">Для вашей подписки сейчас не удалось получить ссылку подключения. Вернитесь в кабинет или обратитесь в поддержку.</p><a class=\"button\" href=\"{path}#support\">💬 Поддержка</a></section></main></div></body></html>""",
            status_code=503,
        )

    # Keep the real subscription URL out of visible page text. The deep-link
    # contains it only as the target of the app-launch button.
    happ_url = html.escape(f"happ://add/{sub_url}", quote=True)
    incy_url = html.escape(f"incy://add/{sub_url}", quote=True)
    return HTMLResponse(
        f"""<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\"><meta name=\"theme-color\" content=\"#0b1220\"><link rel=\"stylesheet\" href=\"/static/panel.css?v={version}\"><title>Как подключиться</title></head><body class=\"cabinet-page\"><div class=\"cabinet-shell\"><header class=\"cabinet-head\"><div><div class=\"cabinet-brand\">{html.escape(str(config.SERVICE_NAME))}</div><div class=\"cabinet-subtitle\">Как подключиться</div></div><a class=\"button secondary small\" href=\"{path}\">← Кабинет</a></header><main class=\"cabinet-main\"><section class=\"cabinet-welcome\"><div><div class=\"cabinet-eyebrow\">Быстрое подключение</div><h1>Выберите устройство</h1><p>Мы не показываем серверы, порты и настройки 3x-ui. Нажмите кнопку приложения — оно откроется и получит вашу подписку автоматически.</p></div><span class=\"badge good\">Подписка готова</span></section><section class=\"cabinet-platform-grid cabinet-app-grid\"><div class=\"card cabinet-card cabinet-app-card\" data-os=\"android\"><div class=\"cabinet-app-icon\">🤖</div><h2>Android</h2><p class=\"muted\">Рекомендуем HAPP. Также можно открыть подписку в INCY.</p><div class=\"cabinet-app-actions\"><a class=\"button app-deeplink recommended\" href=\"{happ_url}\" data-app=\"happ\">⚡ Открыть в HAPP</a><a class=\"button secondary app-deeplink\" href=\"{incy_url}\" data-app=\"incy\">Открыть в INCY</a></div></div><div class=\"card cabinet-card cabinet-app-card\" data-os=\"ios\"><div class=\"cabinet-app-icon\">🍎</div><h2>iPhone / iPad</h2><p class=\"muted\">Рекомендуем INCY. HAPP тоже поддерживает импорт подписки.</p><div class=\"cabinet-app-actions\"><a class=\"button app-deeplink recommended\" href=\"{incy_url}\" data-app=\"incy\">⚡ Открыть в INCY</a><a class=\"button secondary app-deeplink\" href=\"{happ_url}\" data-app=\"happ\">Открыть в HAPP</a></div></div><div class=\"card cabinet-card cabinet-app-card\" data-os=\"windows\"><div class=\"cabinet-app-icon\">🪟</div><h2>Windows</h2><p class=\"muted\">Откройте подписку прямо в установленном VPN-клиенте.</p><div class=\"cabinet-app-actions\"><a class=\"button app-deeplink recommended\" href=\"{happ_url}\" data-app=\"happ\">⚡ Открыть в HAPP</a><a class=\"button secondary app-deeplink\" href=\"{incy_url}\" data-app=\"incy\">Открыть в INCY</a></div></div></section><section class=\"card cabinet-card\"><h2>Не открылось?</h2><p class=\"muted\">Если приложение не запустилось, это обычно означает, что HAPP или INCY не установлено либо браузер заблокировал переход. В таком случае можно скопировать ссылку подписки.</p><button id=\"cabinet-copy-sub\" class=\"button secondary\" type=\"button\">📋 Скопировать ссылку подписки</button><div id=\"cabinet-copy-result\" class=\"muted cabinet-result\" aria-live=\"polite\"></div></section></main></div><script>(function(){{const ua=navigator.userAgent||'',cards=[...document.querySelectorAll('.cabinet-app-card')];const os=/iPhone|iPad|iPod/i.test(ua)?'ios':(/Android/i.test(ua)?'android':(/Windows/i.test(ua)?'windows':''));cards.forEach(card=>{{if(os&&card.dataset.os===os)card.classList.add('detected')}});const copy=document.getElementById('cabinet-copy-sub'),out=document.getElementById('cabinet-copy-result'),sub={json.dumps(sub_url)};copy?.addEventListener('click',async()=>{{try{{await navigator.clipboard.writeText(sub);out.textContent='✅ Ссылка скопирована'}}catch(e){{out.textContent='❌ Не удалось скопировать ссылку'}}}});document.querySelectorAll('.app-deeplink').forEach(link=>link.addEventListener('click',()=>{{setTimeout(()=>{{if(document.visibilityState==='visible')out.textContent='Если приложение не открылось, установите выбранный клиент или используйте кнопку копирования ниже.'}},1200)}}))}})();</script></body></html>"""
    )

@app.get("/messages", response_class=HTMLResponse)
def messages_page(request: Request, tg_id: int = 0, direction: str = "all"):
    require_auth(request)
    direction = direction if direction in {"all", "in", "out", "system"} else "all"
    participants = user_events.conversation_users(db_path=config.DB_PATH)
    selected = int(tg_id or 0)
    events = user_events.all_events(limit=500, tg_id=selected if selected > 0 else None, direction=direction, db_path=config.DB_PATH)
    unread_snapshot = user_events.unread_messages_summary(db_path=config.DB_PATH)
    unread_map = {int(item["tg_id"]): item for item in (unread_snapshot.get("items") or [])}
    selected_name = next((str(x["username"]) for x in participants if int(x["tg_id"]) == selected), f"id_{selected}" if selected else "Все сообщения")
    user_rows = []
    for item in participants:
        tid = int(item["tg_id"]); active = "active" if tid == selected else ""
        unread_count = max(0, int((unread_map.get(tid) or {}).get("count") or 0))
        badge = f'<span class="message-unread-badge">{unread_count}</span>' if unread_count else ''
        user_rows.append('<a class="message-user-row %s%s" href="/messages?tg_id=%s&direction=%s"><span class="message-user-main"><strong>@%s</strong><span>#%s</span></span>%s<small>%s входящих · %s исходящих</small></a>' % (active, ' has-unread' if unread_count else '', tid, quote(direction), html.escape(str(item["username"])), tid, badge, item["incoming_events"], item["outgoing_events"]))
    event_rows = []
    if selected > 0:
        incoming_visible = [int(event.get("id") or 0) for event in events if str(event.get("direction") or "") == "in"]
        if incoming_visible:
            try:
                user_events.mark_messages_read(selected, through_event_id=max(incoming_visible), db_path=config.DB_PATH)
            except Exception as error:
                LOGGER.warning("Не удалось подтвердить прочитанные сообщения %s: %s", selected, error)
    for event in reversed(events):
        tid = int(event.get("tg_id") or 0); css = str(event.get("direction") or "system")
        label = {"in":"← Входящее", "out":"→ Исходящее", "system":"• Система"}.get(css, css)
        text = html.escape(str(event.get("text") or "").strip())
        uname = html.escape(str(event.get("display_username") or f"id_{tid}"))
        at = html.escape(fmt_event_timestamp(event.get("created_at")))
        event_rows.append(f'<div class="chat-message {css}"><div class="muted">{label} · @{uname} · {at} · #{int(event.get("id") or 0)}</div><div class="chat-message-text">{text}</div></div>')
    filters = []
    for key,label in (("all","Все"),("in","Входящие"),("out","Исходящие"),("system","Система")):
        cls = "" if direction == key else "secondary"
        filters.append(f'<a class="button small {cls}" href="/messages?tg_id={selected}&direction={key}">{label}</a>')
    card_link = f'<a class="button secondary small" href="/users/id/{selected}">Карточка пользователя</a>' if selected else ''
    body = f'''<header class="page-header"><div><h1>Сообщения</h1><div class="subtitle">Компактный центр переписки · {len(participants)} собеседников · {len(events)} событий в выборке</div></div><div class="actions">{" ".join(filters)} {card_link}</div></header>
<div class="messages-layout messages-layout-modern"><section class="card messages-users"><div class="compact-panel-head"><div><strong>Диалоги</strong><span class="muted">Нажмите пользователя для открытия чата</span></div><span class="badge">{len(participants)}</span></div><label class="messages-search"><span>⌕</span><input type="search" placeholder="Поиск по @username или ID" data-message-search autocomplete="off"></label><div class="messages-users-list" data-message-list>{"".join(user_rows) or '<p class="muted">Сообщений ещё нет.</p>'}</div></section>
<section class="card messages-history"><div class="chat-head"><div><strong>@{html.escape(selected_name)}</strong><span class="muted">{len(events)} событий</span></div>{card_link}</div><div class="chat-window">{"".join(event_rows) or '<div class="muted">Для выбранного пользователя сообщений нет.</div>'}</div></section></div>''' 
    scripts = """<script>(function(){const input=document.querySelector('[data-message-search]');const list=document.querySelector('[data-message-list]');if(input&&list){input.addEventListener('input',()=>{const q=input.value.trim().toLowerCase();list.querySelectorAll('.message-user-row').forEach(row=>{row.hidden=q&&!row.textContent.toLowerCase().includes(q)});});}let timer=0,inflight=false;const refresh=async()=>{if(inflight)return;inflight=true;try{const r=await fetch('""" + public_path('/api/panel/messages/unread') + """',{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}});if(!r.ok)return;const d=await r.json();document.querySelectorAll('[data-unread-total]').forEach(b=>{const n=Math.max(0,Number(d.total)||0);b.textContent=n?String(n):'';b.hidden=!n;});const map=new Map((Array.isArray(d.items)?d.items:[]).map(x=>[Number(x.tg_id),Number(x.count)||0]));document.querySelectorAll('.message-user-row').forEach(row=>{const m=(row.getAttribute('href')||'').match(/[?&]tg_id=(\\d+)/);if(!m)return;const n=map.get(Number(m[1]))||0;let badge=row.querySelector('.message-unread-badge');if(n){if(!badge){badge=document.createElement('span');badge.className='message-unread-badge';row.insertBefore(badge,row.querySelector('small'));}badge.textContent=String(n);row.classList.add('has-unread');}else{badge?.remove();row.classList.remove('has-unread');}});}catch(_e){}finally{inflight=false;timer=setTimeout(refresh,15000);}};refresh();window.addEventListener('pagehide',()=>clearTimeout(timer),{once:true});})();</script>"""
    return page(request, "Сообщения", body, "messages", scripts)


@app.get("/users/new", response_class=HTMLResponse)
def new_user_page(request: Request):
    require_auth(request)
    body = '''<header><div><h1>Новый пользователь</h1><div class="subtitle">Создание доступа одновременно в 3x-ui и базе бота</div></div></header><div class="card" style="max-width:650px"><form method="post"><div class="setting"><label>Telegram ID <span class="muted">(необязательно)</span></label><input name="tg_id" type="number" min="1" placeholder="Можно оставить пустым"><div class="muted" style="margin-top:5px">Если Telegram ID пока неизвестен, пользователь создаётся без Telegram-привязки. Позже ID можно привязать в карточке.</div></div><div class="setting"><label>Имя пользователя</label><input name="username" required maxlength="80" placeholder="Например: Иван"></div><div class="setting"><label>Количество дней</label><input name="days" type="number" value="30" min="1" max="3650" required></div><button>Создать доступ</button></form></div>'''
    return page(request, "Новый пользователь", body, "users")


@app.post("/users/new")
def create_user(request: Request, tg_id: str = Form(""), username: str = Form(), days: int = Form(30)):
    require_auth(request)
    username = username.strip().replace("@", "")[:80]
    text_id = str(tg_id or "").strip()
    try:
        parsed = int(text_id) if text_id else 0
    except ValueError as exc:
        raise HTTPException(400, "Telegram ID должен быть числом или пустым") from exc
    if parsed < 0 or not username or not 1 <= days <= 3650:
        raise HTTPException(400, "Некорректные параметры")
    if parsed == 0:
        with database() as connection:
            row = connection.execute("SELECT MIN(tg_id) FROM users WHERE tg_id<0").fetchone()
            parsed = min(-1, int(row[0] or 0) - 1)
    try:
        result = ensure_subscription_sync(parsed, username, days=days, db_path=config.DB_PATH)
        email = result.email
        fetch_and_sync(force=True, db_path=config.DB_PATH)
    except Exception as error:
        raise HTTPException(502, f"3x-ui не приняла создание пользователя: {error}") from error
    audit(str(request.session.get("user", "web")), "create_user", email)
    user_events.safe_record_event(parsed, username=username, direction="system", event_type="subscription_created" if result.created else "subscription_extended", text=f"Доступ {'создан' if result.created else 'продлён'} на {days} дн.", actor=str(request.session.get("user", "web")), metadata={"email": email, "days": days, "telegram_linked": parsed > 0}, db_path=config.DB_PATH)
    set_flash(request, f"Пользователь {username} {'создан' if result.created else 'найден и продлён'}" + (" без Telegram-привязки" if parsed < 0 else ""))
    return RedirectResponse(public_path("/users"), 303)


@app.post("/users/{tg_id}/days")
def adjust_days(request: Request, tg_id: int, days: int = Form()):
    require_auth(request)
    if days == 0 or not -3650 <= days <= 3650:
        raise HTTPException(400, "Укажите число дней от -3650 до 3650, кроме нуля")
    row = get_user(tg_id)
    if not row:
        raise HTTPException(404)
    try:
        result = change_client_days_sync(
            str(row["email"]), days_delta=days, tg_id=tg_id if tg_id > 0 else None
        )
        with database() as connection:
            connection.execute(
                "UPDATE users SET expiry_time=?,enable=?,last_reminder_days=-1 WHERE tg_id=?",
                (int(result.get("expiry_time") or 0), int(bool(result.get("enable"))), tg_id),
            )
        fetch_and_sync(force=True, db_path=config.DB_PATH)
    except Exception as error:
        raise HTTPException(502, f"3x-ui отклонила изменение срока: {error}") from error
    audit(str(request.session.get("user", "web")), "adjust_user_days", f"{tg_id}: {days:+d}")
    user_events.safe_record_event(
        tg_id,
        username=str(row.get("username") or ""),
        direction="system",
        event_type="subscription_days_changed",
        text=f"Срок изменён на {days:+d} дн.; новый срок {fmt_date(int(result.get('expiry_time') or 0))}",
        actor=str(request.session.get("user", "web")),
        metadata={"days_delta": days, "expiry_time": int(result.get("expiry_time") or 0)},
        db_path=config.DB_PATH,
    )
    verb = "Добавлено" if days > 0 else "Убавлено"
    set_flash(request, f"{verb} {abs(days)} дн. Новый срок: {fmt_date(int(result.get('expiry_time') or 0))}")
    return RedirectResponse(public_path("/users"), 303)


@app.get("/users/{tg_id}/bind-telegram", response_class=HTMLResponse)
def bind_telegram_page(request: Request, tg_id: int):
    require_auth(request)
    if not get_user(tg_id):
        raise HTTPException(404)
    return RedirectResponse(public_path(f"/users/id/{tg_id}"), 303)


@app.post("/users/{tg_id}/bind-telegram")
def bind_telegram_user(
    request: Request,
    tg_id: int,
    new_tg_id: int = Form(),
    username: str = Form(""),
):
    require_auth(request)
    try:
        result = identity_migration.rebind_local_identity(
            tg_id,
            new_tg_id,
            username.strip().lstrip("@"),
            source=f"manual:{request.session.get('user', 'web')}",
            db_path=config.DB_PATH,
        )
        if result.get("email"):
            bind_client_tg_id_sync(str(result["email"]), int(result["tg_id"]))
    except Exception as error:
        set_flash(request, f"Не удалось привязать Telegram ID: {error}", "bad")
        return RedirectResponse(public_path(f"/users/id/{tg_id}"), 303)
    audit(str(request.session.get("user", "web")), "bind_telegram_id", f"{tg_id} -> {new_tg_id}")
    user_events.safe_record_event(
        int(result["tg_id"]),
        username=str(result.get("username") or username),
        direction="system",
        event_type="identity_updated",
        text=f"Telegram ID привязан: {tg_id} → {result['tg_id']}",
        actor=str(request.session.get("user", "web")),
        db_path=config.DB_PATH,
    )
    set_flash(request, f"Пользователь привязан к Telegram ID {result['tg_id']}")
    return RedirectResponse(public_path(f"/users/id/{int(result['tg_id'])}"), 303)


@app.post("/users/{tg_id}/toggle")
def toggle_user(request: Request, tg_id: int):
    require_auth(request)
    row = get_user(tg_id)
    if not row:
        raise HTTPException(404)
    new_state = not bool(row["enable"])
    try:
        set_client_status_sync(str(row["email"]), new_state)
        fetch_and_sync(force=True, db_path=config.DB_PATH)
    except Exception as error:
        raise HTTPException(502, f"3x-ui отклонила изменение статуса: {error}") from error
    audit(str(request.session.get("user", "web")), "toggle_user", f"{tg_id}: {new_state}")
    user_events.safe_record_event(
        tg_id,
        username=str(row.get("username") or ""),
        direction="system",
        event_type="access_enabled" if new_state else "access_blocked",
        text="Доступ включён" if new_state else "Доступ заблокирован",
        actor=str(request.session.get("user", "web")),
        db_path=config.DB_PATH,
    )
    set_flash(request, "Пользователь включён" if new_state else "Пользователь заблокирован")
    return RedirectResponse(public_path("/users"), 303)


@app.post("/users/{tg_id}/delete")
def delete_user(request: Request, tg_id: int):
    require_auth(request)
    row = get_user(tg_id)
    if not row:
        raise HTTPException(404)
    user_events.safe_record_event(
        tg_id,
        username=str(row.get("username") or ""),
        direction="system",
        event_type="user_deleted",
        text="Пользователь удалён из платформы и 3x-ui",
        actor=str(request.session.get("user", "web")),
        metadata={"email": str(row.get("email") or "")},
        db_path=config.DB_PATH,
    )
    try:
        delete_client_sync(str(row["email"]), keep_traffic=False)
        with database() as connection:
            connection.execute("DELETE FROM users WHERE tg_id=?", (tg_id,))
    except Exception as error:
        raise HTTPException(502, f"3x-ui отклонила удаление: {error}") from error
    audit(str(request.session.get("user", "web")), "delete_user", str(tg_id))
    set_flash(request, "Пользователь удалён")
    return RedirectResponse(public_path("/users"), 303)


@app.get("/users/{tg_id}/message", response_class=HTMLResponse)
def message_user_page(request: Request, tg_id: int):
    require_auth(request)
    if not get_user(tg_id):
        raise HTTPException(404)
    return RedirectResponse(public_path(f"/users/id/{tg_id}"), 303)


@app.post("/users/{tg_id}/message")
def message_user(
    request: Request,
    tg_id: int,
    message: str = Form(""),
    media: UploadFile | None = File(None),
):
    require_auth(request)
    row = get_user(tg_id)
    if not row:
        raise HTTPException(404)
    message = message.strip()
    has_media = bool(media and str(media.filename or "").strip())
    if tg_id <= 0 or (not message and not has_media) or len(message) > 4096:
        raise HTTPException(400, "Введите сообщение или выберите фото/видео")
    if has_media and len(message) > 1024:
        raise HTTPException(400, "Подпись к фото или видео не должна превышать 1024 символа")

    metadata: dict[str, Any] = {}
    if has_media and media is not None:
        ok, detail, metadata, event_type, event_text = telegram_send_media(tg_id, media, message)
    else:
        ok, detail = telegram_send(tg_id, message)
        event_type, event_text = "admin_message", message
    actor = str(request.session.get("user", "web"))
    with database() as connection:
        connection.execute(
            "INSERT INTO message_log(actor,tg_id,username,message,success,detail) VALUES(?,?,?,?,?,?)",
            (actor, tg_id, row.get("username"), event_text, int(ok), detail),
        )
    event_metadata = dict(metadata)
    event_metadata["telegram_result"] = detail
    event_id = user_events.safe_record_event(
        tg_id,
        username=str(row.get("username") or ""),
        direction="out",
        event_type=event_type,
        text=event_text,
        actor=actor,
        success=ok,
        metadata=event_metadata,
        db_path=config.DB_PATH,
    )
    event = None
    if event_id:
        recorded = user_events.recent_events(
            tg_id, limit=1, after_id=max(0, int(event_id) - 1), db_path=config.DB_PATH
        )
        event = event_for_web(recorded[0], tg_id) if recorded else None
    try:
        if media is not None:
            media.file.close()
    except Exception:
        pass
    audit(actor, "message_user", f"{tg_id}: {detail}")
    wants_json = "application/json" in request.headers.get("accept", "")
    if not ok:
        if wants_json:
            return JSONResponse(
                {"ok": False, "detail": f"Telegram не принял сообщение: {detail}", "event": event},
                status_code=502,
            )
        set_flash(request, f"Telegram не принял сообщение: {detail}", "bad")
        return RedirectResponse(public_path(f"/users/id/{tg_id}"), 303)
    if wants_json:
        return {"ok": True, "detail": detail, "event": event}
    set_flash(request, "Сообщение отправлено пользователю")
    return RedirectResponse(public_path(f"/users/id/{tg_id}"), 303)

@app.get("/broadcast", response_class=HTMLResponse)
def broadcast_page(request: Request):
    require_auth(request)
    status = broadcast_manager.read_status()
    busy = broadcast_manager.broadcast_busy(status)
    state = str(status.get("state") or "idle")
    labels = {
        "idle": "Ожидание",
        "queued": "В очереди",
        "running": "Отправка",
        "completed": "Завершено",
        "failed": "Ошибка",
    }
    progress = max(0, min(100, int(status.get("progress") or 0)))
    state_class = "bad" if state == "failed" else ("warn" if busy else "good")
    failures = status.get("failure_examples") if isinstance(status.get("failure_examples"), list) else []
    failure_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in failures[:10])
    detail_html = (
        f'<details class="broadcast-errors"><summary>Показать примеры ошибок</summary><ul>{failure_html}</ul></details>'
        if failure_html else ""
    )
    media_limit = max(1, int(getattr(config, "BROADCAST_MEDIA_MAX_MB", 45)))
    body = f'''<header><div><h1>Массовая рассылка</h1><div class="subtitle">Единый редактор: текст можно отправить отдельно или вместе с фотографией, видео либо файлом</div></div><a class="button secondary" href="/users">К пользователям</a></header>
<div class="grid"><div class="card half"><h2>Новое сообщение</h2><p class="muted">Поддерживаются: Текст, Фотография, Видео и Документ. Добавьте текст и при необходимости одно вложение: Telegram отправит их одним сообщением, а текст станет подписью к файлу.</p>
<form id="broadcast-form" method="post" action="/broadcast/start" enctype="multipart/form-data">
<div class="setting"><label id="broadcast-message-label">Текст сообщения</label><textarea id="broadcast-message" name="message" maxlength="4096" rows="7" placeholder="Введите текст сообщения"></textarea><div id="broadcast-limit" class="muted">До 4096 символов без вложения; до 1024 символов с вложением.</div></div>
<div class="setting broadcast-composer-file"><label>Фотография, видео или файл (необязательно)</label><input id="broadcast-file" type="file" name="media"><div id="broadcast-file-hint" class="muted">Можно прикрепить изображение, видео, PDF, архив или другой документ. Максимальный размер: {media_limit} МБ.</div></div>
<label class="check-row"><input type="checkbox" name="confirm" value="1" required> Подтверждаю отправку всем пользователям с положительным Telegram ID</label>
<button id="broadcast-start" {'disabled' if busy else ''}>Запустить рассылку</button>
<div id="broadcast-upload" class="file-progress"><div class="progress large"><span id="broadcast-upload-bar" style="width:0%"></span></div><div id="broadcast-upload-text" class="muted">Передача сообщения…</div></div>
</form></div>
<div class="card half"><div class="section-title"><h2>Текущий результат</h2><span id="broadcast-state" class="badge {state_class}">{html.escape(labels.get(state, state))}</span></div>
<div class="status-panel"><div class="progress large"><span id="broadcast-progress-bar" style="width:{progress}%"></span></div><div class="progress-meta"><span id="broadcast-message-status">{html.escape(str(status.get('message') or 'Рассылка ещё не запускалась'))}</span><strong id="broadcast-progress-value">{progress}%</strong></div>
<div class="broadcast-counters"><span>Всего: <strong id="broadcast-total">{int(status.get('total') or 0)}</strong></span><span>Обработано: <strong id="broadcast-processed">{int(status.get('processed') or 0)}</strong></span><span>Доставлено: <strong id="broadcast-delivered">{int(status.get('delivered') or 0)}</strong></span><span>Ошибок: <strong id="broadcast-failed">{int(status.get('failed') or 0)}</strong></span></div>
<div id="broadcast-error" class="notice" style="{'display:block' if status.get('error') else 'display:none'};margin-top:14px">{html.escape(str(status.get('error') or ''))}</div>{detail_html}</div>
<p><a class="button secondary small" href="/api/broadcast/log" target="_blank">Открыть журнал рассылки</a></p></div></div>'''
    initial = json.dumps(status, ensure_ascii=False, default=str).replace("<", "\\u003c")
    script = r'''<script>
(function(){
const initial=__INITIAL__;const busyStates=new Set(['queued','running']);
const labels={idle:'Ожидание',queued:'В очереди',running:'Отправка',completed:'Завершено',failed:'Ошибка'};
const file=document.getElementById('broadcast-file');const message=document.getElementById('broadcast-message');const messageLabel=document.getElementById('broadcast-message-label');const limit=document.getElementById('broadcast-limit');const fileHint=document.getElementById('broadcast-file-hint');
function updateFields(){const attached=Boolean(file.files&&file.files.length);message.required=!attached;message.maxLength=attached?1024:4096;messageLabel.textContent=attached?'Текст сообщения / подпись к вложению':'Текст сообщения';limit.textContent=attached?'Текст и вложение будут отправлены одним сообщением. Подпись — до 1024 символов.':'До 4096 символов. Можно добавить фотографию, видео или файл.';if(attached){const selected=file.files[0];const type=String(selected.type||'');const kind=type.startsWith('image/')?'фотография':(type.startsWith('video/')?'видео':'файл');fileHint.textContent='Выбрано: '+selected.name+' ('+kind+'). Тип отправки будет определён автоматически.';}else{fileHint.textContent='Можно прикрепить изображение, видео, PDF, архив или другой документ.';}}
file.addEventListener('change',updateFields);updateFields();
function render(data){data=data||{};const state=data.state||'idle';const progress=Math.max(0,Math.min(100,Number(data.progress||0)));document.getElementById('broadcast-progress-bar').style.width=progress+'%';document.getElementById('broadcast-progress-value').textContent=Math.round(progress)+'%';document.getElementById('broadcast-message-status').textContent=data.message||'Ожидание запуска';const badge=document.getElementById('broadcast-state');badge.textContent=labels[state]||state;badge.className='badge '+(state==='failed'?'bad':(busyStates.has(state)?'warn':'good'));document.getElementById('broadcast-error').textContent=data.error||'';document.getElementById('broadcast-error').style.display=data.error?'block':'none';['total','processed','delivered','failed'].forEach((name)=>{document.getElementById('broadcast-'+name).textContent=Number(data[name]||0);});document.getElementById('broadcast-start').disabled=busyStates.has(state);return state;}
let lastState=render(initial);
async function poll(){try{const response=await fetch('/api/broadcast/status',{cache:'no-store'});if(response.ok)lastState=render(await response.json());}catch(_e){}finally{setTimeout(poll,busyStates.has(lastState)?1200:5000);}}
document.getElementById('broadcast-form').addEventListener('submit',(event)=>{event.preventDefault();const hasFile=Boolean(file.files&&file.files.length);const textValue=(message.value||'').trim();if(!hasFile&&!textValue){alert('Введите текст сообщения или прикрепите файл');return;}if(hasFile&&textValue.length>1024){alert('При наличии вложения текст должен быть не длиннее 1024 символов');return;}if(!window.confirm('Запустить массовую рассылку всем пользователям?'))return;const form=event.currentTarget;const xhr=new XMLHttpRequest();const upload=document.getElementById('broadcast-upload');const bar=document.getElementById('broadcast-upload-bar');const statusText=document.getElementById('broadcast-upload-text');const button=document.getElementById('broadcast-start');upload.classList.add('visible');button.disabled=true;statusText.textContent='Передача данных на сервер…';xhr.open('POST',form.action);xhr.setRequestHeader('Accept','application/json');xhr.upload.onprogress=(e)=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);bar.style.width=p+'%';statusText.textContent='Загружено '+p+'%';}};xhr.onload=()=>{let data={};try{data=JSON.parse(xhr.responseText)}catch(_e){}if(xhr.status>=200&&xhr.status<300){bar.style.width='100%';statusText.textContent='Сообщение принято, рассылка запущена';lastState=render(data.status||data);}else{button.disabled=false;statusText.textContent='Ошибка: '+(data.detail||data.error||'HTTP '+xhr.status);}};xhr.onerror=()=>{button.disabled=false;statusText.textContent='Соединение прервано. Статус будет проверен автоматически.';};xhr.send(new FormData(form));});
setTimeout(poll,600);
})();
</script>'''.replace('__INITIAL__', initial)
    return page(request, "Массовая рассылка", body, "broadcast", script)


@app.get("/api/broadcast/status")
def broadcast_status_api(request: Request):
    require_auth(request)
    status = broadcast_manager.read_status()
    return {**status, "state": str(status.get("state") or "idle")}


@app.get("/api/broadcast/log")
def broadcast_log_api(request: Request):
    require_auth(request)
    status = broadcast_manager.read_status()
    job_id = str(status.get("job_id") or "")
    path = broadcast_manager.log_path(job_id) if job_id else Path("")
    if not job_id or not path.is_file():
        return PlainTextResponse("Журнал рассылки пока пуст.")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename="vpn-service-broadcast.log")


@app.post("/broadcast/start")
def broadcast_start(
    request: Request,
    kind: str = Form("auto"),
    message: str = Form(""),
    confirm: str = Form(""),
    media: UploadFile | None = File(None),
):
    require_auth(request)
    if confirm != "1":
        raise HTTPException(400, "Необходимо подтвердить массовую рассылку")
    if broadcast_manager.broadcast_busy():
        return JSONResponse(
            {"ok": False, "detail": "Другая массовая рассылка уже выполняется"},
            status_code=409,
        )
    max_size = max(1, int(getattr(config, "BROADCAST_MEDIA_MAX_MB", 45))) * 1024 * 1024
    temp_path: Path | None = None
    try:
        if media is not None and str(media.filename or "").strip():
            suffix = "".join(Path(media.filename or "broadcast.bin").suffixes[-2:])[:20] or ".bin"
            with tempfile.NamedTemporaryFile(prefix="vpn_broadcast_", suffix=suffix, delete=False) as handle:
                temp_path = Path(handle.name)
                total = 0
                while True:
                    chunk = media.file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_size:
                        raise broadcast_manager.BroadcastError(
                            f"Файл рассылки превышает допустимый размер {max_size // (1024 * 1024)} МБ"
                        )
                    handle.write(chunk)
        actor = str(request.session.get("user", "web"))
        result = broadcast_manager.start_broadcast(
            actor=actor,
            message=message,
            kind="auto" if temp_path is not None else "text",
            source=temp_path,
            original_name=str(media.filename or "") if media is not None else "",
            content_type=str(media.content_type or "") if media is not None else "",
        )
        audit(actor, "web_broadcast_started", result["job_id"])
        return JSONResponse({"ok": True, **result}, status_code=202)
    except broadcast_manager.BroadcastError as error:
        return JSONResponse({"ok": False, "detail": str(error)}, status_code=409)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        if media is not None:
            media.file.close()


@app.get("/payments", response_class=HTMLResponse)
def payments(request: Request):
    require_auth(request)
    with database() as connection:
        rows = connection.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 500").fetchall()
    table_rows: list[str] = []
    for row in rows:
        actions = f'<a class="button small secondary" href="/payments/{row["id"]}/receipt" target="_blank">Чек</a>'
        if row["status"] == "pending":
            actions += f' <form class="inline" method="post" action="/payments/{row["id"]}/approve"><button class="small">Подтвердить</button></form> <form class="inline" method="post" action="/payments/{row["id"]}/decline"><button class="small danger">Отклонить</button></form>'
        if row["status"] == "declined":
            badge = '<span class="badge bad">Отклонён</span>'
        elif row["status"] == "approved":
            badge = '<span class="badge good">Подтверждён</span>'
        elif row["status"] == "processing":
            badge = '<span class="badge warn">Обрабатывается</span>'
        else:
            badge = '<span class="badge warn">Ожидает</span>'
        if int(row["auto_approved"] or 0):
            badge += '<br><span class="badge good" style="margin-top:5px">Автоматически</span>'
        ocr_status = str(row["ocr_status"] or "not_checked")
        ocr_badges = {
            "passed": '<span class="badge good">Все совпало</span>',
            "duplicate": '<span class="badge bad">Дубликат</span>',
            "error": '<span class="badge bad">Ошибка OCR</span>',
            "manual": '<span class="badge warn">Нужна проверка</span>',
            "not_checked": '<span class="badge warn">Не проверен</span>',
        }
        ocr_badge = ocr_badges.get(ocr_status, '<span class="badge warn">Нужна проверка</span>')
        recognized = []
        if row["receipt_receiver"]:
            recognized.append(f'имя: {html.escape(str(row["receipt_receiver"]))}')
        if row["receipt_phone"]:
            recognized.append(f'номер: {html.escape(str(row["receipt_phone"]))}')
        if row["receipt_date"]:
            recognized.append(f'дата: {html.escape(str(row["receipt_date"]))}')
        ocr_note = '<br><small>' + '<br>'.join(recognized) + '</small>' if recognized else ''
        error_note = ""
        if row["last_error"]:
            error_note = f'<br><small style="color:var(--red)">{html.escape(str(row["last_error"]))}</small>'
        actual_amount = row["receipt_amount"] if row["receipt_amount"] is not None else row["amount"]
        amount_text = f"{float(actual_amount):g} ₽" if actual_amount is not None else "—"
        table_rows.append(
            f'<tr><td>#{row["id"]}</td><td>@{html.escape(row["username"] or "нет")}<br><small>ID {row["tg_id"]}</small></td><td>{amount_text}</td><td>{html.escape(row["created_at"] or "")}</td><td>{ocr_badge}{ocr_note}</td><td>{badge}{error_note}</td><td>{actions}</td></tr>'
        )
    body = f'''<header><div><h1>Платежи</h1><div class="subtitle">Все чеки в одном списке: новые платежи всегда сверху, статус и результат OCR видны в строке</div></div><span class="badge">{len(rows)} записей</span></header><div class="card table-wrap payments-table"><table><thead><tr><th>ID</th><th>Пользователь</th><th>Сумма чека</th><th>Получен</th><th>OCR</th><th>Статус</th><th>Действия</th></tr></thead><tbody>{''.join(table_rows) or '<tr><td colspan="7"><div class="empty-state">Платежей пока нет.</div></td></tr>'}</tbody></table></div>'''
    return page(request, "Платежи", body, "payments")


@app.get("/payments/{payment_id}/receipt", response_class=HTMLResponse)
def receipt(request: Request, payment_id: int):
    require_auth(request)
    with database() as connection:
        row = connection.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if not row["telegram_file_id"]:
        raise HTTPException(404)
    amount = "—" if row["receipt_amount"] is None else f"{float(row['receipt_amount']):g} ₽"
    status = html.escape(str(row["ocr_status"] or "not_checked"))
    try:
        details = json.loads(str(row["ocr_details"] or "{}"))
    except (ValueError, TypeError):
        details = {}
    filters = details.get("filters") or {}
    checks = [
        ("Имя получателя", bool(details.get("name_match")), bool(filters.get("name", True))),
        ("Номер получателя", bool(details.get("phone_match")), bool(filters.get("phone", True))),
        ("Сумма не ниже порога", bool(details.get("amount_match")), bool(filters.get("amount", True))),
        ("Свежая дата", bool(details.get("date_match")), bool(filters.get("date", True))),
    ]
    checks_html = "".join(
        f'<div class="check-row"><span class="badge {"good" if (not enabled or ok) else "bad"}">{"○" if not enabled else ("✓" if ok else "✗")}</span><span>{html.escape(label)}{" (фильтр выключен)" if not enabled else ""}</span></div>'
        for label, ok, enabled in checks
    )
    duplicate = details.get("duplicate_payment_id")
    duplicate_html = f'<div class="notice">Этот чек уже использовался или совпал с платежом #{int(duplicate)}.</div>' if duplicate else ""
    reasons = details.get("reasons") or []
    reasons_html = "" if not reasons else '<p class="muted">Причины ручной проверки: ' + html.escape("; ".join(map(str, reasons))) + '</p>'
    ocr_text = html.escape(str(row["ocr_text"] or "Текст не распознан"))
    body = f'''<header><div><h1>Чек #{payment_id}</h1><div class="subtitle">@{html.escape(str(row["username"] or "нет"))} · Telegram ID {row["tg_id"]} · {html.escape(str(row["created_at"] or ""))}</div></div><a class="button secondary" href="/payments">Назад</a></header>{duplicate_html}<div class="grid"><div class="card wide" style="text-align:center"><img src="/payments/{payment_id}/receipt/image" alt="Чек #{payment_id}" style="display:block;max-width:100%;max-height:78vh;margin:auto;border-radius:10px;object-fit:contain" loading="eager"></div><div class="card side"><h2>Результат OCR</h2><p>Статус: <span class="code">{status}</span></p><p>Сумма: <strong>{amount}</strong><br>Дата: <strong>{html.escape(str(row["receipt_date"] or "—"))}</strong><br>Имя: <strong>{html.escape(str(row["receipt_receiver"] or "—"))}</strong><br>Номер: <strong>{html.escape(str(row["receipt_phone"] or "—"))}</strong></p>{checks_html}{reasons_html}</div><div class="card full"><h2>Распознанный текст</h2><pre>{ocr_text}</pre></div></div>'''
    return page(request, f"Чек #{payment_id}", body, "payments")


@app.get("/payments/{payment_id}/receipt/image")
def receipt_image(request: Request, payment_id: int):
    require_auth(request)
    content, media_type, filename = telegram_receipt_file(payment_id)
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(len(content)),
        },
    )


def approve_payment_logic(payment_id: int) -> tuple[bool, str]:
    result = approve_payment_sync(payment_id, "web", days=30, db_path=config.DB_PATH)
    if not result.success:
        return False, result.message
    if result.already_processed:
        referral = result.referral or {}
        if referral.get("status") == referral_rewards.GRANTED and not referral.get("already_processed"):
            referrer_id = int(referral.get("referrer_tg_id") or 0)
            if referrer_id > 0:
                telegram_send(
                    referrer_id,
                    f"🎉 Новый реферал!\n\nПриглашённый пользователь впервые успешно оплатил подписку.\nВам начислено +{referral_rewards.REWARD_DAYS} бесплатных дней.",
                )
        return True, "Платёж уже был подтверждён; повторное продление не выполнено"
    subscription = result.subscription
    if not subscription:
        return False, "Не получены данные активированной подписки"
    action = "активирована" if subscription.created else "продлена"
    notification = (
        f"🎉 Ваша подписка {config.SERVICE_NAME} успешно {action} "
        f"до {fmt_date(subscription.expiry_time)}!"
    )
    sent, detail = telegram_send(subscription.tg_id, notification)
    referral = result.referral or {}
    referral_notice = ""
    if referral.get("status") == referral_rewards.GRANTED and not referral.get("already_processed"):
        referrer_id = int(referral.get("referrer_tg_id") or 0)
        if referrer_id > 0:
            ok_ref, detail_ref = telegram_send(
                referrer_id,
                f"🎉 Новый реферал!\n\nПриглашённый пользователь впервые успешно оплатил подписку.\nВам начислено +{referral_rewards.REWARD_DAYS} бесплатных дней.",
            )
            if not ok_ref:
                referral_notice = f" Реферальный бонус начислен, но уведомление не доставлено: {detail_ref}"
    if not sent:
        return True, f"Подписка {action}, но Telegram-уведомление не доставлено: {detail}.{referral_notice}"
    return True, f"Подписка {action}.{referral_notice}"


@app.post("/payments/{payment_id}/approve")
def approve_payment(request: Request, payment_id: int):
    require_auth(request)
    ok, message = approve_payment_logic(payment_id)
    if not ok:
        set_flash(request, message, "bad")
    else:
        audit(str(request.session.get("user", "web")), "approve_payment", str(payment_id))
        set_flash(request, message)
    return RedirectResponse(public_path("/payments"), 303)


@app.post("/payments/{payment_id}/decline")
def decline_payment(request: Request, payment_id: int):
    require_auth(request)
    try:
        payment = decline_payment_sync(payment_id, "web", db_path=config.DB_PATH)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except Exception as error:
        set_flash(request, str(error), "bad")
        return RedirectResponse(public_path("/payments"), 303)
    telegram_send(int(payment["tg_id"]), "❌ Платёж не подтверждён. Проверьте чек или обратитесь к администратору.")
    audit(str(request.session.get("user", "web")), "decline_payment", str(payment_id))
    set_flash(request, "Платёж отклонён")
    return RedirectResponse(public_path("/payments"), 303)


@app.get("/monitoring", response_class=HTMLResponse)
def monitoring(request: Request):
    require_auth(request)
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    boot = panel_from_timestamp(psutil.boot_time())
    load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else os.getloadavg()
    net = psutil.net_io_counters()
    control = fetch_control_snapshot_sync()
    control_status = control.get("status") if isinstance(control.get("status"), dict) else {}
    fail2ban = control.get("fail2ban") if isinstance(control.get("fail2ban"), dict) else {}
    nodes = control.get("nodes") if isinstance(control.get("nodes"), list) else []
    node_rows: list[str] = []
    for node in nodes[:12]:
        status = str(node.get("status") or "unknown").lower()
        css = "good" if status in {"online", "running", "ok", "active"} else ("bad" if status in {"offline", "error", "failed", "disabled"} else "warn")
        node_rows.append(f'<tr><td>{html.escape(str(node.get("name") or "Без имени"))}</td><td>{html.escape(str(node.get("address") or "—"))}</td><td><span class="badge {css}">{html.escape(status)}</span></td><td>{html.escape(str(node.get("version") or "—"))}</td></tr>')
    xui_state = str(control_status.get("xray_state") or "Нет данных")
    xui_state_class = "good" if xui_state.startswith("running") else "warn"
    xui_card = f'<div class="card half compact-xui-card"><div class="section-title"><h2>3x-ui</h2><span class="badge {xui_state_class}">{html.escape(xui_state)}</span></div><div class="numbers"><div><small>Xray</small><strong>{html.escape(str(control_status.get("xray_version") or "—"))}</strong></div><div><small>TCP-соединения</small><strong>{int(control_status.get("tcp_count") or 0)}</strong></div><div><small>Fail2ban</small><strong>{html.escape(str(fail2ban.get("status") or fail2ban.get("state") or "—"))}</strong></div></div></div>'
    nodes_card = ""
    if nodes:
        online_nodes = sum(1 for x in nodes if str(x.get("status") or "").lower() in {"online", "running", "ok", "active"})
        nodes_card = f'<div class="card full table-wrap compact-xui-card"><div class="section-title"><h2>Узлы 3x-ui</h2><span class="badge good">{online_nodes}/{len(nodes)} онлайн</span></div><table><thead><tr><th>Узел</th><th>Адрес</th><th>Статус</th><th>Версия</th></tr></thead><tbody>{"".join(node_rows)}</tbody></table></div>'
    processes: list[str] = []
    candidates = sorted(
        psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
        key=lambda process: process.info.get("memory_percent") or 0,
        reverse=True,
    )[:12]
    for process in candidates:
        processes.append(
            f'<tr><td>{process.info["pid"]}</td><td>{html.escape(process.info["name"] or "")}</td><td>{process.info.get("cpu_percent",0):.1f}%</td><td>{process.info.get("memory_percent",0):.1f}%</td></tr>'
        )
    body = f'''<header><div><h1>Мониторинг сервера</h1><div class="subtitle">CPU, RAM, Swap, диск и сеть · живое обновление каждые 2 секунды</div></div></header><div class="grid monitoring-grid"><div class="card metric"><div class="label">CPU</div><div id="mon-cpu" class="value">{psutil.cpu_percent():.0f}%</div><div id="mon-load" class="hint">Load: {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}</div></div><div class="card metric"><div class="label">RAM</div><div id="mon-ram" class="value">{vm.percent:.0f}%</div><div id="mon-ram-hint" class="hint">{fmt_bytes(vm.used)} из {fmt_bytes(vm.total)}</div></div><div class="card metric"><div class="label">Swap</div><div id="mon-swap" class="value">{swap.percent:.0f}%</div><div id="mon-swap-hint" class="hint">{fmt_bytes(swap.used)} из {fmt_bytes(swap.total)}</div></div><div class="card metric"><div class="label">Диск</div><div id="mon-disk" class="value">{disk.percent:.0f}%</div><div id="mon-disk-hint" class="hint">Свободно {fmt_bytes(disk.free)}</div></div><div class="card metric"><div class="label">Пользователи онлайн</div><div id="mon-online" class="value">—</div><div class="hint">Непосредственно из 3x-ui</div></div><div class="card wide chart-card"><div class="section-title"><h2>CPU в реальном времени</h2><span class="badge good">2 сек</span></div><div id="monitor-chart" class="chart"></div></div><div class="card side"><h2>Сетевые счётчики</h2><p style="font-size:25px">↓ <span id="mon-bytes-in">{fmt_bytes(net.bytes_recv)}</span></p><p style="font-size:25px">↑ <span id="mon-bytes-out">{fmt_bytes(net.bytes_sent)}</span></p><p class="subtitle">Uptime с <span id="mon-boot">{boot.strftime('%d.%m.%Y %H:%M')}</span></p><p class="subtitle">Данные процессов: снимок при открытии страницы</p></div><div class="card full table-wrap"><div class="section-title"><h2>Процессы по памяти</h2></div><table><thead><tr><th>PID</th><th>Процесс</th><th>CPU</th><th>RAM</th></tr></thead><tbody>{''.join(processes)}</tbody></table></div>{xui_card}{nodes_card}</div>'''
    script = '''<script>const a=[];async function t(){try{const [mr,or]=await Promise.all([fetch('/api/metrics',{cache:'no-store'}),fetch('/api/online-metrics',{cache:'no-store'})]);const d=await mr.json();const online=await or.json();a.push(Number(d.cpu)||0);if(a.length>50)a.shift();let w=800,h=210,p=a.map((v,i)=>`${i*w/Math.max(1,a.length-1)},${h-v*h/100}`).join(' ');document.getElementById('monitor-chart').innerHTML=`<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${p}"/></svg>`;document.getElementById('mon-cpu').textContent=Math.round(d.cpu||0)+'%';document.getElementById('mon-ram').textContent=Math.round(d.ram||0)+'%';document.getElementById('mon-swap').textContent=Math.round(d.swap||0)+'%';document.getElementById('mon-disk').textContent=Math.round(d.disk||0)+'%';document.getElementById('mon-load').textContent=`Load: ${(d.load1||0).toFixed(2)} / ${(d.load5||0).toFixed(2)} / ${(d.load15||0).toFixed(2)}`;document.getElementById('mon-ram-hint').textContent=`${formatBytes(d.ram_used)} из ${formatBytes(d.ram_total)}`;document.getElementById('mon-swap-hint').textContent=`${formatBytes(d.swap_used)} из ${formatBytes(d.swap_total)}`;document.getElementById('mon-disk-hint').textContent=`Свободно ${formatBytes(d.disk_free)}`;document.getElementById('mon-online').textContent=Math.max(0,Number(online.count)||0);document.getElementById('mon-bytes-in').textContent=d.net_down||'0 Б/с';document.getElementById('mon-bytes-out').textContent=d.net_up||'0 Б/с'}catch(e){}}function formatBytes(v){const n=Number(v)||0;if(n<1024)return Math.round(n)+' Б';const units=['КБ','МБ','ГБ','ТБ'];let i=-1,x=n;while(x>=1024&&i<units.length-1){x/=1024;i++}return x.toFixed(1)+' '+units[i]}t();setInterval(t,10000)</script>'''
    return page(request, "Мониторинг", body, "monitoring", script)

def safe_backup(name: str) -> Path:
    root = Path(config.BACKUP_DIR).resolve()
    path = (root / Path(name).name).resolve()
    if root not in path.parents or not path.is_file() or not path.name.endswith(".tar.gz"):
        raise HTTPException(404)
    return path


@app.get("/backups", response_class=HTMLResponse)
def backups(request: Request):
    require_auth(request)
    # Delivery reconciliation is intentionally performed by the backup worker,
    # not synchronously while rendering the page.
    folder = Path(config.BACKUP_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        state_path = Path(getattr(config, "BACKUP_STATE_PATH", "/var/lib/vpn-service/backup-state.json"))
        backup_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except Exception:
        backup_state = {}
    pending = backup_state.get("pending_deliveries", []) if isinstance(backup_state, dict) else []
    pending_count = len(pending) if isinstance(pending, list) else 0
    rows = "".join(
        f'<tr><td>{html.escape(file.name)}</td><td>{fmt_bytes(file.stat().st_size)}</td><td>{panel_from_timestamp(file.stat().st_mtime).strftime("%d.%m.%Y %H:%M")}</td><td><a class="button small secondary" href="/backups/{quote(file.name)}/download">Скачать</a> <form class="inline" method="post" action="/backups/{quote(file.name)}/restore" onsubmit="return confirm(\'Восстановить базы и конфигурацию из архива?\')"><button class="small">Восстановить</button></form> <form class="inline" method="post" action="/backups/{quote(file.name)}/delete"><button class="small danger">Удалить</button></form></td></tr>'
        for file in sorted(folder.glob("*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)[:100]
    )
    with database() as connection:
        runs = connection.execute("SELECT * FROM backup_runs ORDER BY id DESC LIMIT 10").fetchall()
    run_rows = "".join(
        f'<tr><td>{html.escape(row["created_at"] or "")}</td><td>{html.escape(row["filename"] or "—")}</td><td>{status_badge(bool(row["telegram_ok"]), "Отправлен", "Ошибка")}</td><td>{status_badge(bool(row["yandex_ok"]), "Загружен", "Не загружен")}</td><td>{html.escape(row["error"] or row["yandex_detail"] or row["telegram_detail"] or "")}</td></tr>'
        for row in runs
    )
    queue_notice = (f" Сейчас в очереди повторной доставки: <strong>{pending_count}</strong>. Неудачные загрузки автоматически повторяются." if pending_count else " Загрузка в облака подтверждается после создания архива; при временной ошибке доставка автоматически попадёт в очередь повторов.")
    body = f'''<header><div><h1>Резервные копии</h1><div class="subtitle">Полная папка бота, SQLite-снимки бота и 3x-ui, systemd и манифест</div></div><form method="post" action="/backups/create"><button>＋ Создать и отправить сейчас</button></form></header><div class="notice">Плановый таймер запускается ежедневно, но новый полный архив создаётся только раз в {int(getattr(config, 'BACKUP_INTERVAL_DAYS', 3))} дня. Файловая блокировка исключает одновременные дубли.{queue_notice}</div><div class="card table-wrap"><table><thead><tr><th>Архив</th><th>Размер</th><th>Дата</th><th>Действия</th></tr></thead><tbody>{rows or '<tr><td colspan="4">Бэкапов пока нет.</td></tr>'}</tbody></table></div><div class="card table-wrap" style="margin-top:15px"><div class="section-title"><h2>Последние доставки</h2></div><table><thead><tr><th>Дата</th><th>Файл</th><th>Telegram</th><th>Яндекс.Диск</th><th>Подробности</th></tr></thead><tbody>{run_rows or '<tr><td colspan="5">Запусков пока нет.</td></tr>'}</tbody></table></div>'''
    body += '<script>setTimeout(()=>window.location.reload(),15000);</script>'
    return page(request, "Бэкапы", body, "backups")


@app.post("/backups/create")
def create_backup(request: Request):
    require_auth(request)
    try:
        # Creation and cloud upload can take minutes; never occupy an interactive
        # FastAPI worker with a long-running subprocess.
        unit = f"fargovpn-manual-backup-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
        mode = launch_detached(
            unit,
            [str(PYTHON), str(APP_DIR / "backup.py"), "--force"],
            description=f"FargoVPN manual backup {unit}",
            working_directory=APP_DIR,
        )
        set_flash(request, f"Резервная копия запущена в фоне ({mode}). Результат появится в таблице доставок автоматически")
    except DetachedJobError as error:
        set_flash(request, f"Не удалось запустить бэкап в фоне: {error}", "bad")
    except Exception as error:
        set_flash(request, f"Ошибка запуска бэкапа: {error}", "bad")
    audit(str(request.session.get("user", "web")), "backup_create")
    return RedirectResponse(public_path("/backups"), 303)


@app.get("/backups/{name}/download")
def backup_download(request: Request, name: str):
    require_auth(request)
    return FileResponse(safe_backup(name), filename=Path(name).name)


@app.post("/backups/{name}/delete")
def backup_delete(request: Request, name: str):
    require_auth(request)
    safe_backup(name).unlink()
    audit(str(request.session.get("user", "web")), "backup_delete", name)
    set_flash(request, "Архив удалён")
    return RedirectResponse(public_path("/backups"), 303)


def safe_extract_backup(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            if member.issym() or member.islnk() or member.isdev() or not (member.isfile() or member.isdir()):
                raise RuntimeError("Архив содержит ссылки или специальные файлы")
            name = member.name.replace("\\", "/")
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise RuntimeError("Архив содержит небезопасный путь")
            target = (destination / name).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError("Архив выходит за каталог распаковки")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("Не удалось прочитать файл из архива")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, member.mode & 0o777)
    return destination / "vpn_service_backup"


def restore_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source, timeout=30)
    target_connection = sqlite3.connect(target, timeout=30)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()


@app.post("/backups/{name}/restore")
def backup_restore(request: Request, name: str):
    require_auth(request)
    archive = safe_backup(name)
    work = Path(tempfile.mkdtemp(prefix="vpn_restore_"))
    try:
        root = safe_extract_backup(archive, work)
        if not root.is_dir():
            raise RuntimeError("В архиве нет каталога vpn_service_backup")
        bot_root = root / "bot"
        bot_db_candidates = [
            root / "databases" / "vpn_bot.db",
            bot_root / "data" / "vpn_bot.db",
            root / "vpn_bot.db",
        ]
        xui_candidates = [root / "databases" / "x-ui.db", root / "x-ui.db"]
        config_candidates = [bot_root / "config.py", root / "config.py"]
        bot_source = next((path for path in bot_db_candidates if path.is_file()), None)
        xui_source = next((path for path in xui_candidates if path.is_file()), None)
        config_source = next((path for path in config_candidates if path.is_file()), None)
        if bot_source:
            restore_sqlite(bot_source, Path(config.DB_PATH))
        if xui_source and Path(config.XUI_DB_PATH).parent.exists():
            restore_sqlite(xui_source, Path(config.XUI_DB_PATH))
        if config_source:
            temporary = CONFIG_PATH.with_suffix(".restore.tmp")
            shutil.copy2(config_source, temporary)
            os.chmod(temporary, 0o600)
            temporary.replace(CONFIG_PATH)
        if not any((bot_source, xui_source, config_source)):
            raise RuntimeError("Подходящие базы и config.py в архиве не найдены")
        audit(str(request.session.get("user", "web")), "backup_restore", name)
        set_flash(request, "Базы и конфигурация восстановлены; службы перезапускаются")
        schedule_restart(["x-ui", "vpn-service-bot", "vpn-service-web"], delay=2)
    except Exception as error:
        set_flash(request, f"Ошибка восстановления: {error}", "bad")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return RedirectResponse(public_path("/backups"), 303)


def _service_unit(service: str) -> str:
    return {
        "bot": "vpn-service-bot",
        "web": "vpn-service-web",
        "backup": "vpn-service-backup",
        "reminders": "vpn-service-reminders",
        "update": "vpn-service-update*",
    }.get(service, "vpn-service-bot")


def _read_service_logs(service: str, lines: int) -> str:
    unit = _service_unit(service)
    safe_lines = max(50, min(int(lines), 2000))
    return shell(["journalctl", "-u", unit, "-n", str(safe_lines), "--no-pager"])



@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, actor: str = "", action: str = "", q: str = "", limit: int = 200):
    require_auth(request)
    safe_limit = max(25, min(int(limit), 1000))
    actor = str(actor or "").strip()[:128]
    action = str(action or "").strip()[:128]
    q = str(q or "").strip()[:300]
    clauses: list[str] = []
    params: list[Any] = []
    if actor:
        clauses.append("actor LIKE ?")
        params.append(f"%{actor}%")
    if action:
        clauses.append("action LIKE ?")
        params.append(f"%{action}%")
    if q:
        clauses.append("(details LIKE ? OR actor LIKE ? OR action LIKE ?)")
        pattern = f"%{q}%"
        params.extend([pattern, pattern, pattern])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with database() as connection:
        rows = connection.execute(
            f"SELECT id,created_at,actor,action,details FROM audit_log{where} ORDER BY id DESC LIMIT ?",
            (*params, safe_limit),
        ).fetchall()
        actions = [row[0] for row in connection.execute("SELECT DISTINCT action FROM audit_log WHERE action IS NOT NULL AND action<>'' ORDER BY action LIMIT 200").fetchall()]
    rows_html = []
    for row in rows:
        details = audit_details_label(row[4], row[3])
        rows_html.append(
            f'<tr><td>{html.escape(fmt_audit_timestamp(row[1]))}</td>'
            f'<td><span class="badge good">{html.escape(str(row[2] or "system"))}</span></td>'
            f'<td><span class="audit-action-label">{html.escape(audit_action_label(row[3]))}</span></td>'
            f'<td class="audit-details">{html.escape(details)}</td></tr>'
        )
    options = ''.join(f'<option value="{html.escape(str(item), quote=True)}" {"selected" if action == str(item) else ""}>{html.escape(audit_action_label(item))}</option>' for item in actions)
    body = f'''<header><div><h1>Журнал аудита</h1><div class="subtitle">Кто, когда и что изменил или отправил. Время показано по часовому поясу панели: {html.escape(panel_timezone_name())}. Новые записи сверху.</div></div><a class="button secondary" href="/audit">Сбросить фильтры</a></header>
<form class="audit-filters" method="get"><input name="actor" value="{html.escape(actor, quote=True)}" placeholder="Администратор / actor"><select name="action"><option value="">Все действия</option>{options}</select><input name="q" value="{html.escape(q, quote=True)}" placeholder="Поиск по деталям"><input name="limit" type="number" value="{safe_limit}" min="25" max="1000"><button>Фильтровать</button></form>
<div class="card table-wrap"><table><thead><tr><th>Дата</th><th>Кто</th><th>Действие</th><th>Детали</th></tr></thead><tbody>{''.join(rows_html) or '<tr><td colspan="4">Записей не найдено.</td></tr>'}</tbody></table></div>'''
    return page(request, "Аудит", body, "audit")


@app.get("/api/audit", response_class=JSONResponse)
def audit_api(request: Request, actor: str = "", action: str = "", q: str = "", limit: int = 200):
    require_auth(request)
    safe_limit = max(25, min(int(limit), 1000))
    actor = str(actor or "").strip()[:128]
    action = str(action or "").strip()[:128]
    q = str(q or "").strip()[:300]
    clauses: list[str] = []
    params: list[Any] = []
    if actor:
        clauses.append("actor LIKE ?")
        params.append(f"%{actor}%")
    if action:
        clauses.append("action LIKE ?")
        params.append(f"%{action}%")
    if q:
        clauses.append("(details LIKE ? OR actor LIKE ? OR action LIKE ?)")
        pattern = f"%{q}%"
        params.extend([pattern, pattern, pattern])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with database() as connection:
        rows = connection.execute(
            f"SELECT id,created_at,actor,action,details FROM audit_log{where} ORDER BY id DESC LIMIT ?",
            (*params, safe_limit),
        ).fetchall()
    return {"items": [{**dict(row), "created_at": fmt_audit_timestamp(row[1]), "action_label": audit_action_label(row[3]), "details_label": audit_details_label(row[4], row[3])} for row in rows]}


@app.get("/reminders", response_class=HTMLResponse)
def reminders_page(request: Request):
    require_auth(request)
    configured = sorted({int(x) for x in getattr(config, "REMINDER_DAYS", [7, 3, 1, 0]) if int(x) >= 0}, reverse=True)
    with database() as connection:
        rows = connection.execute(
            "SELECT action, details, created_at FROM audit_log WHERE action='reminder_sent' ORDER BY id DESC LIMIT 300"
        ).fetchall()
    total = len(rows)
    by_bucket: dict[int, int] = {}
    recent_html = []
    for row in rows:
        days = None; username = ""; tg_id = 0
        try:
            payload = json.loads(str(row[1] or "{}"))
            days = int(payload.get("days")) if payload.get("days") is not None else None
            username = str(payload.get("username") or "")
            tg_id = int(payload.get("tg_id") or 0)
        except Exception:
            pass
        if days is not None: by_bucket[days] = by_bucket.get(days, 0) + 1
        who = f"@{username}" if username else (f"TG ID {tg_id}" if tg_id else "Пользователь")
        recent_html.append(f'<tr><td>{html.escape(str(row[2] or ""))}</td><td>{html.escape(who)}</td><td>{html.escape("сегодня" if days == 0 else (f"за {days} дн." if days is not None else "—"))}</td></tr>')
    bucket_html = ''.join(f'<span class="badge good">{("сегодня" if day == 0 else f"за {day} дн.")}: {by_bucket.get(day, 0)}</span>' for day in configured)
    body = f'''<header><div><h1>Напоминания</h1><div class="subtitle">Автоматические уведомления о скором окончании подписки.</div></div><span class="badge good">Настроено: {", ".join(map(str, configured)) or "нет"}</span></header>
<div class="grid"><div class="card half"><h2>Текущая схема</h2><p class="muted">Напоминания отправляются один раз на каждый календарный день из списка. В уведомлении есть цена текущего тарифа и кнопки «Продлить подписку» и «Моя статистика».</p><div class="actions">{bucket_html or '<span class="badge warn">Дни не настроены</span>'}</div></div><div class="card half"><h2>Отправлено за последние записи</h2><div class="metric-inline"><strong>{total}</strong><span>зафиксированных напоминаний</span></div><div class="muted">Для каждого отправленного напоминания сохраняется запись в журнале аудита.</div></div><div class="card full table-wrap"><h2>Последние отправки</h2><table><thead><tr><th>Дата</th><th>Пользователь</th><th>Этап</th></tr></thead><tbody>{''.join(recent_html) or '<tr><td colspan="3">Отправок пока нет.</td></tr>'}</tbody></table></div></div>'''
    return page(request, "Напоминания", body, "reminders")


@app.get("/api/logs", response_class=JSONResponse)
def logs_api(request: Request, service: str = "bot", lines: int = 300):
    require_auth(request)
    service = service if service in {"bot", "web", "backup", "reminders", "update"} else "bot"
    safe_lines = max(50, min(int(lines), 2000))
    return {"service": service, "lines": safe_lines, "text": _read_service_logs(service, safe_lines)}


@app.get("/logs", response_class=HTMLResponse)
def logs(request: Request, service: str = "bot", lines: int = 300):
    require_auth(request)
    service = service if service in {"bot", "web", "backup", "reminders", "update"} else "bot"
    lines = max(50, min(lines, 2000))
    data = _read_service_logs(service, lines)
    def selected(key: str) -> str:
        return "selected" if service == key else ""
    body = f"""<header><div><h1>Журналы</h1><div class="subtitle">Последние сообщения systemd · переключение журнала происходит сразу</div></div></header>
<div class="toolbar"><div class="logs-toolbar"><label class="logs-filter"><span>Журнал</span><select id="logs-service" name="service">
<option value="bot" {selected('bot')}>Telegram-бот</option><option value="web" {selected('web')}>Веб-панель</option><option value="backup" {selected('backup')}>Бэкапы</option><option value="reminders" {selected('reminders')}>Напоминания</option><option value="update" {selected('update')}>Обновления</option>
</select></label><label class="logs-filter"><span>Строк</span><input id="logs-lines" type="number" name="lines" value="{lines}" min="50" max="2000"></label><span id="logs-loading" class="muted" aria-live="polite"></span></div></div>
<pre id="logs-output" class="auto-scroll-bottom">{html.escape(data)}</pre>"""
    script = """<script>(function(){
const select=document.getElementById('logs-service');
const lines=document.getElementById('logs-lines');
const output=document.getElementById('logs-output');
const loading=document.getElementById('logs-loading');
let requestNo=0;
function bottom(){if(output)output.scrollTop=output.scrollHeight;}
async function loadLogs(){
  if(!select||!output)return;
  const mine=++requestNo;
  let value=Number(lines&&lines.value||300);if(!Number.isFinite(value))value=300;value=Math.max(50,Math.min(2000,Math.trunc(value)));
  if(lines)lines.value=String(value);
  if(loading)loading.textContent='Загрузка…';
  try{
    const r=await fetch('/api/logs?service='+encodeURIComponent(select.value)+'&lines='+encodeURIComponent(value),{cache:'no-store',credentials:'same-origin',headers:{Accept:'application/json'}});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const data=await r.json();
    if(mine!==requestNo)return;
    output.textContent=String(data.text||'');
    if(data.service&&select.value!==data.service)select.value=data.service;
    requestAnimationFrame(bottom);setTimeout(bottom,50);
    if(history.replaceState)history.replaceState(null,'','/logs?service='+encodeURIComponent(data.service||select.value)+'&lines='+encodeURIComponent(value));
  }catch(error){if(mine===requestNo)output.textContent='Не удалось загрузить журнал: '+(error.message||error);}
  finally{if(mine===requestNo&&loading)loading.textContent='';}
}
if(select)select.addEventListener('change',loadLogs);
if(lines){lines.addEventListener('change',loadLogs);lines.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();loadLogs();}});}
requestAnimationFrame(bottom);setTimeout(bottom,80);
})();</script>"""
    return page(request, "Журналы", body, "logs", script)


def replace_assignment(text: str, name: str, value: Any) -> str:
    line = f"{name} = {value!r}"
    pattern = rf"(?m)^{re.escape(name)}\s*=.*$"
    return re.sub(pattern, line, text) if re.search(pattern, text) else text.rstrip() + "\n" + line + "\n"


def save_config_values(values: dict[str, Any]) -> None:
    """Atomically update config.py; TLS certificate settings are Nginx-owned."""
    values = dict(values)
    values["WEB_REVERSE_PROXY"] = True
    values["WEB_HOST"] = "127.0.0.1"
    values["WEB_SOCKET_PATH"] = "/run/vpn-service/fargovpn.sock"
    values["WEB_COOKIE_HTTPS_ONLY"] = True
    values["WEB_TRUST_PROXY_HEADERS"] = True
    text = CONFIG_PATH.read_text(encoding="utf-8")
    backup = CONFIG_PATH.with_name(f"config.py.before_web_{int(time.time())}")
    shutil.copy2(CONFIG_PATH, backup)
    # Remove obsolete app-side TLS settings when an old installation is edited.
    for key, value in values.items():
        text = replace_assignment(text, key, value)
    temporary = CONFIG_PATH.with_suffix(".py.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(CONFIG_PATH)
    for key, value in values.items():
        setattr(config, key, value)
    invalidate_snapshot_cache()
    update_manager.invalidate_update_cache()


def schedule_restart(units: list[str], delay: int = 2) -> None:
    safe_units = [unit for unit in units if re.fullmatch(r"[A-Za-z0-9_.@*-]+", unit)]
    if not safe_units:
        return
    command = f"sleep {max(1, delay)}; systemctl restart {' '.join(safe_units)}"
    subprocess.Popen(
        ["/bin/bash", "-c", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _config_text(name: str, default: Any = "") -> str:
    return html.escape(str(getattr(config, name, default) or ""), quote=True)


def _config_checked(name: str, default: bool = False) -> str:
    return "checked" if bool(getattr(config, name, default)) else ""


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 260_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest}"


def _http_url(
    value: str,
    label: str,
    *,
    allow_empty: bool = False,
    trailing_slash: bool = False,
) -> str:
    clean = str(value or "").strip()
    if not clean and allow_empty:
        return ""
    parts = urlsplit(clean)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise HTTPException(400, f"{label}: нужен полный URL с http:// или https://")
    if parts.username or parts.password:
        raise HTTPException(400, f"{label}: логин и пароль нельзя хранить внутри URL")
    if len(clean) > 1000:
        raise HTTPException(400, f"{label}: URL слишком длинный")
    return clean.rstrip("/") + "/" if trailing_slash else clean.rstrip("/")


def _form_int(form: Any, name: str, default: int, minimum: int, maximum: int, label: str) -> int:
    try:
        value = int(str(form.get(name, default)).strip())
    except (TypeError, ValueError) as error:
        raise HTTPException(400, f"{label}: требуется целое число") from error
    if not minimum <= value <= maximum:
        raise HTTPException(400, f"{label}: допустимо от {minimum} до {maximum}")
    return value


def _form_float(form: Any, name: str, default: float, minimum: float, maximum: float, label: str) -> float:
    try:
        value = float(str(form.get(name, default)).strip().replace(",", "."))
    except (TypeError, ValueError) as error:
        raise HTTPException(400, f"{label}: требуется число") from error
    if not minimum <= value <= maximum:
        raise HTTPException(400, f"{label}: значение вне допустимого диапазона")
    return value


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    require_auth(request)
    reminder_value = ",".join(map(str, getattr(config, "REMINDER_DAYS", [7, 3, 1, 0])))
    admin_value = ",".join(map(str, getattr(config, "ADMIN_IDS", [])))
    publisher = update_publisher_for_request(request)
    publisher_username = str(getattr(config, "UPDATE_PUBLISHER_USERNAME", "") or "").strip()
    role_label = "GitHub Publisher" if publisher else "GitHub Client"
    role_class = "good" if publisher else "warn"
    current_username = str(getattr(config, "WEB_USERNAME", ""))
    subscription_days = int(getattr(config, "SUBSCRIPTION_DAYS", 30))
    updates_settings_block = "" if not publisher else f'''
  <section class="setting-section" id="updates" data-tab-section="updates">
  <div class="grid">
    <div class="card half">
      <h2>GitHub Releases</h2>
      <div class="notice"><b>Текущая роль:</b> {html.escape(role_label)}.<br>Все панели получают обновления из последнего GitHub Release. Публикация доступна назначенной главной панели (<span class="code">{html.escape(publisher_username or 'не назначена')}</span>).</div>
      <div class="setting"><label>GitHub Personal Access Token</label><input type="password" name="github_api_token" placeholder="{'Токен уже сохранён; пусто — не менять' if getattr(config, 'GITHUB_API_TOKEN', '') else 'Введите токен с Contents: write'}" autocomplete="new-password"><div class="muted">Токен не показывается обратно. Для публикации нужен доступ к Contents → Read and write.</div></div>
      <div class="setting"><label>Владелец репозитория</label><input name="github_repository_owner" value="{html.escape(str(getattr(config, 'GITHUB_REPOSITORY_OWNER', '')))}" required></div>
      <div class="setting"><label>Репозиторий</label><input name="github_repository_name" value="{html.escape(str(getattr(config, 'GITHUB_REPOSITORY_NAME', 'FargoVPN')))}" required></div>
      <div class="setting"><label>Ветка для tag target</label><input name="github_target_branch" value="{html.escape(str(getattr(config, 'GITHUB_TARGET_BRANCH', 'main')))}" required></div>
    </div>
    <div class="card half">
      <h2>Настройки публикации</h2>
      <div class="setting"><label>Префикс тега</label><input name="github_release_tag_prefix" value="{html.escape(str(getattr(config, 'GITHUB_RELEASE_TAG_PREFIX', 'FargoVPN-')))}" required></div>
      <div class="setting"><label>Имя релиза</label><input name="github_release_name_template" value="{html.escape(str(getattr(config, 'GITHUB_RELEASE_NAME_TEMPLATE', 'FargoVPN {version}')))}" required></div>
      <div class="setting"><label>Имя asset-архива</label><input name="github_release_asset_name" value="{html.escape(str(getattr(config, 'GITHUB_RELEASE_ASSET_NAME', 'VPN_Service_Platform_{version}_FULL.tar.gz')))}" required><div class="muted">Поддерживается переменная <span class="code">{{version}}</span>.</div></div>
      <label class="check-row"><input type="checkbox" name="github_release_make_latest" value="1" {_config_checked('GITHUB_RELEASE_MAKE_LATEST', True)}> Делать релиз Latest</label>
      <label class="check-row"><input type="checkbox" name="github_release_draft" value="1" {_config_checked('GITHUB_RELEASE_DRAFT', False)}> Создавать Draft</label>
      <label class="check-row"><input type="checkbox" name="github_release_prerelease" value="1" {_config_checked('GITHUB_RELEASE_PRERELEASE', False)}> Отмечать как Pre-release</label>
      <div class="setting"><label>Интервал проверки, секунд</label><input type="number" name="update_check_interval" value="{int(getattr(config, 'UPDATE_CHECK_INTERVAL', 60))}" min="15" max="86400"></div>
      <div class="setting"><label>Максимальный архив, МБ</label><input type="number" name="update_max_archive_mb" value="{int(getattr(config, 'UPDATE_MAX_ARCHIVE_MB', 1024))}" min="64" max="4096"></div>
      <div class="setting"><label>Считать задачу зависшей через, сек.</label><input type="number" name="update_stale_job_seconds" value="{int(getattr(config, 'UPDATE_STALE_JOB_SECONDS', 7200))}" min="900" max="86400"></div>
      <a class="button secondary" href="{html.escape(public_path("/updates"), quote=True)}">Открыть центр обновлений</a>
    </div>
  </div>
  </section>
    ''' if publisher else ""

    panel_push_script = r"""<script>(function(){
const ps=document.getElementById('panel-push-status'),ph=document.getElementById('panel-push-help'),pl=document.getElementById('panel-push-log');
const meta=(n)=>document.querySelector('meta[name="'+n+'"]');
const scopeRaw=String((meta('fargovpn-sw-scope')||{}).content||'').trim();
const scopePath=scopeRaw?(scopeRaw.endsWith('/')?scopeRaw:scopeRaw+'/'):'/';
const prefix=scopePath!=='/'?scopePath.slice(0,-1):'';
const pushUrl=(p)=>{const raw=String(p||'/').trim();const path=raw.startsWith('/')?raw:'/'+raw;if(!prefix)return path;if(path===prefix||path.startsWith(prefix+'/'))return path;return prefix+path;};
const setPush=(t,c,h)=>{if(ps){ps.textContent=t;ps.className='badge '+c;}if(ph)ph.textContent=h||'';};
const clientLogs=[];
let logsPromise=null;let actionBusy=false;const delayedTimers=new Set();
const renderVisibleLogs=(serverText='')=>{if(!pl)return;const parts=[];if(serverText)parts.push('=== СЕРВЕРНЫЙ ЖУРНАЛ PUSH ===\n'+serverText);if(clientLogs.length)parts.push('=== БРАУЗЕРНЫЙ ЖУРНАЛ PUSH ===\n'+clientLogs.join('\n'));pl.textContent=parts.join('\n\n')||'Журнал пока пуст.';pl.scrollTop=pl.scrollHeight;};
const addClientLog=(line)=>{const now=new Date().toLocaleTimeString();clientLogs.push(now+'  '+line);while(clientLogs.length>200)clientLogs.shift();renderVisibleLogs();};
const call=async(p,o={},timeout=20000)=>{const url=pushUrl(p);addClientLog((o.method||'GET')+' '+url);const controller=new AbortController();let timedOut=false;const timer=setTimeout(()=>{timedOut=true;controller.abort();},timeout);try{const r=await fetch(url,{...o,credentials:'same-origin',cache:'no-store',signal:controller.signal,headers:{Accept:'application/json',...(o.headers||{})}});let d={};try{d=await r.json();}catch(_){}if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));return d;}catch(e){if(timedOut){const err=new Error('Таймаут запроса ('+timeout+' мс)');err.name='TimeoutError';addClientLog('ОШИБКА TimeoutError: '+err.message);throw err;}addClientLog('ОШИБКА '+(e.name||'Error')+': '+(e.message||e));throw e;}finally{clearTimeout(timer);}};
const renderLogs=async()=>{if(logsPromise)return logsPromise;logsPromise=(async()=>{try{const d=await call('/api/panel/push/logs?limit=200',{},15000);const text=(d.logs||[]).map(x=>[x.created_at,x.level,x.event,x.message].join('  ')).join('\n');renderVisibleLogs(text);}catch(e){renderVisibleLogs('Не удалось загрузить серверный журнал: '+(e.name||'Error')+': '+(e.message||e));}})();try{return await logsPromise;}finally{logsPromise=null;}};
const scheduleLogRefreshes=()=>{[1000,3000,7000,15000].forEach(delay=>{const timer=setTimeout(()=>{delayedTimers.delete(timer);renderLogs().catch(()=>{});},delay);delayedTimers.add(timer);});};
const isIOS=()=>/iPhone|iPad|iPod/i.test(navigator.userAgent)||((navigator.platform==='MacIntel')&&navigator.maxTouchPoints>1);
const isStandalone=()=>Boolean(navigator.standalone===true||(window.matchMedia&&window.matchMedia('(display-mode: standalone)').matches));
const pushCapability=()=>({ios:isIOS(),standalone:isStandalone(),supported:Boolean('serviceWorker'in navigator&&'PushManager'in window&&'Notification'in window)});
const check=async()=>{if(actionBusy)return;addClientLog('Проверка состояния Push запущена');setPush('Проверка…','warn','Проверяю сервер, VAPID, подписки и browser capability.');try{const cap=pushCapability();addClientLog('platform iOS='+cap.ios+' standalone='+cap.standalone+' supported='+cap.supported);if(!cap.supported){setPush('Недоступно','bad','Браузер не предоставляет Service Worker / PushManager / Notification.');return;}if(cap.ios&&!cap.standalone){setPush('Требуется PWA','warn','На iPhone/iPad Push для панели доступен после добавления сайта на экран «Домой» и открытия его как веб-приложения.');return;}const d=await call('/api/panel/push/status');let extras='Подписок: '+(d.count||0)+'. VAPID: '+(d.vapid_configured?'готов':'ошибка')+'. ';if(d.private_key_exists===false)extras+='Приватный VAPID-ключ отсутствует. ';if(d.db_ok===false)extras+='База Push недоступна. ';if(d.last_error)extras+='Последняя ошибка: '+d.last_error;setPush(d.ok?'Готово':'Ошибка',d.ok?'good':'bad',extras||'Состояние получено.');}catch(e){setPush('Ошибка','bad','Не удалось проверить Push: '+(e.name==='TimeoutError'?'таймаут':(e.message||e)));}finally{await renderLogs();}};
const enable=async()=>{if(actionBusy)return;actionBusy=true;const b=document.getElementById('panel-push-enable');if(b)b.disabled=true;addClientLog('Включение Push запущено');setPush('Включение…','warn','Проверяю HTTPS, Service Worker, разрешение и VAPID.');try{const cap=pushCapability();addClientLog('secureContext='+String(window.isSecureContext)+' origin='+location.origin);if(!window.isSecureContext)throw new Error('Страница не является secure context. Панель должна открываться через HTTPS 443.');if(!cap.supported)throw new Error('Браузер не поддерживает Service Worker / PushManager / Notification');if(cap.ios&&!cap.standalone){setPush('Требуется PWA','warn','На iPhone/iPad сначала добавьте панель на экран «Домой» и откройте её как веб-приложение.');return;}const permission=Notification.permission==='granted'?'granted':await Notification.requestPermission();addClientLog('Notification.permission='+permission);if(permission!=='granted'){setPush(permission==='denied'?'Запрещены':'Ожидается разрешение','warn',permission==='denied'?'Разрешите уведомления в системных настройках браузера/PWA.':'Разрешите уведомления в системном запросе.');return;}const cfg=await call('/api/panel/push/config');if(!cfg.public_key)throw new Error('Сервер не вернул VAPID public key');let raw=String(cfg.public_key).replace(/-/g,'+').replace(/_/g,'/');const bin=atob(raw+'='.repeat((4-raw.length%4)%4));if(bin.length!==65)throw new Error('Некорректный VAPID public key: '+bin.length+' байт');addClientLog('VAPID public key OK, 65 bytes');const swMeta=meta('fargovpn-sw-url');let swUrl=(swMeta&&swMeta.content)||pushUrl('/service-worker.js');swUrl=new URL(swUrl,location.href).href;const swProbe=await fetch(swUrl,{credentials:'same-origin',cache:'no-store'});const swType=String(swProbe.headers.get('content-type')||'');const swAllowed=String(swProbe.headers.get('service-worker-allowed')||'');addClientLog('SW probe HTTP '+swProbe.status+' content-type='+swType+' responseURL='+swProbe.url);addClientLog('Service-Worker-Allowed='+(swAllowed||'<missing>'));if(!swProbe.ok)throw new Error('Service Worker вернулся с HTTP '+swProbe.status);if(!swType.toLowerCase().includes('javascript'))throw new Error('Service Worker имеет неверный Content-Type: '+(swType||'отсутствует'));const expectedScope=new URL(scopePath,location.origin).href;const expectedScopeDir=expectedScope.endsWith('/')?expectedScope:expectedScope+'/';addClientLog('register SW '+swUrl+' expected scope '+expectedScope);if(!swUrl.startsWith(location.origin+'/'))throw new Error('Service Worker находится вне текущего origin');if(swAllowed){const allowedUrl=new URL(swAllowed,swUrl);const allowedPath=allowedUrl.pathname.endsWith('/')?allowedUrl.pathname:allowedUrl.pathname+'/';const expectedPath=new URL(expectedScope,location.origin).pathname;if(allowedUrl.origin!==location.origin||!expectedPath.startsWith(allowedPath))throw new Error('Service-Worker-Allowed не покрывает требуемый scope');}const reg=await navigator.serviceWorker.register(swUrl,{updateViaCache:'none'});addClientLog('Browser-selected SW scope: '+reg.scope);const actualScope=reg.scope.endsWith('/')?reg.scope:reg.scope+'/';if(actualScope!==expectedScopeDir)throw new Error('Браузер назначил неожиданный scope Service Worker: '+reg.scope);await reg.update().catch(e=>addClientLog('SW update: '+e));const ready=await navigator.serviceWorker.ready;addClientLog('Service Worker ready: '+ready.scope);let sub=await ready.pushManager.getSubscription();if(!sub){addClientLog('Создаю PushSubscription…');sub=await ready.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:Uint8Array.from(bin,c=>c.charCodeAt(0))});addClientLog('PushSubscription создан');}else addClientLog('Существующая PushSubscription найдена');await call('/api/panel/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subscription:sub.toJSON(),user_agent:navigator.userAgent.slice(0,500)})});setPush('Включены','good','Push-подписка административной панели зарегистрирована.');}catch(e){setPush('Ошибка','bad','Не удалось включить Push: '+(e.name==='TimeoutError'?'таймаут':(e.message||e)));}finally{actionBusy=false;if(b)b.disabled=false;await renderLogs();}};
const disable=async()=>{if(actionBusy)return;actionBusy=true;addClientLog('Отключение Push запущено');setPush('Отключение…','warn','Удаляю Push-подписку.');try{const reg=await navigator.serviceWorker.ready,sub=await reg.pushManager.getSubscription();if(sub){await call('/api/panel/push/unsubscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:sub.endpoint})});await sub.unsubscribe();}setPush('Выключены','warn','Push-подписка отключена.');}catch(e){setPush('Ошибка','bad','Не удалось выключить Push: '+(e.name==='TimeoutError'?'таймаут':(e.message||e)));}finally{actionBusy=false;await renderLogs();}};
const test=async()=>{if(actionBusy)return;actionBusy=true;const b=document.getElementById('panel-push-test');if(b)b.disabled=true;addClientLog('Тестовая Push-доставка запущена');setPush('Отправка…','warn','Запрашиваю фоновую отправку тестового уведомления. Результат доставки появится в журнале.');try{const d=await call('/api/panel/push/test',{method:'POST'},10000);if(!d.queued)throw new Error('Сервер не подтвердил постановку Push-теста в очередь');setPush('Отправка запущена','good','Тест поставлен в фоновую отправку. Подписок: '+(d.subscriptions||0)+'. Проверьте уведомление и журнал доставки.');scheduleLogRefreshes();}catch(e){setPush('Ошибка','bad','Тест не отправлен: '+(e.name==='TimeoutError'?'таймаут':(e.message||e)));}finally{actionBusy=false;if(b)b.disabled=false;await renderLogs();}};
document.getElementById('panel-push-check')?.addEventListener('click',check);document.getElementById('panel-push-enable')?.addEventListener('click',enable);document.getElementById('panel-push-disable')?.addEventListener('click',disable);document.getElementById('panel-push-test')?.addEventListener('click',test);document.getElementById('panel-push-log-refresh')?.addEventListener('click',renderLogs);
const renderAppLog=async()=>{const out=document.getElementById('app-log-output'),sel=document.getElementById('app-log-limit');if(!out)return;try{const lim=Number(sel?.value||500);const r=await fetch(pushUrl('/api/panel/app-log?limit='+lim),{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));out.textContent=(d.lines||[]).join('')||'Лог пока пуст или файл ещё не создан.';}catch(e){out.textContent='Не удалось загрузить лог: '+(e.message||e);}};
document.getElementById('app-log-refresh')?.addEventListener('click',renderAppLog);document.getElementById('app-log-limit')?.addEventListener('change',renderAppLog);const boot=()=>{if(window.__fargovpnPushBooted)return;window.__fargovpnPushBooted=true;renderLogs();check();renderAppLog();};if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();window.addEventListener('pagehide',()=>{for(const timer of delayedTimers)clearTimeout(timer);delayedTimers.clear();},{once:true});
})();</script>
"""


    body = f'''
<header>
  <div><h1>Настройки</h1><div class="subtitle">Разделены по назначению. Обязательны только данные, без которых конкретная функция не может работать.</div></div>
  <span class="badge {role_class}">{html.escape(role_label)}</span>
</header>
<form id="settings-form" method="post" action="{html.escape(public_path("/settings"), quote=True)}" autocomplete="off" novalidate>
  <nav class="tabs" data-tabs aria-label="Разделы настроек">
    <a role="tab" data-tab="bot" href="{html.escape(public_path('/settings'), quote=True)}#bot">Бот и сервис</a>
    <a role="tab" data-tab="payment" href="{html.escape(public_path('/settings'), quote=True)}#payment">Оплата и чеки</a>
    <a role="tab" data-tab="xui" href="{html.escape(public_path('/settings'), quote=True)}#xui">3x-ui</a>
    <a role="tab" data-tab="security" href="{html.escape(public_path('/settings'), quote=True)}#security">Панель и защита</a>
    {('<a role="tab" data-tab="updates" href="' + html.escape(public_path('/settings'), quote=True) + '#updates">Обновления</a>') if publisher else ''}
    <a role="tab" data-tab="notifications" href="{html.escape(public_path('/settings'), quote=True)}#notifications">Уведомления</a>
    <a role="tab" data-tab="data" href="{html.escape(public_path('/settings'), quote=True)}#data">Бэкапы и данные</a>
  </nav>

  <section class="setting-section active" id="bot" data-tab-section="bot">
    <div class="grid">
      <div class="card half">
        <h2>Основные параметры</h2>
        <div class="setting"><label>Название сервиса</label><input name="service_name" value="{_config_text('SERVICE_NAME')}" maxlength="100" required></div>
        <div class="setting"><label>Telegram Bot Token</label><input type="password" name="bot_token" placeholder="Пусто — оставить текущий" autocomplete="new-password"><div class="muted">Текущий токен никогда не выводится в браузер.</div></div>
        <div class="setting"><label>Telegram ID администраторов</label><input name="admin_ids" value="{html.escape(admin_value)}" placeholder="123456789, 987654321"><div class="muted">Положительные ID через запятую, пробел или точку с запятой.</div></div>
        <div class="setting"><label>Срок одной покупки, дней</label><input type="number" name="subscription_days" value="{subscription_days}" min="1" max="3650" required></div>
      </div>
      <div class="card half">
        <h2>Тексты и интерактивность</h2>
        <div class="setting"><label>Приветствие / текст главного меню</label><textarea name="bot_welcome_text" maxlength="3500" placeholder="Пусто — стандартный текст. Можно использовать {{service}}.">{_config_text('BOT_WELCOME_TEXT')}</textarea></div>
        <div class="setting"><label>Подсказка перед обращением в поддержку</label><textarea name="bot_support_prompt" maxlength="1500" placeholder="Пусто — стандартная подсказка.">{_config_text('BOT_SUPPORT_PROMPT')}</textarea></div>
        <div class="setting"><label>Ссылка на Incy</label><input type="text" inputmode="url" name="faq_incy_url" value="{_config_text('FAQ_INCY_URL', 'https://apps.apple.com/ru/app/incy/id6756943388')}" required></div>
        <div class="setting"><label>Часовой пояс панели</label><input name="web_timezone" value="{_config_text('WEB_TIMEZONE', 'Asia/Almaty')}" maxlength="100" required><div class="muted">Используется для дат/времени в веб-панели, журнале, подписках и статистике. Например: Asia/Almaty.</div></div>
        <div class="setting"><label>Домен веб-панели</label><input name="web_domain" value="{_config_text('WEB_DOMAIN')}" placeholder="panel.example.com или example.com"><div class="muted">Имя должно входить в SAN сертификата. При пустом поле панель пытается определить hostname из URL/конфигурации текущего reverse-proxy.</div></div>
        <div class="two">
          <div class="setting"><label>Обновлять Telegram-привязку, сек.</label><input type="number" name="bot_identity_refresh_seconds" value="{int(getattr(config, 'BOT_IDENTITY_REFRESH_SECONDS', 600))}" min="60" max="86400"></div>
          <div class="setting"><label>Синхронизация с 3x-ui, сек.</label><input type="number" name="bot_sync_interval_seconds" value="{int(getattr(config, 'BOT_SYNC_INTERVAL_SECONDS', 3600))}" min="300" max="86400"></div>
        </div>
      </div>
      <div class="card full">
        <div class="section-title"><h2>Массовая рассылка из веб-панели</h2><a class="button secondary small" href="/broadcast">Открыть рассылку</a></div>
        <div class="two">
          <div class="setting"><label>Максимальный файл, МБ</label><input type="number" name="broadcast_media_max_mb" value="{int(getattr(config, 'BROADCAST_MEDIA_MAX_MB', 45))}" min="1" max="49"><div class="muted">Фото, видео или документ загружается в Telegram один раз, затем используется полученный file_id.</div></div>
          <div class="setting"><label>Пауза между получателями, сек.</label><input type="number" step="0.01" name="broadcast_send_delay_seconds" value="{float(getattr(config, 'BROADCAST_SEND_DELAY_SECONDS', 0.04)):g}" min="0.02" max="5"><div class="muted">Небольшая пауза снижает риск ограничений Telegram при большой базе.</div></div>
          <div class="setting"><label>Считать рассылку зависшей через, сек.</label><input type="number" name="broadcast_stale_seconds" value="{int(getattr(config, 'BROADCAST_STALE_SECONDS', 7200))}" min="300" max="86400"></div>
        </div>
      </div>
    </div>
  </section>

  <section class="setting-section" id="payment" data-tab-section="payment">
    <div class="grid">
      <div class="card half">
        <h2>Реквизиты оплаты</h2>
        <div class="setting"><label>Цена за {subscription_days} дней, ₽</label><input name="price" type="number" value="{int(getattr(config, 'PAYMENT_PRICE', 150))}" min="0" max="10000000"></div>
        <div class="setting"><label>Телефон / реквизиты</label><input name="phone" value="{_config_text('PAYMENT_PHONE')}" maxlength="500"></div>
        <div class="setting"><label>Банк</label><input name="bank" value="{_config_text('PAYMENT_BANK')}" maxlength="200"></div>
        <div class="setting"><label>Получатель</label><input name="receiver" value="{_config_text('PAYMENT_RECEIVER')}" maxlength="200"></div>
        <div class="notice">Бот выдаёт ровно выбранный срок подписки. Переплата не увеличивает срок автоматически.</div>
      </div>
      <div class="card half">
        <h2>OCR и автопроверка</h2>
        <div class="check-row"><label><input type="checkbox" name="receipt_ocr_enabled" value="1" {_config_checked('RECEIPT_OCR_ENABLED', True)}> Локально распознавать чеки</label></div>
        <div class="check-row"><label><input type="checkbox" name="receipt_auto_approve" value="1" {_config_checked('RECEIPT_AUTO_APPROVE', True)}> Автоматически подтверждать при успешных фильтрах</label></div>
        <div class="check-row"><label><input type="checkbox" name="receipt_allow_masked_phone" value="1" {_config_checked('RECEIPT_ALLOW_MASKED_PHONE', True)}> Разрешать частично скрытый телефон</label></div>
        <div class="two">
          <div class="setting"><label>Минимальная сумма, ₽</label><input name="receipt_min_amount" type="number" step="0.01" value="{float(getattr(config, 'RECEIPT_MIN_AMOUNT', 150)):g}" min="0" max="10000000"></div>
          <div class="setting"><label>Максимальный возраст, часов</label><input name="receipt_max_age_hours" type="number" value="{int(getattr(config, 'RECEIPT_MAX_AGE_HOURS', 24))}" min="1" max="168"></div>
        </div>
        <div class="setting"><label>Дополнительные варианты имени</label><textarea name="receipt_aliases" maxlength="2000" placeholder="Иван И.; И. Иванов">{_config_text('RECEIPT_RECEIVER_ALIASES')}</textarea></div>
      </div>
      <div class="card full">
        <h2>Фильтры и движок OCR</h2>
        <div class="two">
          <div>
            <div class="check-row"><label><input type="checkbox" name="receipt_filter_name" value="1" {_config_checked('RECEIPT_FILTER_NAME', True)}> Проверять имя получателя</label></div>
            <div class="check-row"><label><input type="checkbox" name="receipt_filter_phone" value="1" {_config_checked('RECEIPT_FILTER_PHONE', False)}> Проверять телефон</label></div>
            <div class="check-row"><label><input type="checkbox" name="receipt_filter_amount" value="1" {_config_checked('RECEIPT_FILTER_AMOUNT', True)}> Проверять сумму</label></div>
            <div class="check-row"><label><input type="checkbox" name="receipt_filter_date" value="1" {_config_checked('RECEIPT_FILTER_DATE', False)}> Проверять дату</label></div>
            <div class="check-row"><label><input type="checkbox" name="receipt_filter_status" value="1" {_config_checked('RECEIPT_FILTER_STATUS', True)}> Блокировать отменённые / ошибочные чеки</label></div>
            <div class="check-row"><label><input type="checkbox" name="receipt_filter_duplicate" value="1" {_config_checked('RECEIPT_FILTER_DUPLICATE', True)}> Блокировать дубликаты</label></div>
          </div>
          <div>
            <div class="setting"><label>Часовой пояс чеков</label><input name="receipt_timezone" value="{_config_text('RECEIPT_TIMEZONE', 'Asia/Almaty')}" maxlength="100" required></div>
            <div class="setting"><label>Языки Tesseract</label><input name="receipt_ocr_languages" value="{_config_text('RECEIPT_OCR_LANGUAGES', 'rus+eng')}" maxlength="80" required></div>
            <div class="setting"><label>Тайм-аут одного прохода, сек.</label><input name="receipt_ocr_timeout" type="number" value="{int(getattr(config, 'RECEIPT_OCR_TIMEOUT', 20))}" min="5" max="120"></div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="setting-section" id="xui" data-tab-section="xui">
    <div class="grid">
      <div class="card half xui-connection-card">
        <div class="section-title"><div><h2>Подключение к 3x-ui</h2><div class="muted">Единый адрес панели: браузерный переход и API используют одну настройку.</div></div></div>
        <div class="setting"><label>URL панели 3x-ui</label><input type="text" inputmode="url" name="xui_panel_url" value="{_config_text('XUI_PANEL_URL', getattr(config, 'BASE_URL', ''))}" placeholder="https://example.com:2053/panel/"><div class="muted">API-токен в ссылку не попадает.</div></div>
        <div class="setting"><label>Публичный URL FargoVPN</label><input type="url" value="{html.escape(cabinet_service.public_web_base_url() or "")}" readonly><div class="muted">Адрес формируется автоматически: HTTPS 443 + уникальный путь. Он используется кнопками «Кабинет» и «Сообщения»; отдельный порт не настраивается.</div></div>
        <div class="setting"><label>Базовый URL подписок</label><input type="text" inputmode="url" name="sub_url" value="{_config_text('SUB_BASE_URL')}"></div>
        <div class="setting"><label>API-токен 3x-ui</label><input type="password" name="api_token" placeholder="Пусто — оставить текущий" autocomplete="new-password"></div>
        <div class="check-row"><label><input type="checkbox" name="xui_verify_tls" value="1" {_config_checked('XUI_VERIFY_TLS', True)}> Проверять TLS-сертификат 3x-ui</label></div>
        <div class="integration-actions">{f'<a class="button secondary" href="{html.escape(str(getattr(config, 'XUI_PANEL_URL', '') or getattr(config, 'BASE_URL', '')).rstrip('/'), quote=True)}" target="_blank" rel="noopener noreferrer">◈ Открыть 3x-ui</a>' if (str(getattr(config, 'XUI_PANEL_URL', '') or getattr(config, 'BASE_URL', '')).strip()) else ''}{f'<a class="button secondary" href="{html.escape(str(getattr(config, 'BOT_PANEL_URL', '')).rstrip('/'), quote=True)}" target="_blank" rel="noopener noreferrer">V Открыть FargoVPN</a>' if str(getattr(config, 'BOT_PANEL_URL', '') or '').strip() else ''}</div>
      </div>
      <div class="card half">
        <h2>Синхронизация и уведомления</h2>
        <div class="setting"><label>Кэш снимка 3x-ui, секунд</label><input name="cache_seconds" type="number" value="{int(getattr(config, 'XUI_CACHE_SECONDS', 15))}" min="1" max="300"></div>
        <div class="setting"><label>Дни напоминаний</label><input name="reminder_days" value="{html.escape(reminder_value)}" required><div class="muted">Например: 7, 3, 1, 0.</div></div>
        <div class="notice">Общий трафик берётся из счётчиков inbound. Сумма по пользователям используется только для детализации и не складывает дубли одного клиента.</div>
      </div>
    </div>
  </section>

  <section class="setting-section" id="security" data-tab-section="security">
    <div class="grid">
      <div class="card full">
        <h2>HTTPS / 443 и защита панели</h2>
        <div class="notice">TLS завершает существующий Nginx на порту 443. FargoVPN не открывает отдельный TCP-порт и не терминирует TLS самостоятельно. Публичный адрес панели использует отдельный уникальный путь на этом же HTTPS 443. Сертификат выбранного домена остаётся под управлением Nginx и системного Let's Encrypt.</div>
        <div class="two">
          <div class="setting"><label>Домен HTTPS</label><input value="{_config_text('WEB_DOMAIN') or _config_text('WEB_TLS_SERVER_NAME')}" readonly></div>
          <div class="setting"><label>Публичный путь FargoVPN</label><input value="{html.escape(str(getattr(config, 'WEB_PUBLIC_PREFIX', '') or ''))}" readonly></div>
        </div>
        <div class="notice">Отдельная выдача/выбор TLS-сертификата для FargoVPN отключены, чтобы не конфликтовать с маскирующим Nginx. Для проверки существующего сертификата используйте диагностику сервера.</div>
        <div class="card" style="margin-top:16px">
          <h3>Параметры защиты веб-панели</h3>
          <div class="two">
            <div class="setting"><label>Логин панели</label><input name="web_username" value="{_config_text('WEB_USERNAME', 'admin')}" maxlength="100" required></div>
            <div class="setting"><label>Новый пароль</label><input type="password" name="web_password" placeholder="Пусто — оставить текущий" autocomplete="new-password"></div>
            <div class="setting"><label>Подтверждение пароля</label><input type="password" name="web_password_confirm" placeholder="Только при смене пароля" autocomplete="new-password"></div>
            <div class="setting"><label>Срок сессии, сек.</label><input type="number" name="web_session_max_age_seconds" value="{int(getattr(config, 'WEB_SESSION_MAX_AGE_SECONDS', 28800))}" min="900" max="604800"></div>
            <div class="setting"><label>Максимум ошибок входа</label><input type="number" name="web_login_max_attempts" value="{int(getattr(config, 'WEB_LOGIN_MAX_ATTEMPTS', 5))}" min="2" max="20"></div>
            <div class="setting"><label>Окно ошибок, сек.</label><input type="number" name="web_login_window_seconds" value="{int(getattr(config, 'WEB_LOGIN_WINDOW_SECONDS', 900))}" min="30" max="86400"></div>
            <div class="setting"><label>Первая блокировка, сек.</label><input type="number" name="web_login_block_seconds" value="{int(getattr(config, 'WEB_LOGIN_BLOCK_SECONDS', 900))}" min="30" max="86400"></div>
            <div class="setting"><label>Максимальная блокировка, сек.</label><input type="number" name="web_login_max_block_seconds" value="{int(getattr(config, 'WEB_LOGIN_MAX_BLOCK_SECONDS', 86400))}" min="30" max="604800"></div>
            <div class="setting"><label>Хранить журнал входов, дней</label><input type="number" name="web_login_security_retention_days" value="{int(getattr(config, 'WEB_LOGIN_SECURITY_RETENTION_DAYS', 30))}" min="1" max="365"></div>
          </div>
          <div class="check-row"><label><input type="checkbox" checked disabled> Только HTTPS-cookie</label><div class="muted">Фиксировано: панель работает за HTTPS reverse proxy.</div></div>
          <div class="check-row"><label><input type="checkbox" checked disabled> Доверять X-Forwarded-* от reverse proxy</label><div class="muted">Фиксировано для внешнего Nginx 443.</div></div>
        </div>
      </div>
    </div>
  </section>
  {updates_settings_block}
  <section class="setting-section" id="notifications" data-tab-section="notifications">
    <div class="grid">
      <div class="card full">
        <h2>Браузерные и PWA уведомления</h2>
        <div class="notice">Только для административной веб-панели. В личном кабинете пользователей Push не используется. Панель должна быть открыта через HTTPS 443; локальные HTTP-адреса для Push не подходят.</div>
        <div class="two">
          <div class="setting"><label>Состояние</label><div id="panel-push-status" class="badge">Не проверено</div><div id="panel-push-help" class="muted" style="margin-top:8px">Нажмите «Проверить», чтобы получить фактическое состояние.</div></div>
          <div class="setting"><label>Публичный адрес Service Worker</label><input value="{html.escape(public_path('/service-worker.js'), quote=True)}" readonly></div>
        </div>
        <div class="actions push-actions" style="display:flex;flex-wrap:wrap;gap:8px"><button type="button" id="panel-push-enable">Включить</button><button type="button" class="secondary" id="panel-push-test">Тест</button><button type="button" class="secondary" id="panel-push-disable">Выключить</button><button type="button" class="secondary" id="panel-push-check">Проверить</button><button type="button" class="secondary" id="panel-push-log-refresh">Обновить журнал</button></div>
        <div class="setting" style="margin-top:16px"><label>Журнал Push / Service Worker</label><pre id="panel-push-log" class="log-output" data-auto-scroll-bottom style="max-height:360px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere">Загрузка…</pre><div class="muted">Здесь фиксируются ответы backend и основные этапы регистрации браузерной подписки. Секреты и приватный VAPID-ключ в журнал не записываются.</div></div>
        <div class="setting" style="margin-top:16px"><label>Лог приложения</label><div class="muted">Файл: <span class="code">{html.escape(str(getattr(config, "APP_LOG_PATH", "/var/log/vpn_bot.log")))}</span></div><div class="actions" style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap"><button type="button" class="secondary" id="app-log-refresh">Обновить</button><select id="app-log-limit"><option value="100">Последние 100 строк</option><option value="500" selected>Последние 500 строк</option><option value="1000">Последние 1000 строк</option></select></div><pre id="app-log-output" class="log-output" style="max-height:420px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere">Загрузка…</pre></div>
      </div>
    </div>
  </section>
  <section class="setting-section" id="data" data-tab-section="data">
    <div class="grid">
      <div class="card half">
        <h2>Бэкапы</h2>
        <div class="two">
          <div class="setting"><label>Хранить локально, дней</label><input type="number" name="backup_keep_days" value="{int(getattr(config, 'BACKUP_KEEP_DAYS', 14))}" min="1" max="365"></div>
          <div class="setting"><label>Интервал бэкапа, дней</label><input type="number" name="backup_interval_days" value="{int(getattr(config, 'BACKUP_INTERVAL_DAYS', 3))}" min="1" max="30"><div class="muted">Служба проверяет необходимость ежедневно, но новый архив создаётся только по этому интервалу.</div></div>
          <div class="setting"><label>Повтор неудачной доставки, сек.</label><input type="number" name="backup_retry_interval_seconds" value="{int(getattr(config, 'BACKUP_RETRY_INTERVAL_SECONDS', 900))}" min="60" max="86400"></div>
          <div class="setting"><label>Хранить очередь доставки, дней</label><input type="number" name="backup_pending_keep_days" value="{int(getattr(config, 'BACKUP_PENDING_KEEP_DAYS', 30))}" min="1" max="365"></div>
          <div class="setting"><label>Часть Telegram, МБ</label><input type="number" name="backup_telegram_part_mb" value="{int(getattr(config, 'BACKUP_TELEGRAM_PART_MB', 45))}" min="5" max="49"></div>
        </div>
        <div class="check-row"><label><input type="checkbox" name="backup_telegram" value="1" {_config_checked('BACKUP_TELEGRAM', True)}> Отправлять резервные копии в Telegram</label></div>
        <div class="check-row"><label><input type="checkbox" name="backup_include_venv" value="1" {_config_checked('BACKUP_INCLUDE_VENV', True)}> Включать Python-окружение</label></div>
        <div class="settings-links"><a class="button secondary" href="{html.escape(public_path("/settings/yandex"), quote=True)}">Яндекс.Диск</a></div>
      </div>
      <div class="card half">
        <h2>Нагрузка и хранение истории</h2>
        <div class="setting"><label>Хранить чат и действия, дней</label><input type="number" name="user_event_keep_days" value="{int(getattr(config, 'USER_EVENT_KEEP_DAYS', 365))}" min="1" max="3650"></div>
        <div class="setting"><label>Максимум записей истории</label><input type="number" name="user_event_max_rows" value="{int(getattr(config, 'USER_EVENT_MAX_ROWS', 250000))}" min="1000" max="5000000"></div>
        <div class="setting"><label>Интервал записи метрик, сек.</label><input type="number" name="metrics_store_interval_seconds" value="{int(getattr(config, 'METRICS_STORE_INTERVAL_SECONDS', 60))}" min="15" max="3600"></div>
        <div class="setting"><label>Максимум архива импорта ID, МБ</label><input type="number" name="identity_import_max_mb" value="{int(getattr(config, 'IDENTITY_IMPORT_MAX_MB', 512))}" min="10" max="2048"></div>
        <div class="two">
          <div class="setting"><label>Максимальный файл в чате, МБ</label><input type="number" name="chat_media_max_mb" value="{int(getattr(config, 'CHAT_MEDIA_MAX_MB', 100))}" min="1" max="2000"></div>
          <div class="setting"><label>Хранить кэш медиа, дней</label><input type="number" name="chat_media_cache_days" value="{int(getattr(config, 'CHAT_MEDIA_CACHE_DAYS', 30))}" min="1" max="3650"></div>
          <div class="setting"><label>Максимальный кэш медиа, МБ</label><input type="number" name="chat_media_cache_max_mb" value="{int(getattr(config, 'CHAT_MEDIA_CACHE_MAX_MB', 512))}" min="16" max="10240"></div>
        </div>
        <div class="muted">История очищается без тяжёлого VACUUM. Фото и видео кэшируются отдельно и удаляются по возрасту и общему размеру.</div>
      </div>
    </div>
  </section>

  <div class="card full settings-actions">
    <div><strong>Сохранение применит конфигурацию атомарно.</strong><div class="muted">Бот и веб-панель автоматически перезапустятся через несколько секунд.</div></div>
    <button type="submit">Сохранить настройки</button>
  </div>
</form>
<script>
(function(){{
  // Settings must remain usable even when an old PWA cache delays panel.js.
  const settingsForm=document.getElementById('settings-form');
  if(settingsForm){{
    const saveButton=settingsForm.querySelector('button[type="submit"]');
    const saveCard=settingsForm.querySelector('.settings-actions');
    let status=settingsForm.querySelector('#settings-save-status');
    if(!status){{
      status=document.createElement('div');
      status.id='settings-save-status';
      status.className='notice';
      status.style.marginTop='10px';
      status.setAttribute('role','status');
      status.setAttribute('aria-live','polite');
      if(saveCard) saveCard.appendChild(status);
    }}
    settingsForm.addEventListener('submit', function(){{
      if(status){{
        status.textContent='Отправляю настройки на сервер…';
        status.hidden=false;
      }}
      if(saveButton){{
        saveButton.disabled=true;
        saveButton.dataset.originalText=saveButton.textContent;
        saveButton.textContent='Сохранение…';
      }}
      // Do not cancel the submit event: the browser sends the real POST.
    }});
  }}
  const root=document.querySelector('[data-tabs]');
  if(!root) return;
  const buttons=[...root.querySelectorAll('[data-tab]')];
  const sections=[...document.querySelectorAll('[data-tab-section]')];
  const activate=(name)=>{{
    buttons.forEach(b=>{{
      const on=b.dataset.tab===name;
      b.classList.toggle('active',on);
      b.setAttribute('aria-selected',on?'true':'false');
    }});
    sections.forEach(s=>s.classList.toggle('active',s.dataset.tabSection===name));
    try{{if(history.replaceState) history.replaceState(null,'','{html.escape(public_path('/settings'), quote=True)}'+'#'+encodeURIComponent(name));}}catch(_){{}}
  }};
  buttons.forEach(b=>b.addEventListener('click',(e)=>{{e.preventDefault();activate(b.dataset.tab);}}));
  let name=''; try{{name=decodeURIComponent(location.hash.slice(1));}}catch(_){{}}
  activate(buttons.some(b=>b.dataset.tab===name)?name:(buttons[0]?.dataset.tab||''));
}})();
</script>
'''
    return page(request, "Настройки", body, "settings", scripts=panel_push_script)




@app.post("/settings")
async def save_settings(request: Request):
    require_auth(request)
    form = await request.form()
    try:
        return await run_in_threadpool(_save_settings, request, form)
    except HTTPException as error:
        # Never expose FastAPI's raw JSON validation document to the human-facing settings form.
        set_flash(request, f"Не удалось сохранить настройки: {error.detail}", "bad")
        return RedirectResponse(public_path("/settings"), 303)
    except Exception as error:
        set_flash(request, f"Не удалось сохранить настройки: {error}")
        return RedirectResponse(public_path("/settings"), 303)


def _save_settings(request: Request, form):

    def text_value(name: str, default: str = "") -> str:
        return str(form.get(name, default) or "").strip()

    def checked(name: str) -> bool:
        return text_value(name) == "1"

    service_name = text_value("service_name")
    if not service_name or len(service_name) > 100:
        raise HTTPException(400, "Название сервиса должно содержать от 1 до 100 символов")

    raw_admin_ids = text_value("admin_ids")
    try:
        admin_ids = sorted({int(value) for value in re.split(r"[,;\s]+", raw_admin_ids) if value})
    except ValueError as error:
        raise HTTPException(400, "Telegram ID администраторов должны быть целыми числами") from error
    if any(value <= 0 for value in admin_ids):
        raise HTTPException(400, "Telegram ID администраторов должны быть положительными")

    web_username = text_value("web_username")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", web_username):
        raise HTTPException(400, "Логин панели: 3–64 символа, латиница, цифры и . _ @ -")
    web_password = text_value("web_password")
    if web_password:
        if len(web_password) < 10 or len(web_password) > 200:
            raise HTTPException(400, "Новый пароль должен содержать от 10 до 200 символов")
        if web_password != text_value("web_password_confirm"):
            raise HTTPException(400, "Повтор нового пароля не совпадает")

    try:
        reminder_days = sorted(
            {int(value) for value in re.split(r"[,;\s]+", text_value("reminder_days")) if value},
            reverse=True,
        )
    except ValueError as error:
        raise HTTPException(400, "Дни напоминаний должны быть целыми числами") from error
    if not reminder_days or any(day < 0 or day > 365 for day in reminder_days):
        raise HTTPException(400, "Некорректный список дней напоминаний")

    web_timezone = text_value("web_timezone", "Asia/Almaty")
    try:
        ZoneInfo(web_timezone)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(400, "Неизвестный часовой пояс панели") from error

    receipt_timezone = text_value("receipt_timezone", web_timezone)
    try:
        ZoneInfo(receipt_timezone)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(400, "Неизвестный часовой пояс чеков") from error
    web_domain = text_value("web_domain", str(getattr(config, "WEB_DOMAIN", "")))
    tls_server_name = str(getattr(config, "WEB_TLS_SERVER_NAME", "") or "").strip()
    if web_domain:
        if not re.fullmatch(r"(?:\*\.)?[A-Za-z0-9.-]+", web_domain) or len(web_domain) > 253:
            raise HTTPException(400, "Некорректное имя домена веб-панели")
    # TLS is terminated by the existing Nginx 443 frontend. Legacy form fields are ignored.
    receipt_languages = text_value("receipt_ocr_languages", "rus+eng")
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", receipt_languages) or len(receipt_languages) > 80:
        raise HTTPException(400, "Некорректный список языков OCR")

    xui_raw = text_value("xui_panel_url")
    xui_panel_url = _http_url(xui_raw, "URL панели 3x-ui") if xui_raw else ""
    api_base_url = xui_panel_url
    if urlsplit(api_base_url).path.rstrip("/").lower().endswith("/panel"):
        parts = urlsplit(api_base_url)
        api_base_url = parts._replace(path=parts.path.rstrip("/")[:-len("/panel")]).geturl()
    base_url = _http_url(api_base_url, "URL API 3x-ui", trailing_slash=True) if api_base_url else ""
    bot_panel_url = ""
    public_domain = str(web_domain or tls_server_name or getattr(config, "WEB_DOMAIN", "") or "").strip()
    public_prefix_value = str(getattr(config, "WEB_PUBLIC_PREFIX", "") or "").strip()
    if public_domain and re.fullmatch(r"/[A-Za-z0-9_-]{8,96}", public_prefix_value):
        bot_panel_url = f"https://{public_domain}{public_prefix_value.rstrip('/')}/"
    sub_raw = text_value("sub_url")
    sub_url = _http_url(sub_raw, "URL подписок", allow_empty=True, trailing_slash=True)
    faq_incy_url = _http_url(text_value("faq_incy_url"), "Ссылка Incy")
    current_session_username = str(request.session.get("user") or "").strip()
    current_is_publisher = update_publisher_for_request(request)
    if not current_is_publisher and update_manager.hmac_compare(web_username, update_manager.PUBLISHER_USERNAME):
        raise HTTPException(403, "Логин главного издателя доступен только главной панели")
    is_publisher = current_is_publisher
    if current_is_publisher:
        github_owner = text_value("github_repository_owner")
        github_repo = text_value("github_repository_name", "FargoVPN") or "FargoVPN"
        github_branch = text_value("github_target_branch", "main")
        github_tag_prefix = text_value("github_release_tag_prefix", "FargoVPN-")
        github_name_template = text_value("github_release_name_template", "FargoVPN {version}")
        github_asset_template = text_value("github_release_asset_name", "VPN_Service_Platform_{version}_FULL.tar.gz")
    else:
        github_owner = str(getattr(config, "GITHUB_REPOSITORY_OWNER", ""))
        github_repo = str(getattr(config, "GITHUB_REPOSITORY_NAME", "FargoVPN"))
        github_branch = str(getattr(config, "GITHUB_TARGET_BRANCH", "main"))
        github_tag_prefix = str(getattr(config, "GITHUB_RELEASE_TAG_PREFIX", "FargoVPN-"))
        github_name_template = str(getattr(config, "GITHUB_RELEASE_NAME_TEMPLATE", "FargoVPN {version}"))
        github_asset_template = str(getattr(config, "GITHUB_RELEASE_ASSET_NAME", "VPN_Service_Platform_{version}_FULL.tar.gz"))
    if github_owner and not re.fullmatch(r"[A-Za-z0-9-]{1,39}", github_owner):
        raise HTTPException(400, "Владелец GitHub-репозитория указан некорректно")
    if github_repo and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", github_repo):
        raise HTTPException(400, "Имя GitHub-репозитория указано некорректно")
    if not re.fullmatch(r"[A-Za-z0-9_.\-/]{1,255}", github_branch):
        raise HTTPException(400, "Ветка GitHub указана некорректно")
    if not re.fullmatch(r"[A-Za-z0-9._+\- ]{1,64}", github_tag_prefix):
        raise HTTPException(400, "Префикс GitHub tag содержит недопустимые символы")
    if len(github_name_template) > 200 or "{version}" not in github_name_template:
        raise HTTPException(400, "Имя GitHub release должно содержать {version}")
    if len(github_asset_template) > 255 or not github_asset_template.endswith(".tar.gz") or "{version}" not in github_asset_template:
        raise HTTPException(400, "Имя GitHub asset должно содержать {version} и заканчиваться .tar.gz")

    welcome_text = text_value("bot_welcome_text")
    support_prompt = text_value("bot_support_prompt")
    aliases = text_value("receipt_aliases")
    if len(welcome_text) > 3500 or len(support_prompt) > 1500 or len(aliases) > 2000:
        raise HTTPException(400, "Один из текстовых параметров превышает допустимую длину")

    login_block = _form_int(form, "web_login_block_seconds", 900, 30, 86_400, "Первая блокировка")
    login_max_block = _form_int(form, "web_login_max_block_seconds", 86_400, 30, 604_800, "Максимальная блокировка")
    if login_max_block < login_block:
        raise HTTPException(400, "Максимальная блокировка не может быть короче первой")

    values: dict[str, Any] = {
        "SERVICE_NAME": service_name,
        "ADMIN_IDS": admin_ids,
        "SUBSCRIPTION_DAYS": _form_int(form, "subscription_days", 30, 1, 3650, "Срок подписки"),
        "BOT_WELCOME_TEXT": welcome_text,
        "BOT_SUPPORT_PROMPT": support_prompt,
        "FAQ_INCY_URL": faq_incy_url,
        "BOT_IDENTITY_REFRESH_SECONDS": _form_int(form, "bot_identity_refresh_seconds", 600, 60, 86_400, "Обновление Telegram-привязки"),
        "BOT_SYNC_INTERVAL_SECONDS": _form_int(form, "bot_sync_interval_seconds", 3600, 300, 86_400, "Синхронизация бота"),
        "BROADCAST_MEDIA_MAX_MB": _form_int(form, "broadcast_media_max_mb", 45, 1, 49, "Размер файла рассылки"),
        "BROADCAST_SEND_DELAY_SECONDS": _form_float(form, "broadcast_send_delay_seconds", 0.04, 0.02, 5.0, "Пауза рассылки"),
        "BROADCAST_STALE_SECONDS": _form_int(form, "broadcast_stale_seconds", 7200, 300, 86_400, "Тайм-аут рассылки"),
        "PAYMENT_PRICE": _form_int(form, "price", 150, 0, 10_000_000, "Цена"),
        "PAYMENT_PHONE": text_value("phone")[:500],
        "PAYMENT_BANK": text_value("bank")[:200],
        "PAYMENT_RECEIVER": text_value("receiver")[:200],
        "RECEIPT_OCR_ENABLED": checked("receipt_ocr_enabled"),
        "RECEIPT_AUTO_APPROVE": checked("receipt_auto_approve"),
        "RECEIPT_ALLOW_MASKED_PHONE": checked("receipt_allow_masked_phone"),
        "RECEIPT_FILTER_NAME": checked("receipt_filter_name"),
        "RECEIPT_FILTER_PHONE": checked("receipt_filter_phone"),
        "RECEIPT_FILTER_AMOUNT": checked("receipt_filter_amount"),
        "RECEIPT_FILTER_DATE": checked("receipt_filter_date"),
        "RECEIPT_FILTER_STATUS": checked("receipt_filter_status"),
        "RECEIPT_FILTER_DUPLICATE": checked("receipt_filter_duplicate"),
        "RECEIPT_RECEIVER_ALIASES": aliases,
        "RECEIPT_MIN_AMOUNT": _form_float(form, "receipt_min_amount", 150.0, 0, 10_000_000, "Минимальная сумма"),
        "RECEIPT_MAX_AGE_HOURS": _form_int(form, "receipt_max_age_hours", 24, 1, 168, "Возраст чека"),
        "WEB_TIMEZONE": web_timezone,
        "RECEIPT_TIMEZONE": receipt_timezone,
        "RECEIPT_OCR_LANGUAGES": receipt_languages,
        "RECEIPT_OCR_TIMEOUT": _form_int(form, "receipt_ocr_timeout", 20, 5, 120, "Тайм-аут OCR"),
        "BASE_URL": base_url,
        "MASTER_API_URL": base_url,
        "WEB_DOMAIN": web_domain,
        "XUI_PANEL_URL": xui_panel_url,
        "BOT_PANEL_URL": bot_panel_url,
        "PUBLIC_PANEL_URL": bot_panel_url,
        "WEB_REVERSE_PROXY": True,
        "WEB_HOST": "127.0.0.1",
        "WEB_SOCKET_PATH": "/run/vpn-service/fargovpn.sock",
        "WEB_COOKIE_HTTPS_ONLY": True,
        "WEB_TRUST_PROXY_HEADERS": True,
        "SUB_BASE_URL": sub_url,
        "XUI_CACHE_SECONDS": _form_int(form, "cache_seconds", 15, 1, 300, "Кэш 3x-ui"),
        "XUI_VERIFY_TLS": checked("xui_verify_tls"),
        "REMINDER_DAYS": reminder_days,
        "WEB_USERNAME": web_username,
        "WEB_COOKIE_HTTPS_ONLY": True,
        "WEB_TRUST_PROXY_HEADERS": True,
        "WEB_SESSION_MAX_AGE_SECONDS": _form_int(form, "web_session_max_age_seconds", 28_800, 900, 604_800, "Срок сессии"),
        "WEB_LOGIN_MAX_ATTEMPTS": _form_int(form, "web_login_max_attempts", 5, 2, 20, "Ошибки входа"),
        "WEB_LOGIN_WINDOW_SECONDS": _form_int(form, "web_login_window_seconds", 900, 30, 86_400, "Окно ошибок"),
        "WEB_LOGIN_BLOCK_SECONDS": login_block,
        "WEB_LOGIN_MAX_BLOCK_SECONDS": login_max_block,
        "WEB_LOGIN_SECURITY_RETENTION_DAYS": _form_int(form, "web_login_security_retention_days", 30, 1, 365, "Хранение попыток входа"),
        "UPDATE_PUBLISHER_USERNAME": str(getattr(config, "UPDATE_PUBLISHER_USERNAME", "") or web_username),
        "UPDATE_IS_PUBLISHER": is_publisher,
        "GITHUB_REPOSITORY_OWNER": github_owner,
        "GITHUB_REPOSITORY_NAME": github_repo,
        "GITHUB_TARGET_BRANCH": github_branch,
        "GITHUB_RELEASE_TAG_PREFIX": github_tag_prefix,
        "GITHUB_RELEASE_NAME_TEMPLATE": github_name_template,
        "GITHUB_RELEASE_ASSET_NAME": github_asset_template,
        "GITHUB_RELEASE_MAKE_LATEST": checked("github_release_make_latest"),
        "GITHUB_RELEASE_DRAFT": checked("github_release_draft"),
        "GITHUB_RELEASE_PRERELEASE": checked("github_release_prerelease") if current_is_publisher else bool(getattr(config, "GITHUB_RELEASE_PRERELEASE", False)),
        "UPDATE_CHECK_INTERVAL": _form_int(form, "update_check_interval", 60, 15, 86_400, "Интервал обновлений") if current_is_publisher else int(getattr(config, "UPDATE_CHECK_INTERVAL", 60)),
        "UPDATE_VERIFY_TLS": True,
        "UPDATE_MAX_ARCHIVE_MB": _form_int(form, "update_max_archive_mb", 1024, 64, 4096, "Размер архива обновления") if current_is_publisher else int(getattr(config, "UPDATE_MAX_ARCHIVE_MB", 1024)),
        "UPDATE_STALE_JOB_SECONDS": _form_int(form, "update_stale_job_seconds", 7200, 900, 86_400, "Тайм-аут зависшей задачи") if current_is_publisher else int(getattr(config, "UPDATE_STALE_JOB_SECONDS", 7200)),
        "BACKUP_KEEP_DAYS": _form_int(form, "backup_keep_days", 14, 1, 365, "Хранение бэкапов"),
        "BACKUP_INTERVAL_DAYS": _form_int(form, "backup_interval_days", 3, 1, 30, "Интервал бэкапов"),
        "BACKUP_RETRY_INTERVAL_SECONDS": _form_int(form, "backup_retry_interval_seconds", 900, 60, 86_400, "Повтор доставки бэкапа"),
        "BACKUP_PENDING_KEEP_DAYS": _form_int(form, "backup_pending_keep_days", 30, 1, 365, "Хранение очереди бэкапов"),
        "BACKUP_TELEGRAM": checked("backup_telegram"),
        "BACKUP_TELEGRAM_PART_MB": _form_int(form, "backup_telegram_part_mb", 45, 5, 49, "Размер части Telegram"),
        "BACKUP_INCLUDE_VENV": checked("backup_include_venv"),
        "USER_EVENT_KEEP_DAYS": _form_int(form, "user_event_keep_days", 365, 1, 3650, "Хранение истории"),
        "USER_EVENT_MAX_ROWS": _form_int(form, "user_event_max_rows", 250_000, 1000, 5_000_000, "Лимит истории"),
        "METRICS_STORE_INTERVAL_SECONDS": _form_int(form, "metrics_store_interval_seconds", 60, 15, 3600, "Интервал метрик"),
        "IDENTITY_IMPORT_MAX_MB": _form_int(form, "identity_import_max_mb", 512, 10, 2048, "Размер импорта"),
        "CHAT_MEDIA_MAX_MB": _form_int(form, "chat_media_max_mb", 100, 1, 2000, "Размер медиа в чате"),
        "CHAT_MEDIA_CACHE_DAYS": _form_int(form, "chat_media_cache_days", 30, 1, 3650, "Хранение кэша медиа"),
        "CHAT_MEDIA_CACHE_MAX_MB": _form_int(form, "chat_media_cache_max_mb", 512, 16, 10240, "Размер кэша медиа"),
    }
    if web_password:
        values["WEB_PASSWORD_HASH"] = _password_hash(web_password)
    bot_token = text_value("bot_token")
    if bot_token:
        if len(bot_token) > 300:
            raise HTTPException(400, "Telegram-токен слишком длинный")
        values["BOT_TOKEN"] = bot_token
    api_token = text_value("api_token")
    if api_token:
        if len(api_token) > 2000:
            raise HTTPException(400, "API-токен 3x-ui слишком длинный")
        values["MASTER_API_TOKEN"] = api_token
    github_token = text_value("github_api_token") if current_is_publisher else ""
    if github_token:
        if len(github_token) < 20 or len(github_token) > 500:
            raise HTTPException(400, "GitHub token имеет недопустимую длину")
        values["GITHUB_API_TOKEN"] = github_token

    save_config_values(values)
    for _key, _value in values.items():
        setattr(config, _key, _value)
    request.session["user"] = web_username
    auth_security.prune(db_path=config.DB_PATH)
    try:
        user_events.prune_events(
            keep_days=int(values["USER_EVENT_KEEP_DAYS"]),
            max_rows=int(values["USER_EVENT_MAX_ROWS"]),
            db_path=config.DB_PATH,
        )
    except Exception as error:
        # Database cleanup must never turn a successful settings save into a
        # failed-looking request. The cleanup can be retried later.
        LOGGER.warning("Не удалось выполнить обслуживание user_events после сохранения настроек: %s", error)
    audit(web_username, "settings_update", ",".join(sorted(values)))
    role_text = "главной" if is_publisher else "ведомой"
    set_flash(request, f"Настройки сохранены. Панель работает в роли {role_text}; службы перезапускаются.")
    schedule_restart(["vpn-service-bot", "vpn-service-web", "vpn-service-backup.service", "vpn-service-backup.timer", "vpn-service-reminders.service", "vpn-service-reminders.timer"], delay=2)
    return RedirectResponse(public_path("/settings"), 303)


@app.post("/api/tls/auto", response_class=JSONResponse)
def tls_auto_legacy(request: Request):
    require_auth(request)
    raise HTTPException(410, "TLS FargoVPN больше не настраивается приложением. Используется Nginx HTTPS 443.")


@app.post("/api/tls/apply", response_class=JSONResponse)
def tls_apply_legacy(request: Request):
    require_auth(request)
    raise HTTPException(410, "TLS FargoVPN больше не настраивается приложением. Используется Nginx HTTPS 443.")


@app.post("/api/tls/issue", response_class=JSONResponse)
async def tls_issue_legacy(request: Request):
    require_auth(request)
    raise HTTPException(410, "TLS FargoVPN больше не выпускается приложением. Сертификат обслуживается Nginx/Let's Encrypt.")

@app.get("/settings/yandex", response_class=HTMLResponse)
def yandex_settings(request: Request):
    require_auth(request)
    enabled = bool(getattr(config, "YANDEX_DISK_ENABLED", False))
    mode = str(getattr(config, "YANDEX_DISK_MODE", "oauth")).strip().lower()
    if mode not in {"oauth", "local"}:
        mode = "oauth"
    path_value = str(getattr(config, "YANDEX_DISK_PATH", "VPN-Service-Backups"))
    local_path = str(getattr(config, "YANDEX_LOCAL_PATH", "/mnt/yandex-disk"))
    require_mount = bool(getattr(config, "YANDEX_LOCAL_REQUIRE_MOUNT", True))
    token_set = bool(str(getattr(config, "YANDEX_DISK_TOKEN", "")).strip())
    upload_retries = max(1, int(getattr(config, "YANDEX_UPLOAD_RETRIES", 3)))
    verify_attempts = max(1, int(getattr(config, "YANDEX_UPLOAD_VERIFY_ATTEMPTS", 8)))
    verify_delay = max(0.0, float(getattr(config, "YANDEX_UPLOAD_VERIFY_DELAY", 1.5)))
    body = f'''<header><div><h1>Яндекс.Диск</h1><div class="subtitle">OAuth REST API или локально смонтированный WebDAV-каталог</div></div></header><div class="grid"><div class="card half"><h2>Подключение</h2><form method="post" action="{html.escape(public_path("/settings/yandex"), quote=True)}"><div class="setting"><label><input type="checkbox" name="enabled" value="1" {'checked' if enabled else ''}> Включить загрузку на Яндекс.Диск</label></div><div class="setting"><label>Режим</label><select name="mode"><option value="oauth" {'selected' if mode == 'oauth' else ''}>OAuth REST API</option><option value="local" {'selected' if mode == 'local' else ''}>Локально смонтированный WebDAV</option></select></div><div class="setting"><label>OAuth-токен (только для режима OAuth)</label><input type="password" name="token" placeholder="{'Токен уже сохранён; пусто — не менять' if token_set else 'Введите OAuth-токен'}"></div><div class="setting"><label>Точка монтирования (локальный режим)</label><input name="local_path" value="{html.escape(local_path)}" placeholder="/mnt/yandex-disk"></div><div class="setting"><label><input type="checkbox" name="require_mount" value="1" {'checked' if require_mount else ''}> Требовать настоящую точку монтирования (защита от записи на системный диск)</label></div><div class="setting"><label>Каталог для бэкапов</label><input name="path" value="{html.escape(path_value)}" required></div><div class="three"><div class="setting"><label>Попыток загрузки</label><input type="number" name="upload_retries" value="{upload_retries}" min="1" max="10"></div><div class="setting"><label>Проверок файла</label><input type="number" name="verify_attempts" value="{verify_attempts}" min="1" max="30"></div><div class="setting"><label>Пауза проверки, сек.</label><input type="number" step="0.1" name="verify_delay" value="{verify_delay:g}" min="0" max="30"></div></div><button name="action" value="save">Сохранить</button> <button class="secondary" name="action" value="test">Проверить подключение</button> <button class="secondary" name="action" value="test_upload">Тестовая загрузка + проверка</button></form></div><div class="card half"><h2>Локальный режим</h2><p>Установщик включает скрипт <span class="code">configure_yandex_webdav.sh</span>. Он монтирует <span class="code">https://webdav.yandex.ru</span> через davfs2.</p><p class="danger">Доступ по WebDAV предоставляется на тарифах Яндекс 360.</p><p>Для авторизации используйте логин Яндекса и отдельный пароль приложения WebDAV, а не основной пароль аккаунта.</p><pre id="yandex-script-command">sudo {html.escape(str(APP_DIR / "configure_yandex_webdav.sh"), quote=True)}</pre><button type="button" class="button secondary small" id="copy-yandex-command">Копировать команду</button><p class="muted">Перед созданием каталога панель выполняет PROPFIND, поэтому существующая папка больше не создаёт шумный HTTP 405. PUT и проверка размера выполняются через отдельные соединения. Если ответ PUT потерян, но файл уже сохранён, бэкап считается успешным после точного сравнения размера. При подтверждённой ошибке прямого WebDAV используется резервная запись через активную точку монтирования.</p></div></div>'''
    body += """<script>(function(){const b=document.getElementById('copy-yandex-command'),p=document.getElementById('yandex-script-command');if(b&&p)b.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(p.textContent.trim());b.textContent='Скопировано';setTimeout(()=>b.textContent='Копировать команду',1200);}catch(_){}});})();</script>"""
    return page(request, "Яндекс.Диск", body, "yandex")


@app.post("/settings/yandex")
def save_yandex_settings(
    request: Request,
    enabled: str | None = Form(None),
    mode: str = Form("oauth"),
    token: str = Form(""),
    local_path: str = Form("/mnt/yandex-disk"),
    require_mount: str | None = Form(None),
    path: str = Form("VPN-Service-Backups"),
    upload_retries: int = Form(3),
    verify_attempts: int = Form(8),
    verify_delay: float = Form(1.5),
    action: str = Form("save"),
):
    require_auth(request)
    mode = mode.strip().lower()
    if mode not in {"oauth", "local"}:
        raise HTTPException(400, "Некорректный режим Яндекс.Диска")
    token_value = token.strip() or str(getattr(config, "YANDEX_DISK_TOKEN", "")).strip()
    local_raw = local_path.strip() or "/mnt/yandex-disk"
    public_prefix_value = str(getattr(config, "WEB_PUBLIC_PREFIX", "") or "").rstrip("/")
    for fs_prefix in ("/mnt/", "/root/", "/etc/", "/var/", "/opt/", "/run/", "/tmp/", "/home/"):
        if public_prefix_value and local_raw.startswith(public_prefix_value + fs_prefix):
            local_raw = local_raw[len(public_prefix_value):]
            break
    local_path_value = str(Path(local_raw).expanduser())
    if not Path(local_path_value).is_absolute():
        raise HTTPException(400, "Точка монтирования должна быть абсолютным путём")
    if not 1 <= int(upload_retries) <= 10:
        raise HTTPException(400, "Количество попыток загрузки должно быть от 1 до 10")
    if not 1 <= int(verify_attempts) <= 30:
        raise HTTPException(400, "Количество проверок файла должно быть от 1 до 30")
    if not 0 <= float(verify_delay) <= 30:
        raise HTTPException(400, "Пауза проверки должна быть от 0 до 30 секунд")
    if action == "test":
        ok, detail = test_yandex_connection(
            token_value,
            mode=mode,
            local_path=local_path_value,
            require_mount=require_mount == "1",
        )
        set_flash(request, detail, "good" if ok else "bad")
        return RedirectResponse(public_path("/settings/yandex"), 303)
    if action == "test_upload":
        ok, detail = test_yandex_write(token_value, mode=mode, local_path=local_path_value, require_mount=require_mount == "1")
        set_flash(request, detail, "good" if ok else "bad")
        return RedirectResponse(public_path("/settings/yandex"), 303)
    clean_path = path.strip().strip("/") or "VPN-Service-Backups"
    values: dict[str, Any] = {
        "YANDEX_DISK_ENABLED": enabled == "1",
        "YANDEX_DISK_MODE": mode,
        "YANDEX_DISK_PATH": clean_path,
        "YANDEX_LOCAL_PATH": local_path_value,
        "YANDEX_LOCAL_REQUIRE_MOUNT": require_mount == "1",
        "YANDEX_UPLOAD_RETRIES": int(upload_retries),
        "YANDEX_UPLOAD_VERIFY_ATTEMPTS": int(verify_attempts),
        "YANDEX_UPLOAD_VERIFY_DELAY": float(verify_delay),
    }
    if token.strip():
        values["YANDEX_DISK_TOKEN"] = token.strip()
    save_config_values(values)
    audit(str(request.session.get("user", "web")), "yandex_settings", f"{mode}: {clean_path}")
    set_flash(request, "Настройки Яндекс.Диска сохранены")
    schedule_restart(["vpn-service-web"], delay=2)
    return RedirectResponse(public_path("/settings/yandex"), 303)


@app.get("/api/updates/status")
def updates_status_api(request: Request):
    require_auth(request)
    status = update_manager.read_status()
    return {
        **status,
        "state": str(status.get("state") or "idle"),
        "progress": int(status.get("progress") or 0),
        "busy": update_manager.update_job_busy(status),
        "installed_version": update_manager.current_version(),
    }


@app.get("/api/updates/availability")
def updates_availability_api(request: Request):
    require_auth(request)
    try:
        info = update_manager.check_available_update(force=False)
    except Exception as error:
        info = {"available": False, "error": str(error), "installed_version": update_manager.current_version()}
    return {
        "available": bool(info.get("available")),
        "version": str(info.get("version") or ""),
        "installed_version": str(info.get("installed_version") or update_manager.current_version()),
        "error": str(info.get("error") or ""),
        "source": str(info.get("source") or ""),
    }


@app.get("/api/updates/log")
def updates_log_api(request: Request):
    require_auth(request)
    path = update_manager.update_log_path()
    if not path.is_file():
        return PlainTextResponse("Журнал установки пока пуст.")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename="vpn-service-update.log")


@app.get("/api/updates/releases")
def updates_releases_api(request: Request):
    require_auth(request)
    installed = update_manager.current_version()
    try:
        releases = update_manager.github_release_history(30)
        return {
            "ok": True,
            "installed_version": installed,
            "items": [
                {
                    "version": str(item.get("version") or ""),
                    "published_at": str(item.get("published_at") or ""),
                    "size": int(item.get("size") or 0),
                    "older": update_manager.version_key(str(item.get("version") or "")) < update_manager.version_key(installed),
                }
                for item in releases
            ],
        }
    except Exception as error:
        return JSONResponse({"ok": False, "detail": str(error), "items": []}, status_code=502)


# Совместимость: старые клиенты /panel автоматически направляются на новые API-алиасы.
def update_publisher_for_request(request: Request) -> bool:
    """Return whether the current web session is the dedicated publisher account."""
    username = str(request.session.get("user") or "").strip()
    return bool(username) and update_manager.hmac_compare(username, update_manager.PUBLISHER_USERNAME)


def can_publish_update(request: Request) -> bool:
    return update_publisher_for_request(request) and update_manager.publisher_enabled()


@app.get("/updates", response_class=HTMLResponse)
def updates_page(request: Request):
    require_auth(request)
    info = update_manager.cached_update_info()
    status = update_manager.read_status()
    publisher = can_publish_update(request)
    previous_versions = update_manager.list_preupdate_backups()
    rollback_candidate = next((item for item in previous_versions if item.get("valid")), None)
    busy = update_manager.update_job_busy(status)
    state_names = {
        "idle": "Ожидание",
        "queued": "В очереди",
        "checking": "Проверка версии",
        "downloading": "Скачивание",
        "verifying": "Проверка архива",
        "extracting": "Распаковка",
        "scheduled": "Запланировано",
        "installing": "Установка",
        "migrating": "Обновление базы",
        "rolling-back": "Восстановление предыдущей версии",
        "restarting": "Перезапуск служб",
        "health-check": "Проверка запуска",
        "completed": "Завершено",
        "failed": "Ошибка",
    }
    state_code = str(status.get("state") or "idle")
    state_text = state_names.get(state_code, state_code)
    state_badge_class = "bad" if state_code == "failed" else ("warn" if busy else "good")
    source_code = str(info.get("source") or "")
    source_text = "GitHub Releases" if source_code == "github" else (source_code or "—")
    configured_repo = f"{getattr(config, 'GITHUB_REPOSITORY_OWNER', '')}/{getattr(config, 'GITHUB_REPOSITORY_NAME', 'FargoVPN')}"
    metadata_url = str(info.get("github_release_url") or "").strip()
    source_details = f'<p>Репозиторий: <span class="code">{html.escape(configured_repo)}</span></p>'
    if metadata_url:
        source_details += f'<p>Последний release: <a href="{html.escape(metadata_url, quote=True)}" target="_blank" rel="noopener">открыть GitHub</a></p>'
    state_badge = status_badge(bool(info.get("available")), "Доступно", "Актуально")
    installed_notes = update_manager.installed_changelog()
    installed_changelog_text = str(installed_notes.get("text") or "").strip()
    installed_changelog_version = str(installed_notes.get("version") or update_manager.current_version())
    installed_changelog_block = (
        f'<div class="card full changelog-card"><div class="section-title"><h2>Последняя установленная версия — {html.escape(installed_changelog_version)}</h2><span class="badge good">CHANGELOG</span></div><pre class="changelog">{html.escape(installed_changelog_text)}</pre></div>'
        if installed_changelog_text else ''
    )
    changelog_text = str(info.get("changelog") or "").strip()
    changelog_block = (
        f'<div class="card full changelog-card"><div class="section-title"><h2>Описание последнего GitHub Release — {html.escape(str(info.get("version") or "текущая версия"))}</h2><span class="badge good">GITHUB</span></div><pre class="changelog">{html.escape(changelog_text)}</pre></div>'
        if changelog_text else ''
    )
    rollback_block = ''
    if rollback_candidate:
        rollback_block = f'''<div class="card full"><div class="section-title"><h2>Откат к предыдущей версии</h2><span class="badge warn">Резервная копия найдена</span></div>
<p>Последняя сохранённая версия: <strong>{html.escape(str(rollback_candidate.get("version") or "неизвестно"))}</strong> · {html.escape(str(rollback_candidate.get("filename") or ""))}</p>
<p class="muted">Откат восстанавливает файлы приложения и сохранённую systemd-конфигурацию перед обновлением.</p>
<form method="post" action="{html.escape(public_path("/updates/rollback"), quote=True)}" onsubmit="return confirm('Откатить систему к предыдущей сохранённой версии? Текущая версия будет заменена.')"><input type="hidden" name="backup_path" value="{html.escape(str(rollback_candidate.get("path") or ""), quote=True)}"><button class="danger" {'disabled' if busy else ''}>Откатить к {html.escape(str(rollback_candidate.get("version") or "предыдущей версии"))}</button></form></div>'''
    elif previous_versions:
        rollback_error = str(previous_versions[0].get("validation_error") or "Снимок неполный")
        rollback_block = f'''<div class="card full"><div class="section-title"><h2>Откат к предыдущей версии</h2><span class="badge bad">Старый снимок повреждён</span></div><p class="muted">{html.escape(rollback_error)}. Кнопка отключена, чтобы не останавливать рабочую установку. Для возврата к старому релизу используйте безопасную принудительную установку выше.</p><button class="danger" disabled>Откат недоступен</button></div>'''
    apply_form = (
        f'<form id="apply-update-form" method="post" action="{html.escape(public_path("/updates/apply"), quote=True)}">'
        f'<button id="apply-update-button" data-confirm="Установить обновление и автоматически перезапустить службы?" {"disabled" if busy else ""}>'
        'Установить обновление</button></form>'
        if info.get("available")
        else f'<form id="check-updates-form" method="post" action="{html.escape(public_path("/updates/check"), quote=True)}"><button id="check-updates-button" class="secondary" type="submit">Проверить сейчас</button><div id="check-updates-status" class="muted" aria-live="polite"></div></form>'
    )
    manual_block = ""
    if publisher:
        token_fingerprint = hashlib.sha256(str(getattr(config, "GITHUB_API_TOKEN", "")).encode()).hexdigest()[:12] if str(getattr(config, "GITHUB_API_TOKEN", "")).strip() else "не настроен"
        publisher_block = f'''<div class="card half"><h2>Публикация GitHub Release</h2>
<p>Архив из этой панели валидируется, затем выгружается как asset GitHub Release. В описание попадут CHANGELOG и SHA-256.</p>
<p>Токен: <span class="code">{token_fingerprint}</span> · Репозиторий: <span class="code">{html.escape(configured_repo)}</span></p>
<form id="publish-update-form" method="post" action="{html.escape(public_path("/updates/publish"), quote=True)}" enctype="multipart/form-data">
<div class="setting"><label>Полный архив релиза .tar.gz</label><input id="publish-archive" type="file" name="archive" accept=".tar.gz,application/gzip" required></div>
<button id="publish-button">Проверить и выгрузить на GitHub</button>
<div id="upload-progress" class="file-progress"><div class="progress large"><span id="upload-progress-bar" style="width:0%"></span></div><div id="upload-progress-text" class="muted">Загрузка…</div></div>
</form><form method="post" action="{html.escape(public_path("/updates/github/test"), quote=True)}" style="margin-top:12px"><button class="secondary">Проверить авторизацию GitHub</button></form>
<a class="button secondary" href="{html.escape(public_path("/settings"), quote=True)}#updates" style="margin-top:10px">Настройки GitHub</a></div>'''
    else:
        publisher_block = f'''<div class="card half"><h2>GitHub Releases</h2><p>Источник обновлений: <span class="code">{html.escape(configured_repo)}</span>.</p><p class="muted">Эта панель больше не зависит от мастер-панели и получает последний опубликованный GitHub Release.</p></div>'''
        manual_block = f'''<div class="card full"><div class="section-title"><h2>Ручная установка архива</h2><span class="badge warn">Локальный способ</span></div>
<p class="muted">Можно установить архив напрямую, если GitHub временно недоступен.</p>
<form id="manual-update-form" method="post" action="{html.escape(public_path("/updates/upload-and-apply"), quote=True)}" enctype="multipart/form-data">
<div class="setting"><label>Архив обновления .tar.gz</label><input id="manual-update-archive" type="file" name="archive" accept=".tar.gz,application/gzip" required></div>
<label class="check-row"><input type="checkbox" name="allow_reinstall" value="1"> Разрешить переустановку той же версии</label>
<button id="manual-update-button" {"disabled" if busy else ""}>Проверить файл и установить</button>
<div id="manual-upload-progress" class="file-progress"><div class="progress large"><span id="manual-upload-progress-bar" style="width:0%"></span></div><div id="manual-upload-progress-text" class="muted">Загрузка архива…</div></div>
</form></div>'''
    downgrade_block = f'''<div class="card full downgrade-card"><div class="section-title"><div><h2>Принудительная установка предыдущей версии</h2><p class="muted">Аварийный инструмент администратора. Конфигурация и база сохраняются установщиком.</p></div><span class="badge warn">Требует подтверждения</span></div>
<form id="force-version-form" class="force-version-form" method="post" action="{html.escape(public_path("/updates/force-version"), quote=True)}"><div class="setting"><label for="force-version-select">Версия из GitHub Releases</label><select id="force-version-select" name="version" required disabled><option value="">Загрузка списка…</option></select></div><label class="check-row"><input type="checkbox" name="confirm_downgrade" value="1" required> Понимаю, что будет установлена более ранняя версия</label><button id="force-version-button" class="danger" disabled {'disabled' if busy else ''}>Установить выбранную версию</button><div id="force-version-status" class="muted">Получаю опубликованные версии…</div></form></div>'''
    error = f'<div class="notice">{html.escape(str(info.get("error")))}</div>' if info.get("error") else ""
    progress_value = max(0, min(100, int(status.get("progress") or 0)))
    status_message = str(status.get("message") or status.get("detail") or "Установка ещё не запускалась")
    status_error = str(status.get("error") or "")
    progress_html = f"""<div class="card full update-progress-card"><div class="section-title"><h2>Ход установки</h2><span id="update-state" class="badge {state_badge_class}">{html.escape(state_text)}</span></div>
<div class="status-panel"><div class="progress large"><span id="update-progress-bar" style="width:{progress_value}%"></span></div><div class="progress-meta"><span id="update-message">{html.escape(status_message)}</span><strong id="update-progress-value">{progress_value}%</strong></div><div id="update-error" class="notice" style="{'display:block' if status_error else 'display:none'};margin-top:14px">{html.escape(status_error)}</div><p class="muted" id="update-connection-note">При установке службы перезапускаются автоматически; статус задачи сохраняется локально.</p></div>
<p><a href="{html.escape(public_path("/api/updates/log"), quote=True)}" class="button secondary small" target="_blank">Открыть журнал установки</a></p></div>"""
    body = f"""<header><div><h1>Обновления</h1><div class="subtitle">GitHub Releases как единый источник обновлений FargoVPN</div></div></header>{error}{progress_html}
<div class="grid"><div class="card half"><div class="section-title"><h2>Текущая установка</h2>{state_badge}</div>
<p>Установлено: <strong id="update-current-version">{html.escape(update_manager.current_version())}</strong></p>
<p>Последний релиз: <strong>{html.escape(str(info.get('version') or '—'))}</strong></p>
<p>Источник: <span class="code">{html.escape(source_text)}</span></p>{source_details}{apply_form}</div>{publisher_block}{downgrade_block}{installed_changelog_block}{changelog_block}{rollback_block}{manual_block}</div>"""
    initial = json.dumps(status, ensure_ascii=False, default=str).replace("<", "\\u003c")
    script = r'''<script>
(function(){
const initial=__INITIAL__;
const basePath=__BASE_PATH__;
const purl=(p)=>{const raw=String(p||'/');if(raw===basePath||raw.startsWith(basePath+'/'))return raw;return basePath+(raw.startsWith('/')?raw:'/'+raw);};
const pageVersion=__PAGE_VERSION__;
const busyStates=new Set(['queued','checking','downloading','verifying','extracting','scheduled','installing','migrating','rolling-back','restarting','health-check']);
const stateLabels={idle:'Ожидание',queued:'В очереди',checking:'Проверка версии',downloading:'Скачивание',verifying:'Проверка архива',extracting:'Распаковка',scheduled:'Запланировано',installing:'Установка',migrating:'Обновление базы','rolling-back':'Восстановление предыдущей версии',restarting:'Перезапуск служб','health-check':'Проверка запуска',completed:'Завершено',failed:'Ошибка'};
const phaseCaps={queue:3,acknowledged:4,check:7,download:30,verify:32,extract:49,install:52,dependencies:59,'stop-services':63,backup:69,files:74,python:84,database:90,services:95,restart:97,health:99,complete:100,failed:99};
let lastStatus=initial||{};let lastState=String(lastStatus.state||'idle');let currentJob=String(lastStatus.job_id||sessionStorage.getItem('fargovpn_update_job')||'');let serverProgress=Math.max(0,Math.min(100,Number(lastStatus.progress||0)));let shownProgress=serverProgress;let goalProgress=serverProgress;let lastServerUpdate=performance.now();let reloadScheduled=false;let recoveryInFlight=false;
const bar=document.getElementById('update-progress-bar');const value=document.getElementById('update-progress-value');const note=document.getElementById('update-connection-note');
function paintProgress(){if(bar)bar.style.width=shownProgress.toFixed(2)+'%';if(value)value.textContent=Math.floor(shownProgress)+'%';}
function animateProgress(now){if(busyStates.has(lastState)){const cap=Math.max(serverProgress,Number(phaseCaps[String(lastStatus.phase||'')]||serverProgress));const elapsed=Math.max(0,(now-lastServerUpdate)/1000);const span=Math.max(0,cap-serverProgress);const interpolated=serverProgress+span*(1-Math.exp(-elapsed/7));goalProgress=Math.max(goalProgress,Math.min(cap,interpolated));}else{goalProgress=serverProgress;}const delta=goalProgress-shownProgress;if(Math.abs(delta)>0.015){if(delta>0)shownProgress+=Math.min(delta,Math.max(0.035,delta*0.075));else shownProgress=Math.max(goalProgress,shownProgress-Math.max(0.1,Math.abs(delta)*0.2));paintProgress();}requestAnimationFrame(animateProgress);}
function setRequestError(text){const error=document.getElementById('update-error');if(error){error.textContent=text||'';error.style.display=text?'block':'none';}if(note&&text)note.textContent='Задача не была подтверждена сервером. Исправьте указанную ошибку и повторите запуск.';}
function rememberJob(job){if(job){try{sessionStorage.setItem('fargovpn_update_job',job)}catch(_){}}}
function showReconnect(message){let box=document.getElementById('update-reconnect');if(!box){box=document.createElement('div');box.id='update-reconnect';box.className='update-reconnect card';box.innerHTML='<strong>Веб-панель перезапускается</strong><span></span>';document.body.appendChild(box)}const span=box.querySelector('span');if(span)span.textContent=message||'Соединение будет восстановлено автоматически.';box.classList.add('visible')}
function hideReconnect(){const box=document.getElementById('update-reconnect');if(box)box.classList.remove('visible')}
async function fetchWithTimeout(url,options={},timeout=5000){const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeout);try{return await fetch(url,{...options,signal:controller.signal})}finally{clearTimeout(timer)}}

function renderStatus(data){data=data||{};const nextJob=String(data.job_id||currentJob||'');const nextProgress=Math.max(0,Math.min(100,Number(data.progress||0)));if(nextJob&&currentJob&&nextJob!==currentJob){shownProgress=nextProgress;goalProgress=nextProgress;}currentJob=nextJob;rememberJob(currentJob);serverProgress=nextProgress;goalProgress=Math.max(goalProgress,serverProgress);lastServerUpdate=performance.now();lastStatus=data;lastState=String(data.state||'idle');const message=document.getElementById('update-message');if(message)message.textContent=data.message||data.detail||'Ожидание запуска';const badge=document.getElementById('update-state');if(badge){badge.textContent=stateLabels[lastState]||lastState||'Ожидание';badge.className='badge '+(lastState==='failed'?'bad':(lastState==='completed'||lastState==='idle'?'good':'warn'));}const error=document.getElementById('update-error');if(error){error.textContent=data.error||'';error.style.display=data.error?'block':'none';}const busy=busyStates.has(lastState);if(bar)bar.classList.toggle('active',busy);if(lastState==='completed'){serverProgress=100;goalProgress=100;shownProgress=100;}const apply=document.getElementById('apply-update-button');if(apply)apply.disabled=busy;const manual=document.getElementById('manual-update-button');if(manual)manual.disabled=busy;const version=document.getElementById('update-current-version');if(version&&data.installed_version)version.textContent=data.installed_version;if(note)note.textContent='Статус получен: '+(data.updated_at||'только что')+'. Во время перезапуска подключение восстановится автоматически.';if(data.installed_version&&String(data.installed_version)!==String(pageVersion)&&lastState==='completed'&&!reloadScheduled){reloadScheduled=true;if(note)note.textContent='Обновление завершено. Интерфейс будет обновлён автоматически.';setTimeout(()=>{window.location.replace(purl('/updates')+'?installed='+encodeURIComponent(String(data.installed_version))+'&job='+encodeURIComponent(currentJob));},700);}if(!busy)hideReconnect();paintProgress();return data;}
async function fetchStatus(){const response=await fetchWithTimeout(purl('/api/updates/status'),{cache:'no-store',credentials:'same-origin',headers:{Accept:'application/json'}},5000);if(!response.ok)throw new Error('HTTP '+response.status);return renderStatus(await response.json());}
const checkUpdatesForm=document.getElementById('check-updates-form');
if(checkUpdatesForm)checkUpdatesForm.addEventListener('submit',async event=>{event.preventDefault();const button=document.getElementById('check-updates-button'),statusBox=document.getElementById('check-updates-status');if(button)button.disabled=true;if(statusBox)statusBox.textContent='Проверяю GitHub Releases…';try{const response=await fetchWithTimeout(checkUpdatesForm.action,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json','X-Requested-With':'XMLHttpRequest'}},30000);let data={};try{data=await response.json();}catch(_e){}if(!response.ok||!data.ok)throw new Error(data.error||data.detail||('HTTP '+response.status));if(statusBox)statusBox.textContent=data.available?'Доступно обновление '+(data.version||'новой версии')+'.':'Установлена последняя версия ('+(data.installed_version||pageVersion)+').';if(data.available){setTimeout(()=>window.location.replace(purl('/updates')+'?checked=1'),450);}}catch(error){if(statusBox)statusBox.textContent='Не удалось проверить обновления: '+(error.message||error);}finally{if(button)button.disabled=false;}});
function scheduleStatusCheck(delay){setTimeout(()=>{fetchStatus().catch(()=>{});},Math.max(0,Number(delay)||0));}
async function recover(){if(recoveryInFlight)return;recoveryInFlight=true;try{showReconnect('Проверяем, восстановилась ли веб-служба после перезапуска…');for(;;){try{const health=await fetchWithTimeout(purl('/health'),{cache:'no-store',credentials:'same-origin'},3500);if(health.ok){await fetchStatus();hideReconnect();break;}}catch(_e){}await new Promise(r=>setTimeout(r,1500));}}finally{recoveryInFlight=false}}
async function poll(){try{await fetchStatus();}catch(_error){if(busyStates.has(lastState)){recover().catch(()=>{});}else if(note)note.textContent='Не удалось обновить статус задачи. Повторная проверка выполняется автоматически.';}finally{setTimeout(poll,busyStates.has(lastState)?1200:5000);}}
function optimisticStart(messageText){lastState='queued';lastStatus={...lastStatus,state:'queued',phase:'queue',message:messageText||'Запрос отправлен; ожидается подтверждение фоновой задачи'};serverProgress=Math.max(1,Math.min(serverProgress||1,3));goalProgress=Math.max(goalProgress,serverProgress);const message=document.getElementById('update-message');if(message)message.textContent=lastStatus.message;const badge=document.getElementById('update-state');if(badge){badge.textContent='В очереди';badge.className='badge warn';}if(bar)bar.classList.add('active');setRequestError('');if(note)note.textContent='Запрос передан серверу. Даже если веб-служба перезапустится до HTTP-ответа, статус будет восстановлен из файла задачи.';}
const applyForm=document.getElementById('apply-update-form');
if(applyForm)applyForm.addEventListener('submit',async(event)=>{event.preventDefault();if(!window.confirm('Установить обновление и автоматически перезапустить службы?'))return;window.dispatchEvent(new Event('vpn:update-starting'));const button=document.getElementById('apply-update-button');if(button)button.disabled=true;optimisticStart('Запрос на онлайн-обновление отправлен');try{const response=await fetchWithTimeout(purl('/updates/apply'),{method:'POST',headers:{Accept:'application/json'},credentials:'same-origin'},3500);let data={};try{data=await response.json();}catch(_e){}if(!response.ok){setRequestError(data.detail||data.error||'Сервер отклонил запуск обновления');lastState=String((lastStatus&&lastStatus.state)||'idle');if(button)button.disabled=false;return;}renderStatus(data.status||data);scheduleStatusCheck(250);}catch(_error){showReconnect('Задача могла уже запуститься. Ждём перезапуска веб-службы и читаем сохранённый статус.');recover().catch(()=>{});scheduleStatusCheck(1000);}});
const publishForm=document.getElementById('publish-update-form');
if(publishForm)publishForm.addEventListener('submit',(event)=>{event.preventDefault();const xhr=new XMLHttpRequest();const progress=document.getElementById('upload-progress');const uploadBar=document.getElementById('upload-progress-bar');const text=document.getElementById('upload-progress-text');const button=document.getElementById('publish-button');progress.classList.add('visible');button.disabled=true;text.textContent='Загрузка архива…';xhr.open('POST',purl('/updates/publish'));xhr.setRequestHeader('Accept','application/json');xhr.upload.onprogress=(e)=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);uploadBar.style.width=p+'%';text.textContent='Загружено '+p+'%';}};xhr.onload=()=>{button.disabled=false;let data={};try{data=JSON.parse(xhr.responseText);}catch(_e){}if(xhr.status>=200&&xhr.status<300){uploadBar.style.width='100%';text.textContent='Архив проверен и опубликован: '+(data.version||'готово');const published=document.getElementById('published-version');if(published&&data.version)published.textContent=data.version;}else{text.textContent='Ошибка: '+(data.detail||data.error||'HTTP '+xhr.status);}};xhr.onerror=()=>{button.disabled=false;text.textContent='Ошибка соединения при загрузке';};xhr.send(new FormData(publishForm));});
const manualForm=document.getElementById('manual-update-form');
if(manualForm)manualForm.addEventListener('submit',(event)=>{event.preventDefault();if(!window.confirm('Проверить загруженный архив и установить его на этой панели?'))return;window.dispatchEvent(new Event('vpn:update-starting'));const xhr=new XMLHttpRequest();const progress=document.getElementById('manual-upload-progress');const uploadBar=document.getElementById('manual-upload-progress-bar');const text=document.getElementById('manual-upload-progress-text');const button=document.getElementById('manual-update-button');progress.classList.add('visible');button.disabled=true;text.textContent='Загрузка архива на панель…';xhr.open('POST',purl('/updates/upload-and-apply'));xhr.setRequestHeader('Accept','application/json');xhr.upload.onprogress=(e)=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);uploadBar.style.width=p+'%';text.textContent='Загружено '+p+'%';}};xhr.onload=()=>{let data={};try{data=JSON.parse(xhr.responseText);}catch(_e){}if(xhr.status>=200&&xhr.status<300){uploadBar.style.width='100%';text.textContent='Архив проверен; установка запущена';optimisticStart('Ручной архив принят, запускается фоновая установка');renderStatus(data.status||data);scheduleStatusCheck(250);}else{button.disabled=false;text.textContent='Ошибка: '+(data.detail||data.error||'HTTP '+xhr.status);setRequestError(data.detail||data.error||'Архив не принят');}};xhr.onerror=()=>{optimisticStart('Загрузка завершилась разрывом соединения; проверяется статус задачи');text.textContent='Соединение прервалось. Если архив был принят, прогресс появится автоматически.';scheduleStatusCheck(700);};xhr.send(new FormData(manualForm));});
async function loadDowngradeVersions(){const select=document.getElementById('force-version-select'),button=document.getElementById('force-version-button'),status=document.getElementById('force-version-status');if(!select)return;try{const response=await fetchWithTimeout(purl('/api/updates/releases'),{cache:'no-store',credentials:'same-origin',headers:{Accept:'application/json'}},22000);const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.detail||'Список релизов недоступен');const older=(data.items||[]).filter(item=>item.older);select.innerHTML='';if(!older.length){select.innerHTML='<option value="">Предыдущих версий не найдено</option>';if(status)status.textContent='В GitHub Releases нет версии ниже установленной.';return;}older.forEach(item=>{const option=document.createElement('option');option.value=item.version;option.textContent='Версия '+item.version+(item.published_at?' · '+new Date(item.published_at).toLocaleDateString('ru-RU'):'');select.appendChild(option)});select.disabled=false;if(button)button.disabled=busyStates.has(lastState);if(status)status.textContent='Доступно предыдущих версий: '+older.length+'.';}catch(error){select.innerHTML='<option value="">Не удалось загрузить версии</option>';if(status)status.textContent='Ошибка: '+(error.message||error);}}
const forceForm=document.getElementById('force-version-form');if(forceForm)forceForm.addEventListener('submit',async event=>{const selected=document.getElementById('force-version-select')?.value||'';if(!selected||!window.confirm('Принудительно установить версию '+selected+'? Перед заменой файлов будет создан снимок текущей установки.')){event.preventDefault();return;}event.preventDefault();const button=document.getElementById('force-version-button'),status=document.getElementById('force-version-status');if(button)button.disabled=true;if(status)status.textContent='Запускаю безопасное понижение версии…';try{const response=await fetch(forceForm.action,{method:'POST',body:new FormData(forceForm),credentials:'same-origin',headers:{Accept:'application/json'}});const type=String(response.headers.get('content-type')||'').toLowerCase();let data=null;if(type.includes('application/json'))data=await response.json();else{const text=await response.text();throw new Error('Сервер вернул '+(type||'неизвестный Content-Type')+' вместо JSON'+(text?' ('+text.slice(0,120)+')':''));}if(!response.ok||!data?.ok)throw new Error(data?.detail||('HTTP '+response.status));optimisticStart('Понижение версии '+selected+' запущено');renderStatus(data.status||data);scheduleStatusCheck(250);}catch(error){if(status)status.textContent='Ошибка: '+(error.message||error);if(button)button.disabled=false;}});
renderStatus(initial);loadDowngradeVersions();requestAnimationFrame(animateProgress);setTimeout(poll,450);
})();
</script>'''.replace('__INITIAL__', initial).replace('__PAGE_VERSION__', json.dumps(update_manager.current_version())).replace('__BASE_PATH__', json.dumps(public_prefix()))
    return page(request, "Обновления", body, "updates", script)

@app.post("/updates/check")
def updates_check(request: Request):
    require_auth(request)
    try:
        info = update_manager.check_available_update(force=True)
    except Exception as error:
        info = {"available": False, "error": str(error), "installed_version": update_manager.current_version()}
    wants_json = "application/json" in request.headers.get("accept", "").lower() or request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
    if wants_json:
        return JSONResponse({
            "ok": not bool(info.get("error")),
            "available": bool(info.get("available")),
            "version": str(info.get("version") or ""),
            "installed_version": str(info.get("installed_version") or update_manager.current_version()),
            "source": str(info.get("source") or ""),
            "error": str(info.get("error") or ""),
        }, status_code=200 if not info.get("error") else 502)
    if info.get("available"):
        set_flash(request, f"Доступно обновление {info.get('version')}")
    elif info.get("error"):
        set_flash(request, str(info["error"]), "bad")
    else:
        set_flash(request, "Установлена актуальная версия")
    return RedirectResponse(public_path("/updates"), 303)


@app.post("/updates/github/test")
def updates_github_test(request: Request):
    require_auth(request)
    if not can_publish_update(request):
        raise HTTPException(403, "Настройка GitHub доступна только назначенной главной панели")
    try:
        result = update_manager.github_validate_configuration()
        set_flash(request, f"GitHub подключён: {result.get('login')} → {result.get('repository')}", "good")
    except Exception as error:
        set_flash(request, f"Проверка GitHub не пройдена: {error}", "bad")
    return RedirectResponse(public_path("/updates"), 303)

@app.post("/updates/github/config")
async def updates_github_config(request: Request):
    require_auth(request)
    if not can_publish_update(request):
        raise HTTPException(403, "Настройка GitHub доступна только назначенной главной панели")
    form = await request.form()
    return await run_in_threadpool(_updates_github_config, request, form)


def _updates_github_config(request: Request, form):
    token = str(form.get("github_api_token") or "").strip()
    values = {
        "GITHUB_REPOSITORY_OWNER": str(form.get("github_repository_owner") or "").strip(),
        "GITHUB_REPOSITORY_NAME": str(form.get("github_repository_name") or "FargoVPN").strip(),
        "GITHUB_TARGET_BRANCH": str(form.get("github_target_branch") or "main").strip(),
        "GITHUB_RELEASE_TAG_PREFIX": str(form.get("github_release_tag_prefix") or "FargoVPN-").strip(),
        "GITHUB_RELEASE_NAME_TEMPLATE": str(form.get("github_release_name_template") or "FargoVPN {version}").strip(),
        "GITHUB_RELEASE_ASSET_NAME": str(form.get("github_release_asset_name") or "VPN_Service_Platform_{version}_FULL.tar.gz").strip(),
        "GITHUB_RELEASE_MAKE_LATEST": str(form.get("github_release_make_latest") or "0") == "1",
        "GITHUB_RELEASE_DRAFT": str(form.get("github_release_draft") or "0") == "1",
        "GITHUB_RELEASE_PRERELEASE": str(form.get("github_release_prerelease") or "0") == "1",
    }
    if token:
        if len(token) < 20 or len(token) > 500:
            raise HTTPException(400, "GitHub token имеет недопустимую длину")
        values["GITHUB_API_TOKEN"] = token
    save_config_values(values)
    audit(str(request.session.get("user", "web")), "github_settings_saved", f"{values['GITHUB_REPOSITORY_OWNER']}/{values['GITHUB_REPOSITORY_NAME']}")
    try:
        result = update_manager.github_validate_configuration()
        set_flash(request, f"Настройки GitHub сохранены: {result.get('repository')}", "good")
    except Exception as error:
        set_flash(request, f"Настройки сохранены, но проверка GitHub не прошла: {error}", "bad")
    return RedirectResponse(public_path("/updates"), 303)

@app.post("/updates/config")
def updates_config_legacy(request: Request):
    require_auth(request)
    return JSONResponse({"ok": False, "detail": "Мастер-панель обновлений отключена в FargoVPN 3.0. Источник обновлений — GitHub Releases."}, status_code=410)

@app.post("/updates/token")
def updates_token_legacy(request: Request):
    require_auth(request)
    return JSONResponse({"ok": False, "detail": "Общий токен мастер-панели отключён в FargoVPN 3.0."}, status_code=410)

@app.post("/updates/publish")
def updates_publish(request: Request, archive: UploadFile = File(...)):
    require_auth(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    if not can_publish_update(request):
        raise HTTPException(403, "Публикация доступна только назначенной главной панели")
    max_size = max(1, int(getattr(config, "UPDATE_MAX_ARCHIVE_MB", 1024))) * 1024 * 1024
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="vpn_update_upload_", suffix=".tar.gz", delete=False) as handle:
            temp_path = Path(handle.name)
            total = 0
            while True:
                chunk = archive.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    raise update_manager.UpdateError("Архив превышает допустимый размер")
                handle.write(chunk)
        metadata = update_manager.publish_update(temp_path, archive.filename or "update.tar.gz")
        audit(str(request.session.get("user", "web")), "publish_update", str(metadata.get("version")))
        if wants_json:
            return JSONResponse({"ok": True, **update_manager.api_metadata(metadata)})
        set_flash(request, f"Обновление {metadata.get('version')} опубликовано")
    except Exception as error:
        if wants_json:
            return JSONResponse({"ok": False, "detail": str(error)}, status_code=400)
        set_flash(request, f"Архив отклонён: {error}", "bad")
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        archive.file.close()
    return RedirectResponse(public_path("/updates"), 303)


@app.post("/updates/upload-and-apply")
def updates_upload_and_apply(
    request: Request,
    archive: UploadFile = File(...),
    allow_reinstall: str | None = Form(None),
):
    """Validate a locally uploaded release and install it on a follower panel."""
    require_auth(request)
    if update_manager.publisher_enabled():
        raise HTTPException(403, "На главной панели используйте публикацию архива")
    if update_manager.update_job_busy():
        return JSONResponse(
            {"ok": False, "detail": "Другая установка обновления уже выполняется"},
            status_code=409,
        )
    max_size = max(1, int(getattr(config, "UPDATE_MAX_ARCHIVE_MB", 1024))) * 1024 * 1024
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="vpn_manual_update_", suffix=".tar.gz", delete=False) as handle:
            temp_path = Path(handle.name)
            total = 0
            while True:
                chunk = archive.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    raise update_manager.UpdateError("Архив превышает допустимый размер")
                handle.write(chunk)
        metadata = update_manager.store_manual_update(
            temp_path,
            archive.filename or "update.tar.gz",
            allow_reinstall=allow_reinstall == "1",
        )
        actor = str(request.session.get("user", "web"))
        job = update_manager.start_update_job(actor, update_info=metadata)
        status = update_manager.read_status()
        audit(actor, "manual_update_uploaded", f"{metadata.get('version')}:{metadata.get('sha256')}")
        return JSONResponse(
            {"ok": True, "version": metadata.get("version"), "job": job, "status": status},
            status_code=202,
        )
    except update_manager.UpdateError as error:
        return JSONResponse({"ok": False, "detail": str(error)}, status_code=409)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        archive.file.close()

@app.post("/updates/apply")
def updates_apply(request: Request):
    require_auth(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    try:
        # Запуск обновления не должен ждать сетевой проверки GitHub. Страница
        # /updates уже получила и закэшировала метаданные релиза; сам worker
        # повторно валидирует и скачивает пакет. Это не блокирует HTTP-ответ
        # на десятки секунд при проблемах с GitHub.
        info = update_manager.cached_update_info()
        if not info.get("available"):
            raise update_manager.UpdateError(
                str(info.get("error") or "Актуальные метаданные обновления отсутствуют. Нажмите «Проверить сейчас».")
            )
        job = update_manager.start_update_job(
            str(request.session.get("user", "web")),
            update_info=info,
        )
        audit(str(request.session.get("user", "web")), "apply_update", str(info.get("version")))
        status = update_manager.read_status()
        if wants_json:
            return JSONResponse({"ok": True, "job": job, "status": status}, status_code=202)
        set_flash(request, f"Обновление {info.get('version')} запущено; прогресс появится на этой странице")
    except Exception as error:
        if wants_json:
            return JSONResponse({"ok": False, "detail": str(error)}, status_code=409)
        set_flash(request, f"Не удалось запустить обновление: {error}", "bad")
    return RedirectResponse(public_path("/updates"), 303)


@app.post("/updates/force-version")
def updates_force_version(
    request: Request,
    version: str = Form(...),
    confirm_downgrade: str = Form(""),
):
    """Install one explicitly selected older GitHub release."""
    require_auth(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    try:
        if confirm_downgrade != "1":
            raise update_manager.UpdateError("Подтвердите принудительное понижение версии")
        info = update_manager.github_release_by_version(version)
        installed = update_manager.current_version()
        if update_manager.version_key(str(info.get("version") or "")) >= update_manager.version_key(installed):
            raise update_manager.UpdateError("Для этой кнопки выберите версию ниже установленной")
        info = {**info, "allow_downgrade": True, "requested_by_admin": True}
        job = update_manager.start_update_job(str(request.session.get("user", "web")), update_info=info)
        audit(str(request.session.get("user", "web")), "force_downgrade", json.dumps({"from": installed, "to": version, "job_id": job.get("job_id")}, ensure_ascii=False))
        if wants_json:
            return JSONResponse({"ok": True, "version": version, "job": job, "status": update_manager.read_status()}, status_code=202)
        set_flash(request, f"Принудительная установка версии {version} запущена", "good")
    except Exception as error:
        if wants_json:
            return JSONResponse({"ok": False, "detail": str(error)}, status_code=409)
        set_flash(request, f"Не удалось запустить понижение версии: {error}", "bad")
    return RedirectResponse(public_path("/updates"), 303)

@app.post("/updates/rollback")
def updates_rollback(request: Request, backup_path: str = Form(...)):
    require_auth(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    try:
        job = update_manager.start_rollback_job(str(request.session.get("user", "web")), backup_path)
        audit(str(request.session.get("user", "web")), "rollback_update", Path(backup_path).name)
        status = update_manager.read_status()
        if wants_json:
            return JSONResponse({"ok": True, "job": job, "status": status}, status_code=202)
        set_flash(request, "Откат запущен; дождитесь завершения проверки служб")
    except Exception as error:
        if wants_json:
            return JSONResponse({"ok": False, "detail": str(error)}, status_code=409)
        set_flash(request, f"Не удалось запустить откат: {error}", "bad")
    return RedirectResponse(public_path("/updates"), 303)

@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request):
    require_auth(request)
    units = {
        name: service_state(name)
        for name in (
            "vpn-service-bot",
            "vpn-service-web",
            "vpn-service-backup.timer",
            "vpn-service-reminders.timer",
        )
    }
    try:
        snapshot = fetch_and_sync(force=True, db_path=config.DB_PATH)
        api_ok = not bool(snapshot.get("stale"))
        api_text = "Подключено" if api_ok else str(snapshot.get("error") or "Доступен только кэш")
    except Exception as error:
        snapshot = {"stale": True, "error": str(error), "clients": []}
        api_ok, api_text = False, str(error)
    try:
        tg = httpx.get(f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMe", timeout=10, trust_env=False).json()
        tg_ok = bool(tg.get("ok"))
        tg_text = "Подключено" if tg_ok else str(tg.get("description") or tg)
    except Exception:
        tg_ok, tg_text = False, "Ошибка подключения к Telegram API"
    try:
        tesseract_path = shutil.which("tesseract")
        if not tesseract_path:
            raise RuntimeError("команда tesseract не найдена")
        language_result = subprocess.run(
            [tesseract_path, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        available_languages = {
            line.strip() for line in language_result.stdout.splitlines() if line.strip()
        }
        required_languages = {
            item for item in str(getattr(config, "RECEIPT_OCR_LANGUAGES", "rus+eng")).split("+") if item
        }
        missing_languages = sorted(required_languages - available_languages)
        ocr_ok = language_result.returncode == 0 and not missing_languages
        ocr_text = (
            "Tesseract доступен: " + ", ".join(sorted(required_languages))
            if ocr_ok
            else "Не найдены языки: " + ", ".join(missing_languages)
        )
    except Exception as error:
        ocr_ok, ocr_text = False, str(error)
    audit_result = service_audit.audit()
    update_info = update_manager.check_available_update(force=True)
    local_report = platform_diagnostics.build_report(
        snapshot=snapshot, db_path=config.DB_PATH, update_info=update_info
    )
    db_report = local_report["database"]
    storage_report = local_report["storage"]
    permissions_report = local_report["config_permissions"]
    update_report = local_report["updates"]
    publisher = update_manager.publisher_enabled()
    update_report = update_topology_public(update_report, publisher)
    traffic_report = local_report["traffic"]
    checks_data = [
        ("Telegram-бот", units["vpn-service-bot"] == "active", units["vpn-service-bot"]),
        ("Веб-панель", units["vpn-service-web"] == "active", units["vpn-service-web"]),
        ("3x-ui API", api_ok, api_text),
        ("Telegram API", tg_ok, tg_text),
        ("OCR чеков", ocr_ok, ocr_text),
        ("Таймер бэкапа", units["vpn-service-backup.timer"] == "active", units["vpn-service-backup.timer"]),
        ("Напоминания", units["vpn-service-reminders.timer"] == "active", units["vpn-service-reminders.timer"]),
        ("Дубликаты запуска", bool(audit_result.get("healthy")), "Не обнаружены" if audit_result.get("healthy") else "Обнаружены"),
        (
            "SQLite",
            bool(db_report.get("healthy")),
            f"quick_check={db_report.get('quick_check')}; journal={db_report.get('journal_mode')}",
        ),
        (
            "Свободное место",
            bool(storage_report.get("healthy")),
            f"Свободно {fmt_bytes(int(storage_report.get('free') or 0))} ({storage_report.get('free_percent')}%)",
        ),
        (
            "Права config.py",
            bool(permissions_report.get("healthy")),
            f"Режим {permissions_report.get('mode')}",
        ),
        (
            "Схема обновлений",
            bool(update_report.get("healthy")),
            "Главная панель" if update_report.get("role") == "publisher" else "Ведомая панель",
        ),
    ]
    checks = "".join(
        f'<div class="card metric"><div class="label">{html.escape(label)}</div><div class="value" style="font-size:20px">{status_badge(ok,"Работает","Ошибка")}</div><div class="hint">{html.escape(detail)}</div></div>'
        for label, ok, detail in checks_data
    )
    unit_rows = "".join(
        f'<tr><td>{html.escape(unit.get("name", ""))}</td><td>{html.escape(unit.get("active", ""))}</td><td>{html.escape(unit.get("enabled", ""))}</td><td>{"Да" if unit.get("canonical") else "Нет"}</td><td>{html.escape(unit.get("working_directory", ""))}</td></tr>'
        for unit in audit_result.get("units", [])
    )
    process_rows = "".join(
        f'<tr><td>{process.get("pid")}</td><td>{html.escape(process.get("command", ""))}</td></tr>'
        for process in audit_result.get("processes", [])
    )
    duplicate_details = html.escape(
        json.dumps(
            {
                "duplicate_units": audit_result.get("duplicate_units", []),
                "duplicate_processes": audit_result.get("duplicate_processes", {}),
                "cron_references": audit_result.get("cron_references", []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    identity_ok = (
        int(db_report.get("placeholder_tg_ids") or 0) == 0
        and int(db_report.get("missing_usernames") or 0) == 0
        and int(db_report.get("duplicate_emails") or 0) == 0
        and int(db_report.get("duplicate_uuids") or 0) == 0
    )
    update_errors = update_report.get("errors") or []
    update_warnings = update_report.get("warnings") or []
    update_error_html = (
        "<div class=\"notice\">" + html.escape("; ".join(map(str, update_errors))) + "</div>"
        if update_errors else ""
    )
    update_warning_html = (
        "<div class=\"notice\">" + html.escape("; ".join(map(str, update_warnings))) + "</div>"
        if update_warnings else ""
    )
    update_endpoint_html = "".join(
        f'<li><span class="code">{html.escape(str(endpoint))}</span></li>'
        for endpoint in update_report.get("candidate_endpoints", [])
    )
    if update_endpoint_html:
        update_endpoint_html = f'<p>Проверяемые API:</p><ul>{update_endpoint_html}</ul>'
    resolved_update_html = ""
    if update_report.get("metadata_url"):
        resolved_update_html = (
            '<p>Ответил API: <span class="code">'
            + html.escape(str(update_report.get("metadata_url")))
            + '</span></p>'
        )
    last_update = update_report.get("last_status") if isinstance(update_report.get("last_status"), dict) else {}
    last_update_html = (
        '<p>Последняя задача: <span class="code">'
        + html.escape(str(last_update.get("job_id") or "—"))
        + '</span> · состояние <b>'
        + html.escape(str(last_update.get("state") or "idle"))
        + '</b> · этап '
        + html.escape(str(last_update.get("phase") or "—"))
        + ' · прогресс '
        + html.escape(str(int(last_update.get("progress") or 0)))
        + '% · запуск '
        + html.escape(str(last_update.get("launcher") or "—"))
        + ' · обновлено '
        + html.escape(str(last_update.get("updated_at") or "—"))
        + '</p>'
    )
    traffic_note = (
        "Главный виджет платформы использует netTraffic из server/status — тот же тип счётчика, что главная страница 3x-ui. "
        "Inbound хранит суммарную историю входящих подключений, а список пользователей — дедуплицированную детализацию; "
        "поэтому эти три значения не обязаны совпадать."
        if traffic_report.get("server_counter_available")
        else
        "Эта версия 3x-ui не вернула server/status, поэтому главный виджет использует резервную сумму inbound. "
        "Пользовательская сумма дедуплицирована и предназначена только для детализации."
    )
    if publisher:
        update_topology_html = (
            '<p>Локальный пользователь панели: <span class="code">'
            + html.escape(str(update_report.get("username") or "—"))
            + '</span></p>'
            + update_endpoint_html + resolved_update_html + last_update_html
            + '<p class="muted">Источник обновлений: GitHub Releases.</p>'
        )
    else:
        update_topology_html = (
            '<p>Проверка канала обновлений выполнена через GitHub Releases.</p>'
            '<p class="muted">Репозиторий и release-URL скрыты для обычной панели.</p>'
        )
    body = f'''<header><div><h1>Диагностика</h1><div class="subtitle">Службы, база, Telegram-привязки, обновления и сверка трафика</div></div><div class="actions"><a class="button secondary" href="/api/diagnostics" target="_blank">JSON-отчёт</a><form method="post" action="/diagnostics/fix-duplicates"><button class="secondary">Удалить дубли запусков</button></form><form method="post" action="/diagnostics/auto-fix" onsubmit="return confirm('Найти и исправить типовые проблемы?')"><button class="secondary">🔧 Найти и исправить проблемы</button></form><form method="post" action="/service/all/restart"><button>Перезапустить всё</button></form></div></header>
<div class="grid">{checks}
<div class="card half"><div class="section-title"><h2>Telegram-привязки</h2>{status_badge(identity_ok, "Готово", "Нужна проверка")}</div><div class="numbers"><div><small>Всего</small><strong>{db_report.get('users', 0)}</strong></div><div><small>Положительный TG ID</small><strong>{db_report.get('positive_tg_ids', 0)}</strong></div><div><small>Временный ID</small><strong>{db_report.get('placeholder_tg_ids', 0)}</strong></div></div><p class="muted">Без ника: {db_report.get('missing_usernames', 0)} · без email: {db_report.get('missing_emails', 0)} · без UUID: {db_report.get('missing_uuids', 0)} · дубли email/UUID: {db_report.get('duplicate_emails', 0)}/{db_report.get('duplicate_uuids', 0)}</p><div class="actions"><a class="button small" href="/users/import-identities">Восстановить из старой БД</a><a class="button small secondary" href="/users">Проверить вручную</a></div></div>
<div class="card half"><div class="section-title"><h2>Защита и журнал</h2>{status_badge(True, "Включено", "Ошибка")}</div><div class="numbers"><div><small>Активные блокировки</small><strong>{db_report.get('blocked_logins', 0)}</strong></div><div><small>События пользователей</small><strong>{db_report.get('events', 0)}</strong></div><div><small>Размер БД</small><strong>{fmt_bytes(int(db_report.get('size') or 0))}</strong></div></div><p class="muted">Лимит применяется одновременно к паре IP + логин и ко всем попыткам с одного IP, поэтому смена вводимого логина не обходит блокировку. Состояние сохраняется после перезапуска.</p></div>
<div class="card full"><div class="section-title"><h2>Сверка трафика 3x-ui</h2>{status_badge(bool(traffic_report.get('healthy')), "Актуально", "Кэш/ошибка")}</div><div class="numbers"><div><small>Главная 3x-ui</small><strong>{fmt_bytes(int(traffic_report.get('server_used') or 0)) if traffic_report.get('server_counter_available') else 'Нет данных'}</strong></div><div><small>История inbound</small><strong>{fmt_bytes(int(traffic_report.get('inbound_used') or 0))}</strong></div><div><small>По пользователям</small><strong>{fmt_bytes(int(traffic_report.get('client_used') or 0))}</strong></div><div><small>Удалено повторов</small><strong>{traffic_report.get('duplicate_records_removed', 0)}</strong></div></div><p class="muted">Разница main↔users: {fmt_bytes(abs(int(traffic_report.get('dashboard_difference') or 0)))} · inbound↔users: {fmt_bytes(abs(int(traffic_report.get('difference') or 0)))} · пользователей: {traffic_report.get('clients', 0)}.</p><div class="notice">{html.escape(traffic_note)}</div></div>
<div class="card full"><div class="section-title"><h2>Обновления</h2><span class="badge {'good' if update_report.get('healthy') else 'bad'}">{html.escape('Издатель' if publisher else 'Ведомая панель')}</span></div>{update_error_html}{update_warning_html}{update_topology_html}</div><div class="card full table-wrap"><div class="section-title"><h2>Связанные systemd units</h2></div><table><thead><tr><th>Unit</th><th>Active</th><th>Enabled</th><th>Штатный</th><th>Рабочая папка</th></tr></thead><tbody>{unit_rows or '<tr><td colspan="5">systemd недоступен или units не найдены.</td></tr>'}</tbody></table></div>
<div class="card full table-wrap"><div class="section-title"><h2>Запущенные процессы проекта</h2></div><table><thead><tr><th>PID</th><th>Команда</th></tr></thead><tbody>{process_rows or '<tr><td colspan="2">Процессы не найдены.</td></tr>'}</tbody></table></div>
<div class="card full"><h2>Детали дубликатов</h2><pre>{duplicate_details}</pre></div><div class="card full"><h2>Сведения</h2><p>Версия: {html.escape(update_manager.current_version())}</p><p>Публичный вход: HTTPS 443</p><p>Backend: Unix socket</p><p>База: {html.escape(str(config.DB_PATH))}</p><p>3x-ui DB: {html.escape(str(config.XUI_DB_PATH))}</p><p><a href="/health">Проверить /health</a></p></div></div>'''
    return page(request, "Диагностика", body, "diagnostics")


@app.get("/api/diagnostics")
def diagnostics_api(request: Request):
    require_auth(request)
    try:
        snapshot = fetch_and_sync(force=True, db_path=config.DB_PATH)
    except Exception as error:
        snapshot = {"stale": True, "error": str(error), "clients": []}
    update_info = update_manager.check_available_update(force=True)
    publisher = update_manager.publisher_enabled()
    report = platform_diagnostics.build_report(snapshot=snapshot, db_path=config.DB_PATH, update_info=update_info)
    report["updates"] = update_topology_public(report.get("updates") or {}, publisher)
    return report


@app.post("/diagnostics/fix-duplicates")
def diagnostics_fix_duplicates(request: Request):
    require_auth(request)
    result = service_audit.fix()
    actions = result.get("actions", [])
    if actions:
        set_flash(request, "Устранено: " + "; ".join(map(str, actions)))
    elif result.get("after", {}).get("healthy"):
        set_flash(request, "Дублирующие службы и cron-задания не обнаружены")
    else:
        set_flash(request, "Часть дублей требует ручной проверки процессов", "bad")
    audit(str(request.session.get("user", "web")), "fix_duplicate_services", json.dumps(actions, ensure_ascii=False))
    return RedirectResponse(public_path("/diagnostics"), 303)


@app.post("/service/{target}/restart")
def restart_service(request: Request, target: str):
    require_auth(request)
    mapping = {
        "bot": ["vpn-service-bot"],
        "web": ["vpn-service-web"],
        "all": ["vpn-service-bot", "vpn-service-web", "vpn-service-backup.timer", "vpn-service-reminders.timer"],
    }
    units = mapping.get(target)
    if not units:
        raise HTTPException(404)
    audit(str(request.session.get("user", "web")), "restart_service", target)
    set_flash(request, "Службы перезапускаются")
    schedule_restart(units, delay=2)
    return RedirectResponse(public_path("/diagnostics" if target == "all" else "/"), 303)


@app.post("/sync")
def sync(request: Request):
    require_auth(request)
    try:
        snapshot = fetch_and_sync(force=True, db_path=config.DB_PATH)
        if snapshot.get("stale"):
            raise RuntimeError(str(snapshot.get("error") or "3x-ui API недоступна"))
        set_flash(request, f"Получены актуальные данные: {len(snapshot.get('clients', []))} пользователей")
    except Exception as error:
        set_flash(request, f"Синхронизация не выполнена: {error}", "bad")
    referer = request.headers.get("referer", "/")
    parsed = urlsplit(referer)
    destination = parsed.path or "/"
    destination = public_path(destination)
    if parsed.query:
        destination += f"?{parsed.query}"
    return RedirectResponse(destination, 303)


@app.get("/health", response_class=PlainTextResponse)
async def health():
    return f"OK {config.SERVICE_NAME} Web Panel v{update_manager.current_version()}"

