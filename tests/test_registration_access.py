from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

# The public archive intentionally ships config.example.py, not production
# config.py. Tests provide the minimal runtime config surface they need.
if "config" not in sys.modules:
    cfg = types.ModuleType("config")
    cfg.DB_PATH = ""
    cfg.REGISTRATION_MAX_ATTEMPTS = 5
    cfg.REGISTRATION_ATTEMPT_WINDOW_SECONDS = 600
    cfg.REGISTRATION_BLOCK_SECONDS = 900
    cfg.ADMIN_IDS = []
    sys.modules["config"] = cfg

import registration_access
from db import CompatRow


@pytest.fixture
def sqlite_backend(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    keeper = sqlite3.connect(db_path)
    keeper.executescript(
        """
        CREATE TABLE users(
            tg_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, uuid TEXT, email TEXT,
            expiry_time INTEGER DEFAULT 0, enable INTEGER DEFAULT 1, up INTEGER DEFAULT 0, down INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0, sub_id TEXT, last_reminder_days INTEGER DEFAULT -1, referral_code TEXT,
            referred_by_tg_id INTEGER, referred_by_code TEXT, registered_at TEXT,
            registration_status TEXT DEFAULT 'awaiting_invite', registration_attempts INTEGER DEFAULT 0,
            registration_attempts_reset_at INTEGER DEFAULT 0, registration_blocked_until INTEGER DEFAULT 0
        );
        CREATE TABLE pending_registrations(tg_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE audit_log(actor TEXT, action TEXT, details TEXT);
        CREATE UNIQUE INDEX uq_users_referral_code ON users(referral_code) WHERE referral_code IS NOT NULL AND referral_code<>'';
        """
    )
    keeper.commit(); keeper.close()

    @contextmanager
    def fake_connect():
        conn = sqlite3.connect(db_path, timeout=20, isolation_level='DEFERRED', check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(registration_access, "_connect", fake_connect)
    monkeypatch.setattr(registration_access.config, "DB_PATH", str(db_path), raising=False)
    monkeypatch.setattr(registration_access.config, "REGISTRATION_MAX_ATTEMPTS", 5, raising=False)
    monkeypatch.setattr(registration_access.config, "REGISTRATION_ATTEMPT_WINDOW_SECONDS", 600, raising=False)
    monkeypatch.setattr(registration_access.config, "REGISTRATION_BLOCK_SECONDS", 900, raising=False)
    return db_path


def seed_owner(db_path: Path, tg_id: int = 100, code: str = "1234"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO users(tg_id,username,display_name,referral_code,registration_status) VALUES(?,?,?,?, 'active')",
        (tg_id, "owner", "Owner", code),
    )
    conn.commit(); conn.close()


def test_first_start_creates_awaiting_invite(sqlite_backend):
    state = registration_access.ensure_user_registration_state_sync(555, "new_user", "New User")
    assert state.status == registration_access.AWAITING_INVITE
    assert state.created is True

    again = registration_access.registration_state_sync(555)
    assert again.status == registration_access.AWAITING_INVITE


def test_restart_between_start_and_code_does_not_lose_registration_state(sqlite_backend):
    registration_access.ensure_user_registration_state_sync(555, "new_user", "New User")
    seed_owner(sqlite_backend)
    # A process restart is simulated by calling only DB-backed helpers. No FSM
    # state exists anywhere in this flow.
    result = registration_access.register_with_invitation_sync(555, "new_user", "1234", "New User")
    assert result.registered
    assert registration_access.registration_state_sync(555).status == registration_access.ACTIVE


def test_postgresql_compatible_owner_row_and_text_timestamp(sqlite_backend, monkeypatch):
    seed_owner(sqlite_backend)
    registration_access.ensure_user_registration_state_sync(555, "new_user", "New User")
    original_connect = registration_access._connect
    queries = []

    class Result:
        def __init__(self, cursor):
            self.cursor = cursor
            self.rowcount = cursor.rowcount

        def fetchone(self):
            row = self.cursor.fetchone()
            if row is None:
                return None
            class PgRow:
                _mapping = {key: row[key] for key in row.keys()}
                def __getitem__(self, index):
                    return row[index]
                def __iter__(self):
                    return iter(row)
                def __len__(self):
                    return len(row)
            return CompatRow(PgRow())

    class Connection:
        def __init__(self, raw):
            self.raw = raw
        def execute(self, sql, params=()):
            queries.append(sql)
            return Result(self.raw.execute(sql, params))

    @contextmanager
    def pg_compatible_connect():
        with original_connect() as raw:
            yield Connection(raw)

    monkeypatch.setattr(registration_access, "_connect", pg_compatible_connect)
    assert registration_access.lookup_active_referral_owner_sync("1234")["tg_id"] == 100
    result = registration_access.register_with_invitation_sync(555, "new_user", "1234", "New User")
    assert result.registered
    assert result.owner["tg_id"] == 100
    assert any("COALESCE(registered_at,CAST(CURRENT_TIMESTAMP AS TEXT))" in sql for sql in queries)


def test_double_start_race_creates_one_row(sqlite_backend):
    # Do not globally patch the selector because two SQLite connections would
    # both wait after INSERT. Exercise the public helper concurrently instead.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(registration_access.ensure_user_registration_state_sync, 777, "race", "Race") for _ in range(2)]
        results = [f.result() for f in futures]
    assert all(r.status == registration_access.AWAITING_INVITE for r in results)
    conn = sqlite3.connect(sqlite_backend)
    assert conn.execute("SELECT count(*) FROM users WHERE tg_id=777").fetchone()[0] == 1
    conn.close()


def test_invalid_code_is_not_silently_normalized(sqlite_backend):
    assert registration_access.normalize_invitation_code("1234") == "1234"
    assert registration_access.normalize_invitation_code(" 1234 ") == "1234"
    assert registration_access.normalize_invitation_code("12a34") is None
    assert registration_access.normalize_invitation_code("12345") is None
    assert registration_access.normalize_invitation_code("/start 1234") is None


def test_wrong_code_attempts_lock_the_user(sqlite_backend):
    registration_access.ensure_user_registration_state_sync(555, "new", "New")
    for _ in range(4):
        result = registration_access.record_failed_invitation_attempt_sync(555)
        assert result.status == "invalid_code"
    result = registration_access.record_failed_invitation_attempt_sync(555)
    assert result.status == "blocked"
    assert result.attempt_count == 5
    assert result.blocked_until > 0
    state = registration_access.registration_state_sync(555)
    assert state.blocked_until == result.blocked_until


def test_same_invite_cannot_activate_same_user_twice(sqlite_backend):
    registration_access.ensure_user_registration_state_sync(555, "new", "New")
    seed_owner(sqlite_backend)
    first = registration_access.register_with_invitation_sync(555, "new", "1234", "New")
    second = registration_access.register_with_invitation_sync(555, "new", "1234", "New")
    assert first.registered
    assert second.status == "already_active"


def test_same_personal_referral_code_remains_multi_use(sqlite_backend):
    # FargoVPN's existing referral contract is a reusable personal 4-digit
    # code: different invited users may legitimately use the same owner's code.
    # Atomicity prevents the same Telegram account from being activated twice.
    seed_owner(sqlite_backend)
    for tg_id in (501, 502):
        registration_access.ensure_user_registration_state_sync(tg_id, f"u{tg_id}", "User")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(registration_access.register_with_invitation_sync, tg_id, f"u{tg_id}", "1234", "User") for tg_id in (501, 502)]
        results = [f.result() for f in futures]
    assert all(r.registered for r in results)


def test_pending_owner_code_is_not_valid(sqlite_backend):
    sqlite_backend  # fixture name retained for clarity
    conn = sqlite3.connect(sqlite_backend)
    conn.execute("INSERT INTO users(tg_id,username,referral_code,registration_status) VALUES(100,'owner','1234','awaiting_invite')")
    conn.execute("INSERT INTO users(tg_id,registration_status) VALUES(101,'awaiting_invite')")
    conn.commit(); conn.close()
    result = registration_access.register_with_invitation_sync(101, "new", "1234", "New")
    assert result.status == "invalid_code"


def test_banned_user_can_never_activate(sqlite_backend):
    conn = sqlite3.connect(sqlite_backend)
    conn.execute("INSERT INTO users(tg_id,registration_status) VALUES(900,'banned')")
    conn.commit(); conn.close()
    seed_owner(sqlite_backend)
    result = registration_access.register_with_invitation_sync(900, "bad", "1234", "Bad")
    assert result.status == registration_access.BANNED


@pytest.mark.asyncio
async def test_prompt_retries_after_transient_send_failure(monkeypatch):
    # Install tiny aiogram stubs only for this focused middleware test.
    aiogram = types.ModuleType("aiogram")
    exceptions = types.ModuleType("aiogram.exceptions")
    filters = types.ModuleType("aiogram.filters")
    class BaseMiddleware: pass
    class BaseFilter: pass
    class TelegramBadRequest(Exception): pass
    class TelegramForbiddenError(Exception): pass
    class TelegramRetryAfter(Exception):
        retry_after = 0.01
    aiogram.BaseMiddleware = BaseMiddleware
    exceptions.TelegramBadRequest = TelegramBadRequest
    exceptions.TelegramForbiddenError = TelegramForbiddenError
    exceptions.TelegramRetryAfter = TelegramRetryAfter
    filters.BaseFilter = BaseFilter
    monkeypatch.setitem(sys.modules, "aiogram", aiogram)
    monkeypatch.setitem(sys.modules, "aiogram.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "aiogram.filters", filters)

    import importlib
    middleware = importlib.import_module("telegram_registration_middleware")
    class Bot:
        def __init__(self): self.calls = 0
        async def send_message(self, chat_id, text, parse_mode=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")
    bot = Bot()
    assert await middleware.send_registration_prompt(bot, 123) is True
    assert bot.calls == 2


def test_global_gate_blocks_deep_link_and_callbacks(monkeypatch):
    import telegram_registration_middleware as middleware
    class User:
        id = 777; username = "new"; full_name = "New"
    class Chat:
        id = 777; type = "private"
    class Message:
        def __init__(self, text): self.text=text; self.from_user=User(); self.chat=Chat()
    class CallbackQuery:
        def __init__(self): self.from_user=User(); self.message=Message("old"); self.id="q"
        async def answer(self): pass
    class Update:
        def __init__(self, source): self.message=source if isinstance(source,Message) else None; self.callback_query=source if isinstance(source,CallbackQuery) else None; self.inline_query=None
    monkeypatch.setattr(middleware.registration_access, "ensure_user_registration_state_sync", lambda *a: middleware.registration_access.RegistrationState(777, middleware.registration_access.AWAITING_INVITE))
    class Bot:
        def __init__(self): self.calls=[]
        async def send_message(self,*args,**kwargs): self.calls.append((args,kwargs))
    bot=Bot()
    gate=middleware.InvitationAccessMiddleware()
    async def handler(event,data): data["handled"]=True; return "handled"
    import asyncio
    data={"bot":bot}
    result=asyncio.run(gate(handler, Update(Message("/start renew")), data))
    assert result is None and "handled" not in data and bot.calls
    data={"bot":bot}
    result=asyncio.run(gate(handler, Update(CallbackQuery()), data))
    assert result is None and bot.calls


def test_global_gate_allows_only_plain_four_digit_message(monkeypatch):
    import telegram_registration_middleware as middleware
    class User: id=778; username="new"; full_name="New"
    class Chat: id=778; type="private"
    class Message:
        def __init__(self,text): self.text=text; self.from_user=User(); self.chat=Chat()
    class Update:
        def __init__(self,msg): self.message=msg
    monkeypatch.setattr(middleware.registration_access, "ensure_user_registration_state_sync", lambda *a: middleware.registration_access.RegistrationState(778, middleware.registration_access.AWAITING_INVITE))
    class Bot:
        async def send_message(self,*args,**kwargs): pass
    async def handler(event,data): return "handled"
    import asyncio
    result=asyncio.run(middleware.InvitationAccessMiddleware()(handler, Update(Message("1234")), {"bot":Bot()}))
    assert result=="handled"
    result=asyncio.run(middleware.InvitationAccessMiddleware()(handler, Update(Message("12a34")), {"bot":Bot()}))
    assert result is None
