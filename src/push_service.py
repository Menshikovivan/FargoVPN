#!/usr/bin/env python3
"""Isolated RFC 8291 + VAPID Web Push sender for FargoVPN 4.0"""
from __future__ import annotations
import threading
import base64, hashlib, hmac, json, logging, os, secrets, sqlite3, time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import config
log=logging.getLogger(__name__)

def b64u(data: bytes)->str: return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')
def b64ud(value: str)->bytes: return base64.urlsafe_b64decode((value+'='*(-len(value)%4)).encode('ascii'))
def _pub(key)->bytes: return key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
def _path()->Path: return Path(str(getattr(config,'PUSH_VAPID_PRIVATE_KEY_PATH','/var/lib/vpn-service/vapid_private.pem')))
def _load(path:Path): return serialization.load_pem_private_key(path.read_bytes(),password=None)

def ensure_vapid_keys()->str:
    path=_path(); path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        key=ec.generate_private_key(ec.SECP256R1())
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    os.chmod(path,0o600)
    public=b64u(_pub(_load(path)))
    try:
        setattr(config,'PUSH_VAPID_PUBLIC_KEY',public)
        from webapp import CONFIG_PATH, replace_assignment
        text=CONFIG_PATH.read_text(encoding='utf-8'); text=replace_assignment(text,'PUSH_VAPID_PUBLIC_KEY',public)
        tmp=CONFIG_PATH.with_suffix('.push.tmp'); tmp.write_text(text,encoding='utf-8'); os.chmod(tmp,0o600); tmp.replace(CONFIG_PATH)
    except Exception as exc: log.warning('Не удалось сохранить публичный VAPID-ключ: %s',exc)
    return public

def public_key()->str:
    value=str(getattr(config,'PUSH_VAPID_PUBLIC_KEY','') or '').strip()
    return value or ensure_vapid_keys()

def _subject()->str:
    value=str(getattr(config,'PUSH_VAPID_SUBJECT','') or '').strip()
    if value: return value
    domain=str(getattr(config,'WEB_DOMAIN','') or '').strip()
    if '://' in domain: return domain.rstrip('/')
    return ('https://'+domain) if domain else 'https://localhost'

def _jwt(key,aud:str)->str:
    head=b64u(b'{"typ":"JWT","alg":"ES256"}')
    exp=int(time.time())+min(86400,max(300,int(getattr(config,'PUSH_TTL_SECONDS',3600))))
    body=b64u(json.dumps({'aud':aud,'exp':exp,'sub':_subject()},separators=(',',':')).encode())
    signing=head+'.'+body
    der=key.sign(signing.encode(),ec.ECDSA(hashes.SHA256())); r,s=decode_dss_signature(der)
    return signing+'.'+b64u(r.to_bytes(32,'big')+s.to_bytes(32,'big'))

def _origin(endpoint:str)->str:
    u=urlsplit(endpoint)
    if u.scheme not in ('https','http') or not u.netloc: raise ValueError('Некорректный push endpoint')
    return f'{u.scheme}://{u.netloc}'

def _encrypt(sub:dict,payload:bytes)->bytes:
    ua=b64ud(str(sub['keys']['p256dh'])); auth=b64ud(str(sub['keys']['auth']))
    if len(ua)!=65: raise ValueError('p256dh должен быть P-256 uncompressed point')
    ua_key=ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),ua)
    as_key=ec.generate_private_key(ec.SECP256R1()); as_pub=_pub(as_key)
    shared=as_key.exchange(ec.ECDH(),ua_key)
    prk_key=hmac.new(auth,shared,hashlib.sha256).digest()
    info=b'WebPush: info\0'+ua+as_pub
    key_info_prk=hmac.new(prk_key,info+b'\x01',hashlib.sha256).digest()
    salt=secrets.token_bytes(16)
    prk=hmac.new(salt,key_info_prk,hashlib.sha256).digest()
    # For aes128gcm the content-encoding info is expanded from PRK using one HMAC block.
    cek=hmac.new(prk,b'Content-Encoding: aes128gcm\0\x01',hashlib.sha256).digest()[:16]
    nonce=hmac.new(prk,b'Content-Encoding: nonce\0\x01',hashlib.sha256).digest()[:12]
    ciphertext=AESGCM(cek).encrypt(nonce,payload+b'\x02',None)
    return salt+(4096).to_bytes(4,'big')+bytes([65])+as_pub+ciphertext


def migrate_tables(db_path: str) -> None:
    """Create the administrative Push subscription and diagnostic log tables."""
    with sqlite3.connect(db_path, timeout=30) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS panel_push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            user_agent TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_success_at TEXT,
            last_error TEXT,
            enabled INTEGER NOT NULL DEFAULT 1
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_panel_push_username ON panel_push_subscriptions(username)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_panel_push_enabled ON panel_push_subscriptions(enabled)")
        c.execute("""CREATE TABLE IF NOT EXISTS panel_push_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            level TEXT NOT NULL DEFAULT 'INFO',
            event TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_panel_push_logs_created ON panel_push_logs(created_at DESC)")
        c.commit()

def _log(db_path: str, username: str, event: str, message: str, level: str = 'INFO') -> None:
    try:
        migrate_tables(db_path)
        with sqlite3.connect(db_path, timeout=30) as c:
            c.execute("INSERT INTO panel_push_logs(username,level,event,message) VALUES(?,?,?,?)", (str(username or '')[:100], str(level or 'INFO')[:16], str(event or '')[:80], str(message or '')[:2000]))
            c.execute("DELETE FROM panel_push_logs WHERE id NOT IN (SELECT id FROM panel_push_logs ORDER BY id DESC LIMIT 500)")
            c.commit()
    except Exception:
        log.exception('Не удалось записать Push log')

def panel_logs(db_path: str, limit: int = 80, username: str = "") -> list[dict]:
    """Return recent administrative Push diagnostics without leaking secrets.

    Results are returned oldest-to-newest so the browser can keep the newest entry
    visible at the bottom of the log window.  An optional username filters entries
    to the current administrative account.
    """
    migrate_tables(db_path)
    safe_limit = max(10, min(int(limit or 80), 200))
    username = str(username or "").strip()
    with sqlite3.connect(db_path, timeout=30) as c:
        c.row_factory = sqlite3.Row
        if username:
            rows = c.execute(
                "SELECT created_at,level,event,message FROM panel_push_logs WHERE username IN (?, '') ORDER BY id DESC LIMIT ?",
                (username, safe_limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT created_at,level,event,message FROM panel_push_logs ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
    result = [dict(r) for r in rows]
    result.reverse()
    return result
    return result



def panel_subscribe(db_path: str, username: str, subscription: dict, user_agent: str = "") -> None:
    username = str(username or "").strip()
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys") or {}
    if not username:
        raise ValueError("Не указана учётная запись панели")
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("Некорректная PushSubscription")
    migrate_tables(db_path)
    with sqlite3.connect(db_path, timeout=30) as c:
        c.execute("""INSERT INTO panel_push_subscriptions
            (username, endpoint, p256dh, auth, user_agent, updated_at, last_error, enabled)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL, 1)
            ON CONFLICT(endpoint) DO UPDATE SET
              username=excluded.username, p256dh=excluded.p256dh, auth=excluded.auth,
              user_agent=excluded.user_agent, updated_at=CURRENT_TIMESTAMP,
              last_error=NULL, enabled=1""",
            (username, endpoint, str(keys["p256dh"]), str(keys["auth"]), str(user_agent)[:500]))
        # Limit to the newest subscriptions for one admin account.
        rows = c.execute(
            "SELECT id FROM panel_push_subscriptions WHERE username=? AND enabled=1 ORDER BY updated_at DESC, id DESC",
            (username,),
        ).fetchall()
        for row in rows[8:]:
            c.execute("DELETE FROM panel_push_subscriptions WHERE id=?", (row[0],))
        c.commit()
    try:
        endpoint_host = urlsplit(endpoint).netloc[:120]
    except Exception:
        endpoint_host = "unknown"
    _log(db_path, username, 'subscribe', f'Push-подписка зарегистрирована; provider={endpoint_host}', 'INFO')


def panel_unsubscribe(db_path: str, username: str, endpoint: str) -> None:
    migrate_tables(db_path)
    _log(db_path, username, 'unsubscribe', f'Удаление Push-подписки endpoint={str(endpoint or '')[:80]}…')
    with sqlite3.connect(db_path, timeout=30) as c:
        c.execute(
            "DELETE FROM panel_push_subscriptions WHERE username=? AND endpoint=?",
            (str(username or "").strip(), str(endpoint or "").strip()),
        )
        c.commit()



def active_subscription_count(db_path: str, username: str) -> int:
    migrate_tables(db_path)
    with sqlite3.connect(db_path, timeout=30) as c:
        row = c.execute(
            "SELECT COUNT(*) FROM panel_push_subscriptions WHERE username=? AND enabled=1",
            (str(username or "").strip(),),
        ).fetchone()
    return int(row[0] or 0) if row else 0

def panel_status(db_path: str, username: str) -> dict:
    username = str(username or "").strip()
    db_ok = True
    try:
        migrate_tables(db_path)
    except Exception as exc:
        db_ok = False
        _log(db_path, username, 'status', f'База Push недоступна: {exc}', 'ERROR') if db_path else None
    vapid_ok = False
    private_exists = _path().exists()
    try:
        key = public_key()
        raw = b64ud(key)
        vapid_ok = len(raw) == 65
    except Exception as exc:
        _log(db_path, username, 'vapid', f'Ошибка VAPID: {exc}', 'ERROR')
    count = 0; last_error = ''
    if db_ok:
        with sqlite3.connect(db_path, timeout=30) as c:
            count = int(c.execute("SELECT COUNT(*) FROM panel_push_subscriptions WHERE username=? AND enabled=1", (username,)).fetchone()[0])
            last_error_row = c.execute("SELECT last_error FROM panel_push_subscriptions WHERE username=? AND enabled=1 AND last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1", (username,)).fetchone()
            last_error = str(last_error_row[0]) if last_error_row else ''
    _log(db_path, username, 'status', f'Проверка Push: ok={bool(db_ok and vapid_ok)}, subscriptions={count}, vapid={vapid_ok}, db={db_ok}', 'INFO')
    return {"ok": bool(db_ok and vapid_ok), "enabled": bool(count), "count": count, "supported": True, "vapid_configured": vapid_ok, "private_key_exists": private_exists, "db_ok": db_ok, "last_error": last_error}


def _send_push(endpoint: str, p256dh: str, auth: str, payload: bytes, urgency: str) -> tuple[bool, int, str]:
    sub = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}
    try:
        encrypted = _encrypt(sub, payload)
        key = _load(_path())
        token = _jwt(key, _origin(endpoint))
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Encoding": "aes128gcm",
            "TTL": str(int(getattr(config, "PUSH_TTL_SECONDS", 3600))),
            "Urgency": str(urgency or "normal"),
            "Authorization": f"vapid t={token}, k={b64u(_pub(key))}",
        }
        response = httpx.post(endpoint, content=encrypted, headers=headers, timeout=15, verify=True, trust_env=False)
        return response.is_success, response.status_code, (response.text or "")[:500]
    except Exception as exc:
        return False, 0, str(exc)[:500]


def notify_panel(db_path: str, username: str, title: str, body: str, url: str = "/", tag: str = "fargovpn", urgency: str = "normal") -> dict:
    """Send a browser/PWA push only to the authenticated admin panel account."""
    push_id = secrets.token_hex(8)
    started = time.monotonic()
    log.info("performance operation=push_send_start push_id=%s username=%s t0_unix_ms=%s", push_id, str(username or "")[:100], int(time.time()*1000))
    migrate_tables(db_path)
    username = str(username or "").strip()
    result = {"sent": 0, "removed": 0, "failed": 0}
    message = {
        "title": str(title)[:120],
        "body": str(body)[:500],
        "url": str(url or "/")[:1000],
        "tag": str(tag)[:100],
        "urgency": str(urgency or "normal"),
        "server_sent_at_ms": int(time.time() * 1000),
    }
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with sqlite3.connect(db_path, timeout=30) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM panel_push_subscriptions WHERE username=? AND enabled=1",
            (username,),
        ).fetchall()
    for row in rows:
        endpoint_started = time.monotonic()
        ok, code, detail = _send_push(str(row["endpoint"]), str(row["p256dh"]), str(row["auth"]), payload, str(urgency or "normal"))
        log.info("performance operation=push_provider_response push_id=%s subscription_id=%s duration_ms=%s status=%s success=%s t2_unix_ms=%s", push_id, int(row["id"]), int((time.monotonic()-endpoint_started)*1000), code, bool(ok), int(time.time()*1000))
        with sqlite3.connect(db_path, timeout=30) as c:
            if ok:
                c.execute("UPDATE panel_push_subscriptions SET last_success_at=CURRENT_TIMESTAMP,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
                result["sent"] += 1
                _log(db_path, username, 'delivery', f'Push-доставка успешна; HTTP {code}', 'INFO')
            elif code in (404, 410):
                c.execute("DELETE FROM panel_push_subscriptions WHERE id=?", (row["id"],))
                result["removed"] += 1
            else:
                c.execute("UPDATE panel_push_subscriptions SET last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (detail, row["id"]))
                _log(db_path, username, 'delivery', f'Ошибка доставки HTTP {code}: {detail}', 'ERROR')
                result["failed"] += 1
            c.commit()
    log.info("performance operation=push_send_end push_id=%s duration_ms=%s sent=%s failed=%s removed=%s t2_unix_ms=%s", push_id, int((time.monotonic()-started)*1000), result["sent"], result["failed"], result["removed"], int(time.time()*1000))
    return result

# FARGOVPN_PUSH_SQLITE_DEADLOCK_HOTFIX_V2
#
# Fixes the nested SQLite connection deadlock in push_service.
#
# notify_panel() can hold a write transaction while _log() used to open
# another SQLite connection. The second connection could wait for the
# first connection forever until SQLite timeout expired.
#
# While notify_panel() is executing, _log() calls are collected and executed
# only after notify_panel() has returned and the outer SQLite transaction
# has been closed/committed.

_FARGOVPN_PUSH_HOTFIX_CTX = threading.local()

_FARGOVPN_PUSH_ORIGINAL_LOG = _log
_FARGOVPN_PUSH_ORIGINAL_NOTIFY_PANEL = notify_panel


def _fargovpn_push_hotfix_log(*args, **kwargs):
    ctx = _FARGOVPN_PUSH_HOTFIX_CTX

    if getattr(ctx, "active", False):
        queue = getattr(ctx, "queue", None)

        if queue is None:
            queue = []
            ctx.queue = queue

        queue.append((args, kwargs))
        return None

    return _FARGOVPN_PUSH_ORIGINAL_LOG(*args, **kwargs)


def _fargovpn_push_hotfix_notify_panel(*args, **kwargs):
    ctx = _FARGOVPN_PUSH_HOTFIX_CTX

    previous_active = getattr(ctx, "active", False)
    previous_queue = getattr(ctx, "queue", None)

    ctx.active = True
    ctx.queue = []

    try:
        return _FARGOVPN_PUSH_ORIGINAL_NOTIFY_PANEL(*args, **kwargs)

    finally:
        queued = list(getattr(ctx, "queue", []) or [])

        ctx.active = previous_active
        ctx.queue = previous_queue

        # notify_panel() has already returned here.
        # Its SQLite connection is therefore no longer holding the write lock.
        for log_args, log_kwargs in queued:
            try:
                _FARGOVPN_PUSH_ORIGINAL_LOG(*log_args, **log_kwargs)
            except Exception:
                pass


_log = _fargovpn_push_hotfix_log
notify_panel = _fargovpn_push_hotfix_notify_panel
