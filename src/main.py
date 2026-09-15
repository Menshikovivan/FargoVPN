# -*- coding: utf-8 -*-
import sys
import re
import logging
import asyncio
import html
import hashlib
import sqlite3
import time
import json
from pathlib import Path
from urllib.parse import quote
from datetime import datetime
from time_utils import from_timestamp as panel_from_timestamp, local_date_from_timestamp, now_local
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    UsersShared,
    MenuButtonDefault,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

import config as config
import cabinet_service
import referral_codes
import referral_rewards
import user_events
import push_service
from init_db import migrate as migrate_database
from services.xui_api import (
    bytes_to_gb,
    change_client_days_sync,
    delete_client_sync,
    fetch_and_sync,
    fetch_snapshot_sync,
    inbound_ids_sync,
    current_subscription_url_sync,
)
from services.subscriptions import (
    approve_payment_sync,
    decline_payment_sync,
    ensure_subscription_sync,
    recover_user_identity_sync,
    bind_database_user_tg_id_sync,
    ensure_telegram_user_shell_sync,
)
from services.media import download_telegram_file
from services.telegram_events import (
    incoming_message_event_payload,
    safe_int,
    telegram_message_media,
)
from services.receipt_ocr import ReceiptAnalysis, analyze_payment_receipt
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

INCY_APP_URL = str(
    getattr(config, "FAQ_INCY_URL", "https://apps.apple.com/ru/app/incy/id6756943388")
).strip() or "https://apps.apple.com/ru/app/incy/id6756943388"
IDENTITY_REFRESH_SECONDS = max(60, int(getattr(config, "BOT_IDENTITY_REFRESH_SECONDS", 600)))
UNREAD_NOTIFICATION_STATE_PATH = str(getattr(
    config, "UNREAD_ADMIN_NOTIFICATION_STATE_PATH", "/var/lib/vpn-service/unread-admin-notification.json"
))


def _read_unread_notification_state() -> dict[str, Any]:
    path = Path(UNREAD_NOTIFICATION_STATE_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _unread_notification_due(fingerprint: str = "") -> bool:
    if not bool(getattr(config, "UNREAD_ADMIN_NOTIFICATION_ENABLED", True)):
        return False
    interval = max(60, int(getattr(config, "UNREAD_ADMIN_NOTIFICATION_INTERVAL_SECONDS", 60)))
    data = _read_unread_notification_state()
    last = float(data.get("last_sent_at") or 0)
    # Never spam the same unread state. A notification is emitted again only
    # after the state changed (for example, a new incoming message) and the
    # configured cooldown has elapsed. This prevents a persistent unread chat
    # from generating one Telegram message every minute forever.
    previous_fingerprint = str(data.get("fingerprint") or "")
    if fingerprint and fingerprint == previous_fingerprint:
        return False
    return (time.time() - last) >= interval


def _clear_unread_notification_fingerprint() -> None:
    """Forget which unread state was last announced without restarting the cooldown."""
    path = Path(UNREAD_NOTIFICATION_STATE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = _read_unread_notification_state()
        payload = dict(previous)
        payload["fingerprint"] = ""
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except Exception as error:
        logger.warning("Не удалось сбросить состояние непрочитанных уведомлений: %s", error)


def _mark_unread_notification_sent(fingerprint: str = "") -> None:
    path = Path(UNREAD_NOTIFICATION_STATE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = _read_unread_notification_state()
        payload = {"last_sent_at": time.time(), "fingerprint": str(fingerprint or "")}
        if previous.get("version"):
            payload["version"] = previous["version"]
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except Exception as error:
        logger.warning("Не удалось сохранить состояние уведомлений о непрочитанных: %s", error)



async def safe_callback_answer(call: CallbackQuery, *args: Any, **kwargs: Any) -> bool:
    try:
        await call.answer(*args, **kwargs)
        return True
    except TelegramBadRequest as error:
        if "query is too old" in str(error).lower() or "query id is invalid" in str(error).lower():
            logger.debug("Просроченный callback query %s проигнорирован: %s", getattr(call, "id", ""), error)
            return False
        raise
    except Exception as error:
        logger.debug("Не удалось ответить на callback %s: %s", getattr(call, "id", ""), error)
        return False
SUBSCRIPTION_DAYS = max(1, min(3650, int(getattr(config, "SUBSCRIPTION_DAYS", 30))))
_IDENTITY_REFRESHED_AT: dict[int, float] = {}

# Telegram keeps ReplyKeyboardMarkup on the client until the bot explicitly
# sends ReplyKeyboardRemove. Older FargoVPN releases used a persistent reply
# keyboard, therefore simply switching the code to inline keyboards is not
# enough: users can continue seeing the old fixed panel indefinitely.
_LEGACY_REPLY_KEYBOARD_CLEARED: set[int] = set()
_LEGACY_REPLY_KEYBOARD_IN_FLIGHT: set[int] = set()
_LEGACY_REPLY_KEYBOARD_DELETE_TASKS: set[asyncio.Task[Any]] = set()


def _event_chat_id(event: Any) -> int:
    """Return the chat where a legacy reply keyboard must be removed."""
    message = event if isinstance(event, Message) else getattr(event, "message", None)
    chat = getattr(message, "chat", None)
    try:
        chat_id = int(getattr(chat, "id", 0) or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if chat_id:
        return chat_id
    from_user = getattr(event, "from_user", None)
    try:
        return int(getattr(from_user, "id", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def _delete_legacy_keyboard_notice_later(
    bot_instance: Bot, chat_id: int, message_id: int
) -> None:
    """Keep the removal update long enough for Telegram clients, then tidy it."""
    await asyncio.sleep(1.0)
    try:
        await bot_instance.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as error:
        # Removal has already been delivered. A failed cosmetic delete must not
        # make the old keyboard return or interrupt the user's action.
        logger.debug(
            "Не удалось удалить служебное сообщение очистки клавиатуры в чате %s: %s",
            chat_id,
            error,
        )


async def ensure_legacy_reply_keyboard_removed(
    bot_instance: Bot, chat_id: int
) -> bool:
    """Hide the fixed keyboard left by old versions, once per chat/process.

    The cleanup message is sent through the base Bot implementation so it is
    not written to the web-panel conversation journal. Inline keyboards are
    unaffected and remain the only visible navigation after cleanup.
    """
    try:
        normalized_chat_id = int(chat_id)
    except (TypeError, ValueError):
        return False
    if not normalized_chat_id:
        return False
    if (
        normalized_chat_id in _LEGACY_REPLY_KEYBOARD_CLEARED
        or normalized_chat_id in _LEGACY_REPLY_KEYBOARD_IN_FLIGHT
    ):
        return False

    _LEGACY_REPLY_KEYBOARD_IN_FLIGHT.add(normalized_chat_id)
    try:
        cleanup_message = await Bot.send_message(
            bot_instance,
            chat_id=normalized_chat_id,
            text="✅ Меню обновлено",
            reply_markup=ReplyKeyboardRemove(remove_keyboard=True),
            disable_notification=True,
        )
        _LEGACY_REPLY_KEYBOARD_CLEARED.add(normalized_chat_id)
        task = asyncio.create_task(
            _delete_legacy_keyboard_notice_later(
                bot_instance, normalized_chat_id, int(cleanup_message.message_id)
            )
        )
        _LEGACY_REPLY_KEYBOARD_DELETE_TASKS.add(task)
        task.add_done_callback(_LEGACY_REPLY_KEYBOARD_DELETE_TASKS.discard)
        logger.info(
            "Старая фиксированная Telegram-клавиатура скрыта для чата %s",
            normalized_chat_id,
        )
        return True
    except Exception as error:
        # Do not block the requested bot action. The next interaction will retry.
        logger.warning(
            "Не удалось скрыть старую Telegram-клавиатуру в чате %s: %s",
            normalized_chat_id,
            error,
        )
        return False
    finally:
        _LEGACY_REPLY_KEYBOARD_IN_FLIGHT.discard(normalized_chat_id)


def incoming_event_payload(event: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(event, CallbackQuery):
        message = getattr(event, "message", None)
        metadata = {
            "telegram_message_id": safe_int(getattr(message, "message_id", 0)),
            "callback_data": str(getattr(event, "data", "") or "")[:500],
        }
        return "telegram_callback", f"Нажата кнопка: {str(event.data or '')}", metadata
    return incoming_message_event_payload(event)


class JournalBot(Bot):
    """Record outgoing messages in the shared web-panel conversation history."""

    @staticmethod
    def _target_id(chat_id: int | str) -> int:
        try:
            value = int(chat_id)
            return value if value != 0 else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _media_text(label: str, kwargs: dict[str, Any]) -> str:
        caption = str(kwargs.get("caption") or "").strip()
        return f"[{label}]" + (f"\n{caption}" if caption else "")

    @staticmethod
    async def _record_outgoing(
        target_id: int,
        text: str,
        *,
        event_type: str,
        success: bool = True,
        error: Exception | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not target_id:
            return
        payload = dict(metadata or {})
        if error:
            payload["error"] = str(error)[:800]
        await asyncio.to_thread(
            user_events.safe_record_event,
            target_id,
            direction="out",
            event_type=event_type,
            text=text,
            actor="telegram_bot",
            success=success,
            metadata=payload or None,
            db_path=config.DB_PATH,
        )

    async def send_message(self, chat_id: int | str, text: str, *args: Any, **kwargs: Any):
        target_id = self._target_id(chat_id)
        if target_id:
            await ensure_legacy_reply_keyboard_removed(self, target_id)
        try:
            result = await super().send_message(chat_id, text, *args, **kwargs)
        except Exception as error:
            await self._record_outgoing(
                target_id, text, event_type="bot_message", success=False, error=error
            )
            raise
        await self._record_outgoing(target_id, text, event_type="bot_message")
        return result

    async def send_photo(self, chat_id: int | str, photo: Any, *args: Any, **kwargs: Any):
        target_id = self._target_id(chat_id)
        if target_id:
            await ensure_legacy_reply_keyboard_removed(self, target_id)
        text = self._media_text("Фото", kwargs)
        try:
            result = await super().send_photo(chat_id, photo, *args, **kwargs)
        except Exception as error:
            await self._record_outgoing(
                target_id, text, event_type="bot_photo", success=False, error=error
            )
            raise
        media = telegram_message_media(result)
        await self._record_outgoing(
            target_id, text, event_type="bot_photo", metadata={"media": media} if media else None
        )
        return result

    async def send_document(self, chat_id: int | str, document: Any, *args: Any, **kwargs: Any):
        target_id = self._target_id(chat_id)
        if target_id:
            await ensure_legacy_reply_keyboard_removed(self, target_id)
        text = self._media_text("Документ", kwargs)
        try:
            result = await super().send_document(chat_id, document, *args, **kwargs)
        except Exception as error:
            await self._record_outgoing(
                target_id, text, event_type="bot_document", success=False, error=error
            )
            raise
        media = telegram_message_media(result)
        await self._record_outgoing(
            target_id, text, event_type="bot_document", metadata={"media": media} if media else None
        )
        return result

    async def send_video(self, chat_id: int | str, video: Any, *args: Any, **kwargs: Any):
        target_id = self._target_id(chat_id)
        if target_id:
            await ensure_legacy_reply_keyboard_removed(self, target_id)
        text = self._media_text("Видео", kwargs)
        try:
            result = await super().send_video(chat_id, video, *args, **kwargs)
        except Exception as error:
            await self._record_outgoing(
                target_id, text, event_type="bot_video", success=False, error=error
            )
            raise
        media = telegram_message_media(result)
        await self._record_outgoing(
            target_id, text, event_type="bot_video", metadata={"media": media} if media else None
        )
        return result


def refresh_telegram_identity(tg_id: int, username: str | None) -> None:
    """Refresh Telegram identity with a low-cost throttle on every interaction."""
    tg_id = int(tg_id)
    if tg_id <= 0:
        return
    clean_username = str(username or "").strip().lstrip("@")[:100]
    now = time.monotonic()
    due = now - _IDENTITY_REFRESHED_AT.get(tg_id, 0.0) >= IDENTITY_REFRESH_SECONDS
    with sqlite3.connect(config.DB_PATH, timeout=20) as connection:
        connection.execute("PRAGMA busy_timeout=20000")
        row = connection.execute("SELECT username,email FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if row and clean_username and clean_username != str(row[0] or ""):
            connection.execute(
                "UPDATE users SET username=?,identity_source='telegram',identity_updated_at=CURRENT_TIMESTAMP WHERE tg_id=?",
                (clean_username, tg_id),
            )
    # Do not create a new local user merely because Telegram sent an update.
    # A new Telegram contact must pass referral registration from /start first.
    if not row:
        return
    if not due and row[1]:
        return
    try:
        recover_user_identity_sync(tg_id, clean_username, config.DB_PATH, False)
        _IDENTITY_REFRESHED_AT[tg_id] = now
    except Exception as error:
        logger.debug("Не удалось обновить Telegram-привязку %s: %s", tg_id, error)


async def _refresh_identity_background(tg_id: int, username: str | None) -> None:
    try:
        await asyncio.to_thread(refresh_telegram_identity, tg_id, username)
    except Exception as error:
        logger.warning("Не удалось обновить Telegram-привязку %s в фоне: %s", tg_id, error)


class EventJournalMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        middleware_started = time.monotonic()
        from_user = getattr(event, "from_user", None)
        tg_id = int(getattr(from_user, "id", 0) or 0)
        bot_instance = data.get("bot")
        # CallbackQuery must stay fast: any synchronous 3x-ui/SQLite work here can
        # make Telegram's callback token expire before the actual handler starts.
        is_callback = isinstance(event, CallbackQuery)
        if not is_callback and bot_instance is not None:
            chat_id = _event_chat_id(event) or tg_id
            if chat_id:
                _schedule_legacy_keyboard_cleanup(bot_instance, chat_id)
        if tg_id:
            username = str(getattr(from_user, "username", "") or "")
            if is_callback:
                # Refresh identity outside the callback critical path, but do not
                # skip the audit/unread journal for callback events.
                _schedule_identity_refresh(tg_id, username)
            else:
                # Identity refresh may query 3x-ui when the local snapshot is stale.
                # It is maintenance work, not part of Telegram handler latency.
                # Waiting here made /start and ordinary buttons periodically inherit
                # the full 3x-ui timeout.
                _schedule_identity_refresh(tg_id, username)
            event_type, text, metadata = incoming_event_payload(event)
            event_id = await asyncio.to_thread(
                user_events.safe_record_event,
                tg_id,
                username=username,
                direction="in",
                event_type=event_type,
                text=text,
                actor=f"telegram:@{username}" if username else f"telegram:{tg_id}",
                metadata=metadata,
                db_path=config.DB_PATH,
            )
            if not is_callback and str(event_type).startswith("telegram_"):
                try:
                    admin_username=str(getattr(config,"WEB_USERNAME","admin") or "admin").strip()
                    if admin_username:
                        _schedule_panel_push(
                            config.DB_PATH,
                            admin_username,
                            "Новое сообщение",
                            (text or f"Пользователь {tg_id}")[:500],
                            str(getattr(config, "WEB_PUBLIC_PREFIX", "") or "") + "/messages",
                            "incoming-message",
                            "high",
                        )
                except Exception as push_error:
                    logger.debug("Admin Web Push не отправлен: %s", push_error)
        handler_started = time.monotonic()
        result = await handler(event, data)
        handler_ms = int((time.monotonic() - handler_started) * 1000)
        total_ms = int((time.monotonic() - middleware_started) * 1000)
        if handler_ms >= 1000:
            logger.warning(
                "performance operation=telegram_handler tg_id=%s event=%s duration_ms=%s",
                tg_id, type(event).__name__, handler_ms,
            )
        if total_ms >= 1000:
            logger.warning(
                "performance operation=telegram_update tg_id=%s event=%s duration_ms=%s",
                tg_id, type(event).__name__, total_ms,
            )
        return result


# Dispatcher must exist before decorators are evaluated at import time.
dp = Dispatcher(storage=MemoryStorage())
bot: JournalBot | None = None

_panel_push_tasks: set[asyncio.Task] = set()
_background_identity_tasks: set[asyncio.Task] = set()
_background_identity_tg_ids: set[int] = set()
_legacy_keyboard_tasks: set[asyncio.Task] = set()

def _schedule_legacy_keyboard_cleanup(bot_instance: Bot, chat_id: int) -> None:
    async def runner():
        try:
            await ensure_legacy_reply_keyboard_removed(bot_instance, chat_id)
        except Exception as error:
            logger.debug("Не удалось фоново скрыть старую Telegram-клавиатуру %s: %s", chat_id, error)
    task = asyncio.create_task(runner())
    _legacy_keyboard_tasks.add(task)
    task.add_done_callback(_legacy_keyboard_tasks.discard)

def _schedule_identity_refresh(tg_id: int, username: str | None) -> None:
    tg_id = int(tg_id or 0)
    if tg_id <= 0 or tg_id in _background_identity_tg_ids:
        return
    _background_identity_tg_ids.add(tg_id)
    async def runner():
        try:
            await _refresh_identity_background(tg_id, username)
        finally:
            _background_identity_tg_ids.discard(tg_id)
    task = asyncio.create_task(runner())
    _background_identity_tasks.add(task)
    task.add_done_callback(_background_identity_tasks.discard)

def _schedule_panel_push(*args) -> None:
    async def runner():
        try:
            await asyncio.to_thread(push_service.notify_panel, *args)
        except Exception as error:
            logger.debug("Admin Web Push task failed: %s", error)

    task = asyncio.create_task(runner())
    _panel_push_tasks.add(task)
    task.add_done_callback(_panel_push_tasks.discard)
dp.message.outer_middleware(EventJournalMiddleware())
dp.callback_query.outer_middleware(EventJournalMiddleware())

class FSMStates(StatesGroup):
    wait_for_receipt = State()
    wait_for_admin_msg = State()
    wait_for_user_support_msg = State()
    wait_for_broadcast_msg = State()
    wait_for_manual_username = State()
    wait_for_referral_code = State()

MANUAL_USER_REQUEST_ID = 2704


async def get_all_inbound_ids() -> list[int]:
    try:
        return await asyncio.to_thread(inbound_ids_sync)
    except Exception as error:
        logger.error("Ошибка сбора инбаундов: %s", error)
        return []

async def get_client_from_panel(email: str) -> dict | None:
    """Return the normalized current 3x-ui client snapshot."""
    try:
        snapshot = await asyncio.to_thread(fetch_snapshot_sync, False)
        return snapshot.get("by_email", {}).get(email.lower().strip())
    except Exception as error:
        logger.error("Ошибка получения клиента (email): %s", error)
        return None


async def update_client_in_panel(
    client_email: str,
    _client_uuid: str,
    _sub_id: str,
    days_increment: int,
    tg_id: int = 0,
) -> dict | None:
    try:
        days_increment = int(days_increment)
        return await asyncio.to_thread(
            change_client_days_sync,
            client_email,
            days_increment,
            int(tg_id) if int(tg_id) > 0 else None,
        )
    except Exception as error:
        logger.error("Ошибка update_client_in_panel: %s", error)
        return None


async def delete_client_from_panel(client_email: str) -> bool:
    try:
        await asyncio.to_thread(delete_client_sync, client_email, False)
        return True
    except Exception as error:
        logger.error("Ошибка удаления %s: %s", client_email, error)
        return False

async def sync_panel_to_db() -> bool:
    try:
        snapshot = await asyncio.to_thread(fetch_and_sync, True, config.DB_PATH)
        if snapshot.get("stale"):
            logger.error("Синхронизация 3x-ui не выполнена: %s", snapshot.get("error"))
            return False
        return True
    except Exception as error:
        logger.error(f"Ошибка синхронизации: {error}")
        return False

def init_db():
    migrate_database()
    user_events.ensure_schema(config.DB_PATH)

init_db()

def db_get_user(tg_id: int) -> dict:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def db_upsert_user(tg_id: int, username: str, uuid_str: str, email: str, expiry_time: int, sub_id: str, is_active: int = 1, reminder_days: int = -1):
    clean_name = username.replace("@", "").strip() if username else None
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""INSERT INTO users (tg_id, username, uuid, email, expiry_time, enable, sub_id, last_online, last_reminder_days) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, uuid=excluded.uuid, email=excluded.email,
        expiry_time=excluded.expiry_time, enable=excluded.enable, sub_id=excluded.sub_id, last_online=excluded.last_online, last_reminder_days=excluded.last_reminder_days""",
        (tg_id, clean_name, uuid_str, email, int(expiry_time), is_active, sub_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reminder_days))
    conn.commit()
    conn.close()

def get_cabinet_url() -> str:
    if not bool(getattr(config, "CABINET_ENABLED", True)):
        return ""
    base = cabinet_service.public_web_base_url().rstrip("/")
    path = str(getattr(config, "CABINET_PATH", "/cabinet") or "/cabinet").strip()
    if not path.startswith("/"): path = "/" + path
    prefix = cabinet_service.public_prefix()
    if prefix and (path == prefix or path.startswith(prefix + "/")): path = path[len(prefix):] or "/cabinet"
    return base + path if base else ""

def get_personal_cabinet_url(user_id: int, user: dict | None = None) -> str:
    if int(user_id) <= 0 or not bool(getattr(config, "CABINET_ENABLED", True)):
        return ""
    if user is None:
        user = db_get_user(int(user_id))
    if not user:
        return ""
    try:
        return cabinet_service.personal_url(int(user_id))
    except Exception:
        return ""


def get_cabinet_markup(user_id: int, user: dict | None = None) -> Any:
    url = get_personal_cabinet_url(user_id, user)
    if not url or not bool(getattr(config, "CABINET_ENABLED", True)):
        return None


async def get_cabinet_markup_async(user_id: int, user: dict | None = None) -> Any:
    if user is None:
        user = await asyncio.to_thread(db_get_user, int(user_id))
    return get_cabinet_markup(int(user_id), user)
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Личный кабинет на сайте", url=url)
    return builder.as_markup()



def ensure_referral_code(user_id: int) -> str:
    return referral_codes.ensure_referral_code(config.DB_PATH, int(user_id))

def referral_owner(code: str) -> dict | None:
    normalized = re.sub(r"[^0-9]", "", str(code or ""))
    if len(normalized) != 4:
        return None
    with sqlite3.connect(config.DB_PATH, timeout=20) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM users WHERE tg_id>0 AND UPPER(referral_code)=?", (normalized,)).fetchone()
    return dict(row) if row else None

def register_referral_user(user_id: int, username: str | None, code: str) -> bool:
    owner = referral_owner(code)
    if not owner or int(owner.get("tg_id") or 0) == int(user_id):
        return False
    display = re.sub(r"[\\x00-\\x1f\\x7f]+", "", str(username or "").strip().lstrip("@"))[:80] or f"id_{int(user_id)}"
    own_code = ensure_referral_code(int(owner["tg_id"]))
    with sqlite3.connect(config.DB_PATH, timeout=20) as connection:
        connection.row_factory = sqlite3.Row
        existing = connection.execute(
            "SELECT referred_by_tg_id FROM users WHERE tg_id=?", (int(user_id),)
        ).fetchone()
        existing_referrer = int(existing[0] or 0) if existing else 0
        if existing_referrer and existing_referrer != int(owner["tg_id"]):
            return False
        connection.execute("""
            INSERT INTO users(tg_id,username,uuid,email,expiry_time,enable,up,down,total,sub_id,last_sync_at,last_reminder_days,referral_code,referred_by_tg_id,referred_by_code,registered_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(tg_id) DO UPDATE SET
                username=excluded.username,
                referred_by_tg_id=CASE WHEN COALESCE(users.referred_by_tg_id,0)>0 THEN users.referred_by_tg_id ELSE excluded.referred_by_tg_id END,
                referred_by_code=CASE WHEN COALESCE(users.referred_by_tg_id,0)>0 THEN users.referred_by_code ELSE excluded.referred_by_code END
        """, (int(user_id),display,"","",0,1,0,0,0,"",datetime.now().strftime("%Y-%m-%d %H:%M:%S"),-1,None,int(owner["tg_id"]),own_code))
        connection.execute("DELETE FROM pending_registrations WHERE tg_id=?", (int(user_id),))
        connection.commit()
    referral_rewards.register_referral_if_needed(int(user_id), config.DB_PATH)
    return True

def invitation_text(user_id: int) -> str:
    code = ensure_referral_code(int(user_id))
    return f"🎟 <b>Ваш код приглашения</b>\n\n<code>{html.escape(code)}</code>\n\nПередайте этот 4-значный код знакомому для регистрации в боте.\n\n🎁 <b>Реферальная программа:</b> если приглашённый пользователь зарегистрируется по вашему коду и хотя бы один раз успешно оплатит подписку, вы получите <b>+10 бесплатных дней</b>. Бонус начисляется один раз за каждого приглашённого пользователя.\n\n⏱ <i>Код автоматически обновляется раз в 24 часа.</i> Старый код после обновления больше не подходит для новой регистрации."

def get_user_menu(user_id: int, user: dict | None = None):
    now_ms = int(time.time() * 1000)
    active = bool(
        user
        and bool(user.get("enable", 1))
        and (int(user.get("expiry_time") or 0) <= 0 or int(user.get("expiry_time") or 0) > now_ms)
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Моя подписка", callback_data="menu:stats")
    builder.button(text="🔄 Продлить VPN" if user else "💳 Купить VPN", callback_data="menu:buy")
    builder.button(text="📘 Как подключить", callback_data="menu:faq")
    builder.button(text="🆘 Поддержка", callback_data="menu:support")
    if user:
        builder.button(text="🎟 Код приглашения", callback_data="menu:invite")
    personal_cabinet_url = get_personal_cabinet_url(user_id, user) if active else ""
    if personal_cabinet_url:
        builder.button(text="👤 Кабинет на сайте", url=personal_cabinet_url)
    if user_id in config.ADMIN_IDS:
        builder.button(text="🛠 Админ-панель", callback_data="menu:admin")
        builder.adjust(2, 2, 1, 1, 1)
    elif personal_cabinet_url:
        builder.adjust(2, 2, 1, 1)
    elif user:
        builder.adjust(2, 2, 1)
    else:
        builder.adjust(2, 2)
    return builder.as_markup()


async def get_user_menu_async(user_id: int, user: dict | None = None):
    """Build a user keyboard without synchronous SQLite work in async handlers."""
    if user is None:
        user = await asyncio.to_thread(db_get_user, int(user_id))
    return get_user_menu(int(user_id), user)

def get_messages_panel_url() -> str:
    """Return the direct public FargoVPN web-service URL for admin links."""
    base = cabinet_service.public_web_base_url().rstrip("/")
    return f"{base}/messages" if base else ""


def get_messages_notification_markup():
    """Build a Telegram inline button for opening the web-panel messages page.

    The URL is derived from the public HTTPS origin and unique FargoVPN path prefix.
    API tokens are never included in the generated URL.
    """
    url = get_messages_panel_url()
    if not url:
        return None
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Открыть сообщения", url=url)
    return builder.as_markup()


def get_admin_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Сводка", callback_data="admin:summary")
    builder.button(text="👥 Пользователи", callback_data="admin:users")
    builder.button(text="✉ Рассылка", callback_data="admin:broadcast")
    builder.button(text="🔄 Синхронизация 3x-ui", callback_data="admin:sync")
    builder.button(text="📋 Подписки", callback_data="admin:subscriptions")
    builder.button(text="🔑 Выдать доступ", callback_data="admin:grant")
    messages_url = get_messages_panel_url()
    if messages_url:
        builder.button(text="💬 Сообщения в веб-панели", url=messages_url)
    builder.button(text="🏠 Меню пользователя", callback_data="menu:home")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def get_faq_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="📱 Подключение через Incy", callback_data="faq:incy")
    builder.button(text="📘 Подключение через HApp", callback_data="faq:happ")
    builder.button(text="🛟 Если VPN не подключается", callback_data="faq:troubleshoot")
    builder.button(text="🏠 Главное меню", callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def get_instruction_menu(*, include_store: bool = False):
    builder = InlineKeyboardBuilder()
    if include_store:
        builder.button(text="📲 Открыть Incy в App Store", url=INCY_APP_URL)
    builder.button(text="📚 Все инструкции", callback_data="menu:faq")
    builder.button(text="🏠 Главное меню", callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def home_text(*, first_visit: bool = False, summary: str = "") -> str:
    custom = str(getattr(config, "BOT_WELCOME_TEXT", "") or "").strip()
    if custom:
        rendered = custom.replace("{service}", str(config.SERVICE_NAME))
        return html.escape(rendered[:3500])
    intro = (
        f"👋 Добро пожаловать в <b>{html.escape(str(config.SERVICE_NAME))}</b>!"
        if first_visit else f"<b>🏠 {html.escape(str(config.SERVICE_NAME))}</b>"
    )
    return (
        intro + "\n\n" +
        (summary + "\n\n" if summary else "") +
        "Выберите нужное действие ниже. Основная информация о подписке всегда доступна в разделе «Моя подписка»."
    )


async def resolve_user(user_id: int, username: str | None) -> dict | None:
    try:
        return await asyncio.to_thread(
            recover_user_identity_sync,
            user_id,
            username,
            config.DB_PATH,
            False,
        )
    except Exception as error:
        logger.error("Ошибка восстановления пользователя %s: %s", user_id, error)
        return await asyncio.to_thread(db_get_user, user_id)


async def send_home(message: Message, user_id: int) -> None:
    user = await resolve_user(user_id, getattr(message.from_user, "username", None))
    summary = ""
    if user:
        client = await get_client_from_panel(str(user.get("email") or ""))
        source = client or user
        expiry = int(source.get("expiry_time") or 0)
        enabled = bool(source.get("enable", True))
        now_ms = int(time.time() * 1000)
        if not enabled:
            status = "🔴 Доступ отключён"
        elif expiry > 0 and expiry <= now_ms:
            status = "🔴 Подписка истекла"
        else:
            status = "🟢 Подписка активна"
        if expiry > 0:
            until = panel_from_timestamp(expiry / 1000).strftime("%d.%m.%Y %H:%M")
            summary = f"{status} · до <code>{until}</code>"
        else:
            summary = f"{status} · <b>бессрочно</b>"
    else:
        summary = "ℹ️ Подписка пока не оформлена"
    await message.answer(
        home_text(summary=summary),
        parse_mode="HTML",
        reply_markup=await get_user_menu_async(user_id, user),
    )


async def send_user_stats(message: Message, user_id: int, username: str | None) -> None:
    u = await resolve_user(user_id, username)
    if not u:
        await message.answer(
            "❌ Активная подписка не найдена. Оформите или продлите доступ через меню.",
            reply_markup=await get_user_menu_async(user_id),
        )
        return

    panel_client = await get_client_from_panel(str(u.get("email") or ""))
    status_emoji, expiry_str, up_gb, down_gb = "🟢 Активен", "Бессрочно", 0.0, 0.0
    if panel_client:
        ts = int(panel_client.get("expiry_time", 0) or 0)
        if ts > 0:
            expiry_str = panel_from_timestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
            if ts < int(time.time() * 1000):
                status_emoji = "🔴 Истёк"
        if not panel_client.get("enable", True):
            status_emoji = "🔴 Отключён"
        up_gb = bytes_to_gb(panel_client.get("up", 0) or 0)
        down_gb = bytes_to_gb(panel_client.get("down", 0) or 0)
    else:
        local_expiry = int(u.get("expiry_time") or 0)
        if local_expiry > 0:
            expiry_str = panel_from_timestamp(local_expiry / 1000).strftime("%Y-%m-%d %H:%M:%S")
            if local_expiry < int(time.time() * 1000):
                status_emoji = "🔴 Истёк"
        if not bool(u.get("enable", 1)):
            status_emoji = "🔴 Отключён"
        up_gb = bytes_to_gb(u.get("up", 0) or 0)
        down_gb = bytes_to_gb(u.get("down", 0) or 0)

    sub_id = str((panel_client or {}).get("sub_id") or u.get("sub_id") or "").strip()
    sub_url = (await asyncio.to_thread(current_subscription_url_sync, sub_id, fallback_base_url=str(getattr(config, "SUB_BASE_URL", "")))) if sub_id else ""
    total_tr = round(up_gb + down_gb, 2)
    subscription_line = (
        f"<code>{html.escape(sub_url)}</code>"
        if sub_url
        else "<i>Ссылка временно недоступна; обратитесь к администратору.</i>"
    )
    text = (
        f"<b>📊 Подписка {html.escape(str(config.SERVICE_NAME))}</b>\n\n"
        f"📅 Срок до: <code>{html.escape(expiry_str)}</code>\n"
        f"⚡ Статус: <b>{status_emoji}</b>\n"
        f"📤 Отдано: <code>{up_gb} GB</code>\n"
        f"📥 Скачано: <code>{down_gb} GB</code>\n"
        f"🔄 Общий трафик: <code>{total_tr} GB</code>\n\n"
        f"🔗 <b>Ссылка подписки для Incy / HApp:</b>\n{subscription_line}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="📱 Подключить через Incy", callback_data="faq:incy")
    builder.button(text="📘 Подключить через HApp", callback_data="faq:happ")
    builder.button(text="🔄 Продлить", callback_data="menu:buy")
    personal_cabinet_url = get_personal_cabinet_url(user_id, u)
    if personal_cabinet_url:
        builder.button(text="👤 Кабинет на сайте", url=personal_cabinet_url)
    builder.button(text="🏠 Главное меню", callback_data="menu:home")
    builder.adjust(2, 2, 1, 1)
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


def payment_text() -> str:
    return (
        f"<b>💳 Оплата подписки {html.escape(str(config.SERVICE_NAME))}</b>\n\n"
        f"• Цена: <code>{int(config.PAYMENT_PRICE)}</code> ₽ / {SUBSCRIPTION_DAYS} дней\n"
        f"• Номер: <code>{html.escape(str(config.PAYMENT_PHONE))}</code>\n"
        f"• Банк: <b>{html.escape(str(config.PAYMENT_BANK))}</b>\n"
        f"• Получатель: <b>{html.escape(str(config.PAYMENT_RECEIVER))}</b>\n\n"
        "После перевода сразу пришлите скриншот чека как фото или файл изображения. "
        "Бот проверит чек и сообщит результат."
    )


async def start_purchase(message: Message, state: FSMContext) -> None:
    await state.set_state(FSMStates.wait_for_receipt)
    await message.answer(payment_text(), parse_mode="HTML")


async def start_support(message: Message, state: FSMContext) -> None:
    prompt = str(getattr(config, "BOT_SUPPORT_PROMPT", "") or "").strip()
    await message.answer(
        prompt[:1500]
        or "💬 Напишите вопрос одним сообщением. Он появится у администратора и в истории веб-панели."
    )
    await state.set_state(FSMStates.wait_for_user_support_msg)

@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    args = str(getattr(msg, "text", "") or "").split(maxsplit=1)
    payload = args[1].strip() if len(args) > 1 else ""
    current_user = await asyncio.to_thread(db_get_user, int(msg.from_user.id))
    # Registration is mandatory for a new Telegram contact. This check is done
    # before /start shortcuts so buy/renew deep links cannot bypass the referral code.
    if not current_user and not payload.startswith("bind_"):
        def _save_pending_registration() -> None:
            with sqlite3.connect(config.DB_PATH, timeout=20) as connection:
                connection.execute(
                    "INSERT INTO pending_registrations(tg_id,username) VALUES(?,?) ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username",
                    (int(msg.from_user.id), str(getattr(msg.from_user, "username", "") or "")),
                )
                connection.commit()
        await asyncio.to_thread(_save_pending_registration)
        await state.set_state(FSMStates.wait_for_referral_code)
        await msg.answer(
            "🔐 <b>Регистрация по приглашению</b>\n\nВведите 4-значный код приглашения, который вам передал уже зарегистрированный пользователь.",
            parse_mode="HTML",
        )
        return
    if payload in {"renew", "buy", "purchase"}:
        await state.clear()
        await start_purchase(msg, state)
        return
    if payload.startswith("cablogin_"):
        token = payload[len("cablogin_"):].strip()
        try:
            active = await asyncio.to_thread(
                __import__("webapp")._cabinet_active_user, int(msg.from_user.id)
            )
        except Exception:
            active = None
        if not active:
            await msg.answer("❌ Личный кабинет доступен только пользователям с оформленной подпиской.")
            return
        try:
            ok = await asyncio.to_thread(
                __import__("webapp")._cabinet_resolve_login_request, token, int(msg.from_user.id)
            )
        except Exception as error:
            logger.warning("Ошибка подтверждения входа в кабинет: %s", error)
            ok = False
        if not ok:
            await msg.answer("❌ Запрос входа недействителен или уже использован.")
            return
        cabinet_url = get_cabinet_url()
        if cabinet_url:
            await msg.answer("✅ Telegram подтверждён. Нажмите кнопку, чтобы открыть личный кабинет.", reply_markup=await get_cabinet_markup_async(int(msg.from_user.id), active))
        else:
            await msg.answer("✅ Telegram подтверждён. Публичный адрес кабинета пока не настроен.")
        return
    if payload.startswith("bind_"):
        token = payload[5:].strip()
        def _load_bind_request():
            with sqlite3.connect(config.DB_PATH, timeout=20) as connection:
                return connection.execute(
                    "SELECT local_tg_id,expires_at,consumed_at FROM telegram_link_requests WHERE token=?",
                    (token,),
                ).fetchone()
        row = await asyncio.to_thread(_load_bind_request)
        if not row:
            await msg.answer("❌ Ссылка привязки недействительна или уже удалена.")
            return
        if row[2]:
            await msg.answer("ℹ️ Эта ссылка уже использовалась.")
            return
        if str(row[1]) < datetime.utcnow().replace(tzinfo=None).isoformat(timespec="seconds"):
            await msg.answer("❌ Срок действия ссылки истёк. Попросите администратора создать новую.")
            return
        try:
            bound = await asyncio.to_thread(
                bind_database_user_tg_id_sync,
                int(row[0]),
                int(msg.from_user.id),
                str(getattr(msg.from_user, "username", "") or ""),
                config.DB_PATH,
            )
            def _consume_bind_request() -> None:
                with sqlite3.connect(config.DB_PATH, timeout=20) as connection:
                    connection.execute(
                        "UPDATE telegram_link_requests SET consumed_at=CURRENT_TIMESTAMP,resolved_tg_id=? WHERE token=?",
                        (int(msg.from_user.id), token),
                    )
                    connection.commit()
            await asyncio.to_thread(_consume_bind_request)
            await msg.answer(
                "✅ Telegram успешно привязан. Теперь администратор увидит ваш Telegram ID в панели."
            )
        except Exception as error:
            await msg.answer(f"❌ Не удалось выполнить привязку: {html.escape(str(error)[:400])}", parse_mode="HTML")
        return
    user = current_user or await asyncio.to_thread(db_get_user, int(msg.from_user.id))
    await state.clear()
    await asyncio.to_thread(ensure_referral_code, int(msg.from_user.id))
    await send_home(msg, msg.from_user.id)


@dp.message(FSMStates.wait_for_referral_code)
async def process_referral_code(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    code = re.sub(r"[^0-9]", "", str(msg.text or ""))
    if len(code) != 4:
        await msg.answer("❌ Код должен состоять ровно из 4 цифр. Попробуйте ещё раз.")
        return
    registered = await asyncio.to_thread(register_referral_user, int(msg.from_user.id), getattr(msg.from_user, "username", None), code)
    if registered:
        await asyncio.to_thread(ensure_referral_code, int(msg.from_user.id))
        await state.clear()
        owner = await asyncio.to_thread(referral_owner, code) or {}
        owner_name = str(owner.get("username") or "пользователь")
        await asyncio.to_thread(
            user_events.safe_record_event,
            int(msg.from_user.id), username=str(getattr(msg.from_user, "username", "") or ""),
            direction="system", event_type="referral_registered",
            text=f"Регистрация по приглашению пользователя @{owner_name}",
            actor="telegram", metadata={"referral_code": code, "referred_by_tg_id": int(owner.get("tg_id") or 0)}, db_path=config.DB_PATH,
        )
        await msg.answer("✅ Регистрация завершена. Добро пожаловать!", reply_markup=await get_user_menu_async(int(msg.from_user.id)))
        return
    await msg.answer("❌ Такой код не найден. Попросите зарегистрированного пользователя дать вам свой личный 4-значный код.")

@dp.callback_query(F.data.startswith("menu:"))
async def dynamic_user_menu(call: CallbackQuery, state: FSMContext):
    if not call.message:
        await safe_callback_answer(call)
        return
    action = str(call.data or "").split(":", 1)[-1]
    user_id = int(call.from_user.id)
    if action == "admin" and user_id not in config.ADMIN_IDS:
        await safe_callback_answer(call, "Недостаточно прав", show_alert=True)
        return
    await safe_callback_answer(call)
    if action == "invite":
        user = await asyncio.to_thread(db_get_user, int(call.from_user.id))
        if not user:
            await call.message.answer("❌ Профиль ещё не зарегистрирован.")
        else:
            await call.message.answer(invitation_text(int(call.from_user.id)), parse_mode="HTML", reply_markup=await get_user_menu_async(int(call.from_user.id), user))
        return
    if action == "home":
        await state.clear()
        await send_home(call.message, user_id)
    elif action == "stats":
        await state.clear()
        await send_user_stats(call.message, user_id, call.from_user.username)
    elif action == "buy":
        await start_purchase(call.message, state)
    elif action == "support":
        await start_support(call.message, state)
    elif action == "faq":
        await state.clear()
        await call.message.answer(
            "<b>📚 FAQ и инструкции</b>\n\nВыберите приложение или раздел помощи.",
            parse_mode="HTML",
            reply_markup=get_faq_menu(),
        )
    elif action == "admin":
        await state.clear()
        await call.message.answer(
            f"<b>🛠 Администрирование {html.escape(str(config.SERVICE_NAME))}</b>",
            parse_mode="HTML",
            reply_markup=get_admin_menu(),
        )


@dp.callback_query(F.data.startswith("faq:"))
async def faq_callbacks(call: CallbackQuery, state: FSMContext):
    await safe_callback_answer(call)
    await state.clear()
    if not call.message:
        return
    action = str(call.data or "").split(":", 1)[-1]
    if action == "incy":
        text = (
            "<b>📱 Подключение через Incy</b>\n\n"
            "1. Откройте «Моя подписка» и скопируйте ссылку подписки.\n"
            "2. Установите и откройте Incy.\n"
            "3. Выберите добавление или импорт подписки и вставьте скопированный URL.\n"
            "4. Сохраните подписку, обновите список серверов и выберите подходящий сервер.\n"
            "5. Нажмите подключение и разрешите iOS создать VPN-конфигурацию.\n\n"
            "Названия пунктов могут немного отличаться в разных версиях приложения."
        )
        await call.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=get_instruction_menu(include_store=True),
        )
    elif action == "happ":
        await call.message.answer(
            "<b>📘 Подключение через HApp</b>\n\n"
            "1. Откройте «Моя подписка» и скопируйте ссылку.\n"
            "2. В HApp нажмите «+» и выберите добавление подписки.\n"
            "3. Вставьте URL, сохраните и обновите список серверов.\n"
            "4. Выберите сервер и подключитесь.",
            parse_mode="HTML",
            reply_markup=get_instruction_menu(),
        )
    elif action == "troubleshoot":
        await call.message.answer(
            "<b>🛟 Если VPN не подключается</b>\n\n"
            "• Обновите подписку внутри приложения.\n"
            "• Проверьте, что срок подписки не истёк и доступ не заблокирован.\n"
            "• Переключите сервер или протокол.\n"
            "• Отключите другой VPN/Private Relay и повторите подключение.\n"
            "• Перезапустите приложение и интернет-соединение.\n\n"
            "Если проблема остаётся, отправьте администратору модель устройства, приложение и текст ошибки.",
            parse_mode="HTML",
            reply_markup=get_instruction_menu(),
        )

@dp.callback_query(F.data == "show_instruction")
async def process_instruction(call: CallbackQuery):
    await safe_callback_answer(call)
    await call.message.answer(
        "<b>📘 Подключение через HApp</b>\n\n"
        "Скопируйте ссылку подписки, нажмите «+», добавьте подписку по URL, сохраните и обновите список серверов.",
        parse_mode="HTML",
        reply_markup=get_instruction_menu(),
    )

@dp.message(lambda m: m.text and "Статистика" in m.text)
async def user_stats(msg: Message):
    await send_user_stats(msg, msg.from_user.id, msg.from_user.username)

@dp.message(lambda m: m.text and "Купить" in m.text)
async def buy_vpn(msg: Message, state: FSMContext):
    await start_purchase(msg, state)

def receipt_ocr_options() -> dict:
    return {
        "receiver": str(getattr(config, "PAYMENT_RECEIVER", "")),
        "phone": str(getattr(config, "PAYMENT_PHONE", "")),
        "minimum_amount": float(getattr(config, "RECEIPT_MIN_AMOUNT", 150)),
        "max_age_hours": int(getattr(config, "RECEIPT_MAX_AGE_HOURS", 24)),
        "aliases": getattr(config, "RECEIPT_RECEIVER_ALIASES", ""),
        "allow_masked_phone": bool(getattr(config, "RECEIPT_ALLOW_MASKED_PHONE", True)),
        "timezone_name": str(getattr(config, "RECEIPT_TIMEZONE", "Europe/Moscow")),
        "languages": str(getattr(config, "RECEIPT_OCR_LANGUAGES", "rus+eng")),
        "timeout_seconds": int(getattr(config, "RECEIPT_OCR_TIMEOUT", 20)),
        "check_name": bool(getattr(config, "RECEIPT_FILTER_NAME", True)),
        "check_phone": bool(getattr(config, "RECEIPT_FILTER_PHONE", False)),
        "check_amount": bool(getattr(config, "RECEIPT_FILTER_AMOUNT", True)),
        "check_date": bool(getattr(config, "RECEIPT_FILTER_DATE", False)),
        "check_status": bool(getattr(config, "RECEIPT_FILTER_STATUS", True)),
        "check_duplicate": bool(getattr(config, "RECEIPT_FILTER_DUPLICATE", True)),
    }


def mark_receipt_error(payment_id: int, error: Exception) -> None:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "UPDATE payments SET ocr_status='error',last_error=? WHERE id=?",
            (f"OCR: {str(error)[:900]}", int(payment_id)),
        )
        conn.commit()
    finally:
        conn.close()


async def send_subscription_activated(user_id: int, subscription) -> str:
    expiry_date = panel_from_timestamp(subscription.expiry_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
    action = "активирована" if subscription.created else "продлена"
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 Ваша подписка {config.SERVICE_NAME} успешно {action} до "
                f"<code>{expiry_date}</code>!\nСсылка доступна в меню «📊 Моя Статистика»."
            ),
            parse_mode="HTML",
        )
    except Exception as error:
        logger.warning("Подписка активирована, но уведомление пользователю %s не доставлено: %s", user_id, error)
    return expiry_date


async def send_referral_reward_notification(referral: dict | None) -> None:
    if not isinstance(referral, dict):
        return
    if referral.get("status") != referral_rewards.GRANTED or referral.get("already_processed"):
        return
    referrer_id = int(referral.get("referrer_tg_id") or 0)
    referred_id = int(referral.get("referred_tg_id") or 0)
    if referrer_id <= 0 or referrer_id == referred_id:
        return
    try:
        await bot.send_message(
            chat_id=referrer_id,
            text=(
                "🎉 Новый реферал!\n\n"
                "Приглашённый пользователь впервые успешно оплатил подписку.\n"
                f"Вам начислено +{referral_rewards.REWARD_DAYS} бесплатных дней."
            ),
        )
    except Exception as error:
        logger.warning("Реферальный бонус начислен, но уведомление TG %s не доставлено: %s", referrer_id, error)


async def send_receipt_to_admin(msg: Message, admin_id: int, file_id: str, caption: str, reply_markup=None) -> None:
    if msg.photo:
        await bot.send_photo(
            chat_id=admin_id,
            photo=file_id,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    else:
        await bot.send_document(
            chat_id=admin_id,
            document=file_id,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )


@dp.message(F.photo, FSMStates.wait_for_receipt)
@dp.message(F.document, FSMStates.wait_for_receipt)
async def process_receipt_photo(msg: Message, state: FSMContext):
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document:
        filename = str(msg.document.file_name or "").lower()
        mime_type = str(msg.document.mime_type or "").lower()
        if not (mime_type.startswith("image/") or filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"))):
            await msg.answer("❌ Пришлите чек как фотографию или файл изображения.")
            return
        file_id = msg.document.file_id
    else:
        return

    def _create_payment() -> int:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO payments (tg_id, username, telegram_file_id, amount, ocr_status) VALUES (?, ?, ?, ?, 'not_checked')",
                (msg.from_user.id, msg.from_user.username or "", file_id, config.PAYMENT_PRICE),
            )
            payment_id = int(cur.lastrowid)
            conn.commit()
            return payment_id
        finally:
            conn.close()
    payment_id = await asyncio.to_thread(_create_payment)

    analysis: ReceiptAnalysis | None = None
    approval = None
    ocr_enabled = bool(getattr(config, "RECEIPT_OCR_ENABLED", True))
    if ocr_enabled:
        try:
            content, _media_type, _filename = await asyncio.to_thread(
                download_telegram_file,
                config.BOT_TOKEN,
                file_id,
                45.0,
            )
            analysis = await asyncio.to_thread(
                analyze_payment_receipt,
                config.DB_PATH,
                payment_id,
                content,
                **receipt_ocr_options(),
            )
            if analysis.passed and bool(getattr(config, "RECEIPT_AUTO_APPROVE", True)):
                approval = await asyncio.to_thread(
                    approve_payment_sync,
                    payment_id,
                    "ocr:auto",
                    SUBSCRIPTION_DAYS,
                    config.DB_PATH,
                )
        except Exception as error:
            logger.exception("Ошибка автоматического распознавания чека %s", payment_id)
            await asyncio.to_thread(mark_receipt_error, payment_id, error)

    automatically_approved = bool(approval and approval.success and not approval.already_processed and approval.subscription)
    if automatically_approved:
        subscription = approval.subscription
        await send_referral_reward_notification(approval.referral)
        expiry_date = panel_from_timestamp(subscription.expiry_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
        action = "активирована" if subscription.created else "продлена"
        user_reply = (
            f"✅ Чек распознан и подтверждён автоматически. Подписка {action} до "
            f"<code>{expiry_date}</code>. Выдано {SUBSCRIPTION_DAYS} дней по выбранному тарифу."
        )
    else:
        user_reply = "⏳ Ваш чек принят и отправлен на проверку администратору."

    user_info = (
        f"📑 Чек #{payment_id} от @{html.escape(msg.from_user.username or 'нет')} "
        f"(ID: <code>{msg.from_user.id}</code>)"
    )
    if analysis:
        user_info += "\n\n<b>Автопроверка:</b>\n<pre>" + html.escape(analysis.compact_summary()) + "</pre>"
    elif not ocr_enabled:
        user_info += "\n\n⚪ Автопроверка отключена в настройках."
    else:
        user_info += "\n\n⚠ OCR не завершён; требуется ручная проверка."

    builder = None
    if automatically_approved:
        user_info += f"\n\n✅ <b>Подтверждено автоматически, выдано {SUBSCRIPTION_DAYS} дней.</b>"
    else:
        if approval and not approval.success:
            user_info += "\n\n⚠ Совпадения найдены, но активация не выполнена: " + html.escape(approval.message)
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="☑ Подтвердить", callback_data=f"approve_{msg.from_user.id}_{payment_id}")
        keyboard.button(text="❌ Отказать", callback_data=f"decline_{msg.from_user.id}_{payment_id}")
        keyboard.adjust(1)
        builder = keyboard.as_markup()

    admin_caption = user_info
    if len(admin_caption) > 1024:
        admin_caption = (
            f"📑 Чек #{payment_id} от @{html.escape(msg.from_user.username or 'нет')} "
            f"(ID: <code>{msg.from_user.id}</code>)\n\n"
            "⚠ Подробный результат OCR слишком длинный; откройте чек в веб-панели."
        )
    for admin_id in config.ADMIN_IDS:
        try:
            await send_receipt_to_admin(msg, admin_id, file_id, admin_caption, builder)
        except Exception as error:
            logger.error("Ошибка отправки чека админу %s: %s", admin_id, error)
    await msg.answer(user_reply, reply_markup=await get_user_menu_async(msg.from_user.id), parse_mode="HTML")
    await state.clear()

@dp.message(lambda m: m.text and "Связь" in m.text)
async def support_request(msg: Message, state: FSMContext):
    await start_support(msg, state)

@dp.message(FSMStates.wait_for_user_support_msg)
async def process_support_msg(msg: Message, state: FSMContext):
    support_text = str(msg.text or msg.caption or "").strip()
    media = telegram_message_media(msg)
    if not support_text and not media:
        await msg.answer("Пожалуйста, отправьте текст, фотографию или видео.")
        return
    await asyncio.to_thread(
        user_events.safe_record_event,
        msg.from_user.id,
        username=msg.from_user.username,
        direction="in",
        event_type="support_forwarded",
        text=support_text or (f"[{media.get('kind')}]" if media else "Обращение без текста"),
        actor="telegram_user",
        metadata={"media": media} if media else None,
        db_path=config.DB_PATH,
    )
    media_label = ""
    if media:
        media_label = "фотографию" if media.get("kind") == "photo" else "видео" if media.get("kind") == "video" else "файл"
    for admin_id in config.ADMIN_IDS:
        try:
            heading = (
                f"📬 Поддержка от @{msg.from_user.username or 'нет'} "
                f"(ID: {msg.from_user.id})"
            )
            if media_label:
                heading += f" прислал(а) {media_label}."
            if support_text and not media:
                heading += f":\n\n{support_text}"
            await bot.send_message(chat_id=admin_id, text=heading)
            if media:
                await bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=msg.chat.id,
                    message_id=msg.message_id,
                )
        except Exception as error:
            logger.warning("Не удалось доставить обращение админу %s: %s", admin_id, error)
    await msg.answer("✅ Сообщение доставлено. Ответ придёт в этот чат.", reply_markup=await get_user_menu_async(msg.from_user.id))
    await state.clear()


async def send_crm_list(message: Message) -> None:
    def _load_crm_users():
        with sqlite3.connect(config.DB_PATH, timeout=20) as connection:
            return connection.execute(
                "SELECT tg_id,username,enable,expiry_time FROM users ORDER BY username COLLATE NOCASE, tg_id LIMIT 100"
            ).fetchall()
    rows = await asyncio.to_thread(_load_crm_users)
    if not rows:
        await message.answer("📉 База пользователей пуста.", reply_markup=get_admin_menu())
        return
    now_ms = int(time.time() * 1000)
    builder = InlineKeyboardBuilder()
    for tg_id, username, enabled, expiry in rows:
        active = bool(enabled) and (int(expiry or 0) <= 0 or int(expiry or 0) > now_ms)
        label = f"{'🟢' if active else '🔴'} @{username}" if username else f"{'🟢' if active else '🔴'} ID {tg_id}"
        builder.button(text=label[:48], callback_data=f"crmv_{tg_id}")
    builder.button(text="⬅ Админ-меню", callback_data="menu:admin")
    builder.adjust(2)
    await message.answer(
        f"<b>👥 Пользователи</b>\n\nПоказано {len(rows)} записей. Выберите карточку:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


async def run_admin_sync(message: Message) -> None:
    await message.answer("⏳ Получаю актуальные данные из 3x-ui…")
    success = await sync_panel_to_db()
    await message.answer(
        "✅ Пользователи и трафик синхронизированы."
        if success
        else "❌ Синхронизация не выполнена. Проверьте доступность и токен 3x-ui.",
        reply_markup=get_admin_menu(),
    )


def admin_summary_text() -> str:
    """Compact operational snapshot for the Telegram admin menu."""
    now_ms = int(time.time() * 1000)
    week_ms = now_ms + 7 * 86_400_000
    with sqlite3.connect(config.DB_PATH, timeout=20) as connection:
        total, active, expiring = connection.execute(
            """SELECT COUNT(*),
            SUM(CASE WHEN enable=1 AND (expiry_time<=0 OR expiry_time>?) THEN 1 ELSE 0 END),
            SUM(CASE WHEN enable=1 AND expiry_time>? AND expiry_time<=? THEN 1 ELSE 0 END)
            FROM users""",
            (now_ms, now_ms, week_ms),
        ).fetchone()
        pending = connection.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
        unread = 0
        try:
            unread = connection.execute("SELECT COALESCE(SUM(unread_count),0) FROM user_message_state").fetchone()[0]
        except sqlite3.Error:
            pass
    return (
        "<b>📊 Сводка сервиса</b>\n\n"
        f"👥 Пользователей: <b>{int(total or 0)}</b>\n"
        f"✅ Активных подписок: <b>{int(active or 0)}</b>\n"
        f"⏳ Истекают за 7 дней: <b>{int(expiring or 0)}</b>\n"
        f"💳 Платежей на проверке: <b>{int(pending or 0)}</b>\n"
        f"💬 Непрочитанных сообщений: <b>{int(unread or 0)}</b>"
    )


def subscription_report_text() -> str:
    with sqlite3.connect(config.DB_PATH, timeout=20) as connection:
        users = connection.execute(
            "SELECT username,tg_id,expiry_time,last_reminder_days,enable FROM users ORDER BY expiry_time"
        ).fetchall()
    if not users:
        return "📭 Пользователи с подписками не найдены."
    now_ms = int(time.time() * 1000)
    lines = ["📋 <b>Подписки и уведомления</b>\n"]
    for username, tg_id, expiry_time, last_reminder, enabled in users:
        mention = f"@{html.escape(str(username))}" if username else f"ID <code>{tg_id}</code>"
        expiry_value = int(expiry_time or 0)
        if not enabled:
            state = "заблокирован ⛔"
        elif expiry_value <= 0:
            state = "бессрочно ♾"
        elif expiry_value <= now_ms:
            state = "истёк ❌"
        else:
            days_left = max(0, (local_date_from_timestamp(expiry_value / 1000) - now_local().date()).days)
            reminder = "не отправлялось" if int(last_reminder or -1) == -1 else f"за {last_reminder} дн."
            state = f"{days_left} дн. · уведомление {reminder}"
        lines.append(f"• {mention} — {state}")
    return "\n".join(lines)


async def send_subscription_report(message: Message) -> None:
    text = subscription_report_text()
    chunks = [text[index:index + 3900] for index in range(0, len(text), 3900)] or [text]
    for index, chunk in enumerate(chunks):
        await message.answer(
            chunk,
            parse_mode="HTML",
            reply_markup=get_admin_menu() if index == len(chunks) - 1 else None,
        )


@dp.callback_query(F.data.startswith("admin:"))
async def dynamic_admin_menu(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in config.ADMIN_IDS:
        await safe_callback_answer(call, "Недостаточно прав", show_alert=True)
        return
    await safe_callback_answer(call)
    if not call.message:
        return
    action = str(call.data or "").split(":", 1)[-1]
    if action == "summary":
        await call.message.answer(await asyncio.to_thread(admin_summary_text), parse_mode="HTML", reply_markup=get_admin_menu())
    elif action == "users":
        await send_crm_list(call.message)
    elif action == "sync":
        await run_admin_sync(call.message)
    elif action == "subscriptions":
        await send_subscription_report(call.message)
    elif action == "broadcast":
        await state.set_state(FSMStates.wait_for_broadcast_msg)
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отмена", callback_data="admin:cancel")
        await call.message.answer(
            "📢 Отправьте текст, фото, документ или видео для рассылки всем пользователям.",
            reply_markup=builder.as_markup(),
        )
    elif action == "grant":
        await state.set_state(FSMStates.wait_for_manual_username)
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(
                    text="🔎 Выбрать пользователя в Telegram",
                    request_users=KeyboardButtonRequestUsers(
                        request_id=MANUAL_USER_REQUEST_ID,
                        user_is_bot=False,
                        max_quantity=1,
                        request_name=True,
                        request_username=True,
                    ),
                )
            ]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await call.message.answer(
            "👤 Введите имя пользователя. Telegram ID указывать необязательно.\n"
            "Примеры: <code>ivan</code> или <code>123456789 ivan</code>.\n\n"
            "Либо нажмите кнопку ниже и выберите человека прямо из Telegram —\n"
            "в этом случае Telegram передаст боту настоящий ID автоматически.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    elif action == "cancel":
        await state.clear()
        await call.message.answer("Действие отменено.", reply_markup=get_admin_menu())

@dp.message(lambda m: m.text and "Синхронизация" in m.text)
async def cmd_sync_button(msg: Message):
    if msg.from_user.id not in config.ADMIN_IDS:
        return
    await run_admin_sync(msg)

@dp.callback_query(F.data.startswith("approve_"))
async def handle_pay_approve_callback(call: CallbackQuery):
    if call.from_user.id not in config.ADMIN_IDS:
        try:
            await safe_callback_answer(call, "Нет доступа", show_alert=True)
        except Exception:
            pass
        return
    try: await safe_callback_answer(call, "Обработка подтверждения...", show_alert=False)
    except: pass
    try:
        parts = call.data.split("_")
        payment_id = int(parts[2]) if len(parts) > 2 else 0
        if payment_id <= 0:
            raise ValueError("В кнопке отсутствует номер платежа")

        result = await asyncio.to_thread(
            approve_payment_sync,
            payment_id,
            str(call.from_user.id),
            SUBSCRIPTION_DAYS,
            config.DB_PATH,
        )
        if not result.success:
            await call.message.answer(f"❌ Платёж не подтверждён: {result.message}")
            return
        if result.already_processed:
            await call.message.answer("ℹ️ Этот чек уже был подтверждён ранее; повторное продление не выполнено.")
            await send_referral_reward_notification(result.referral)
            return

        subscription = result.subscription
        if not subscription:
            raise RuntimeError("Результат активации подписки отсутствует")
        user_id = int(subscription.tg_id)
        action = "активирована" if subscription.created else "продлена"
        expiry_date = await send_subscription_activated(user_id, subscription)
        await send_referral_reward_notification(result.referral)
        await asyncio.to_thread(
            user_events.safe_record_event,
            user_id,
            username=subscription.username,
            direction="system",
            event_type="payment_approved",
            text=f"Платёж #{payment_id} подтверждён; доступ продлён на {SUBSCRIPTION_DAYS} дн.",
            actor=f"admin:{call.from_user.id}",
            metadata={"payment_id": payment_id, "expiry_time": subscription.expiry_time},
            db_path=config.DB_PATH,
        )
        caption = (call.message.caption or "Чек") + f"\n\n✅ Одобрено. Подписка {action} до {expiry_date}"
        await call.message.edit_caption(caption=caption, reply_markup=None)
    except Exception as err:
        logger.exception("Ошибка модерации чека")
        await call.message.answer(f"❌ Ошибка модерации чека: {err}")

@dp.callback_query(F.data.startswith("decline_"))
async def handle_pay_decline_callback(call: CallbackQuery):
    if call.from_user.id not in config.ADMIN_IDS:
        try:
            await safe_callback_answer(call, "Нет доступа", show_alert=True)
        except Exception:
            pass
        return
    try: await safe_callback_answer(call, "Заявка отклонена", show_alert=False)
    except: pass
    try:
        parts = call.data.split("_")
        payment_id = int(parts[2]) if len(parts) > 2 else 0
        if payment_id <= 0:
            raise ValueError("В кнопке отсутствует номер платежа")
        payment = await asyncio.to_thread(
            decline_payment_sync,
            payment_id,
            str(call.from_user.id),
            config.DB_PATH,
        )
        user_id = int(payment["tg_id"])
        await asyncio.to_thread(
            user_events.safe_record_event,
            user_id,
            username=str(payment.get("username") or ""),
            direction="system",
            event_type="payment_declined",
            text=f"Платёж #{payment_id} отклонён администратором",
            actor=f"admin:{call.from_user.id}",
            metadata={"payment_id": payment_id},
            db_path=config.DB_PATH,
        )
        try:
            await bot.send_message(chat_id=user_id, text="❌ Ваш чек об оплате отклонён администратором. Проверьте перевод.")
        except Exception as error:
            logger.warning("Не удалось уведомить пользователя %s об отклонении: %s", user_id, error)
        await call.message.edit_caption(caption=(call.message.caption or "Чек") + "\n\n❌ Отклонено администратором.", reply_markup=None)
    except Exception as error:
        await call.message.answer(f"❌ Не удалось отклонить чек: {error}")

@dp.message(lambda m: m.text and "Рассылка" in m.text)
async def broad_start(msg: Message, state: FSMContext):
    if msg.from_user.id not in config.ADMIN_IDS:
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin:cancel")
    await msg.answer(
        "📢 Отправьте текст, фото, документ или видео для массовой рассылки всем пользователям:",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(FSMStates.wait_for_broadcast_msg)

@dp.message(FSMStates.wait_for_broadcast_msg)
async def broad_proc(msg: Message, state: FSMContext):
    if msg.from_user.id not in config.ADMIN_IDS:
        await state.clear()
        return
    if msg.text == "❌ Отмена рассылки":
        await state.clear()
        return await msg.answer("❌ Рассылка успешно отменена.", reply_markup=get_admin_menu())
    await state.clear()
    def _list_broadcast_users() -> list[tuple[int]]:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT tg_id FROM users")
            return cursor.fetchall()
        finally:
            conn.close()
    users = await asyncio.to_thread(_list_broadcast_users)
    await msg.answer(f"🚀 Запуск рассылки по базе из {len(users)} записей...")
    cnt = 0
    failed = 0
    for row in users:
        try:
            target_id = int(row[0])
            if target_id <= 0:
                continue
            if msg.photo: await bot.send_photo(chat_id=target_id, photo=msg.photo[-1].file_id, caption=msg.caption)
            elif msg.document: await bot.send_document(chat_id=target_id, document=msg.document.file_id, caption=msg.caption)
            elif msg.video: await bot.send_video(chat_id=target_id, video=msg.video.file_id, caption=msg.caption)
            else: await bot.send_message(chat_id=target_id, text=msg.text)
            cnt += 1
            await asyncio.sleep(0.04)
        except Exception as error:
            failed += 1
            logger.debug("Рассылка пользователю %s не доставлена: %s", row[0], error)
    await asyncio.to_thread(
        user_events.safe_record_event,
        msg.from_user.id,
        username=msg.from_user.username,
        direction="system",
        event_type="broadcast_completed",
        text=f"Рассылка завершена: доставлено {cnt}, ошибок {failed}",
        actor=f"admin:{msg.from_user.id}",
        metadata={"delivered": cnt, "failed": failed, "total": len(users)},
        db_path=config.DB_PATH,
    )
    await msg.answer(
        f"☑ Рассылка завершена. Доставлено: {cnt}; ошибок: {failed}.",
        reply_markup=get_admin_menu(),
    )

@dp.message(lambda m: m.text and "Управление" in m.text)
async def crm_main(msg: Message):
    if msg.from_user.id not in config.ADMIN_IDS:
        return
    await send_crm_list(msg)

@dp.callback_query(F.data.startswith("crmv_"))
async def crmview_card(call: CallbackQuery):
    if call.from_user.id not in config.ADMIN_IDS:
        await safe_callback_answer(call, "Недостаточно прав", show_alert=True)
        return
    await safe_callback_answer(call)
    tg_id = int(call.data.split("_")[-1])
    u = await asyncio.to_thread(db_get_user, tg_id)
    if not u: return
    builder = InlineKeyboardBuilder()
    builder.button(text=f"➕ Продлить {SUBSCRIPTION_DAYS} дней", callback_data=f"crmmod_{SUBSCRIPTION_DAYS}_{tg_id}")
    builder.button(text=f"➖ Урезать {SUBSCRIPTION_DAYS} дней", callback_data=f"crmmod_-{SUBSCRIPTION_DAYS}_{tg_id}")
    builder.button(text="🗑 Полностью удалить", callback_data=f"crmdel_{tg_id}")
    builder.adjust(2, 1)
    date_str = "Бессрочно" if u["expiry_time"] <= 0 else panel_from_timestamp(u["expiry_time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
    card = f"<b>👤 КЛИЕНТ CRM:</b>\n\n🆔 TG ID: <code>{tg_id}</code>\n👤 Ник: @{u['username'] or 'нет'}\n📧 Email: <code>{u['email']}</code>\n📅 До: <code>{date_str}</code>"
    await call.message.answer(card, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("crmmod_"))
async def crm_mod_proc(call: CallbackQuery):
    if call.from_user.id not in config.ADMIN_IDS:
        await safe_callback_answer(call, "Недостаточно прав", show_alert=True)
        return
    await safe_callback_answer(call, "Изменение...")
    parts = call.data.split("_")
    days = int(parts[1])
    tg_id = int(parts[-1])
    u = await asyncio.to_thread(db_get_user, tg_id)
    if not u: return
    
    panel_client = await update_client_in_panel(
        u["email"], u["uuid"], u["sub_id"], days, tg_id=tg_id
    )
    if panel_client:
        now_ts = int(time.time() * 1000)
        new_expiry_ts = int(panel_client.get("expiry_time") or 0)
        new_expiry_str = (
            "Бессрочно"
            if new_expiry_ts <= 0
            else panel_from_timestamp(new_expiry_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
        )
        await asyncio.to_thread(
            db_upsert_user,
            tg_id,
            u["username"],
            str(panel_client.get("uuid") or u["uuid"] or ""),
            str(panel_client.get("email") or u["email"] or ""),
            new_expiry_ts,
            str(panel_client.get("sub_id") or u["sub_id"] or ""),
            1 if bool(panel_client.get("enable", True)) and (new_expiry_ts <= 0 or new_expiry_ts > now_ts) else 0,
            -1,
        )
        await asyncio.to_thread(
            user_events.safe_record_event,
            tg_id,
            username=str(u.get("username") or ""),
            direction="system",
            event_type="subscription_days_changed",
            text=f"Срок изменён администратором на {days:+d} дн.",
            actor=f"admin:{call.from_user.id}",
            metadata={"expiry_time": new_expiry_ts},
            db_path=config.DB_PATH,
        )
        await call.message.answer(f"✅ Срок подписки изменён на {days:+d} дн. Новая дата: {new_expiry_str}", reply_markup=get_admin_menu())
    else:
        await call.message.answer("❌ Ошибка изменения срока на панели 3X-UI.", reply_markup=get_admin_menu())

@dp.callback_query(F.data.startswith("crmdel_"))
async def crm_delete_proc(call: CallbackQuery):
    if call.from_user.id not in config.ADMIN_IDS:
        await safe_callback_answer(call, "Недостаточно прав", show_alert=True)
        return
    await safe_callback_answer(call, "Удаление...")
    tg_id = int(call.data.split("_")[-1])
    u = await asyncio.to_thread(db_get_user, tg_id)
    if not u: return
    panel_success = await delete_client_from_panel(u["email"])
    if not panel_success:
        await call.message.answer(
            "❌ 3x-ui не подтвердила удаление. Локальная запись сохранена.",
            reply_markup=get_admin_menu(),
        )
        return
    await asyncio.to_thread(
        user_events.safe_record_event,
        tg_id,
        username=str(u.get("username") or ""),
        direction="system",
        event_type="user_deleted",
        text="Пользователь удалён администратором через Telegram-бота",
        actor=f"admin:{call.from_user.id}",
        metadata={"email": str(u.get("email") or "")},
        db_path=config.DB_PATH,
    )
    def _delete_local_user() -> None:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_delete_local_user)
    await call.message.answer(f"🗑 Пользователь успешно удален из базы бота и панели 3X-UI!", reply_markup=get_admin_menu())
    await call.message.delete()

@dp.message(Command("cabinet"))
async def cmd_cabinet(msg: Message):
    if not get_cabinet_url() or not bool(getattr(config, "CABINET_ENABLED", True)):
        await msg.answer("❌ Личный кабинет сейчас недоступен.")
        return
    user = await resolve_user(msg.from_user.id, msg.from_user.username)
    if not user:
        await msg.answer("❌ Личный кабинет доступен только пользователям с оформленной подпиской.")
        return
    await msg.answer("👤 Откройте личный кабинет на сайте. Ссылка персональная — не пересылайте её другим:", reply_markup=await get_cabinet_markup_async(int(msg.from_user.id), user))

@dp.message(lambda m: m.text and "Администратора" in m.text)
@dp.message(Command("admin"))
async def cmd_admin_kb(msg: Message, state: FSMContext):
    await state.clear()
    if msg.from_user.id in config.ADMIN_IDS:
        await msg.answer(
            f"<b>🛠 Администрирование {html.escape(str(config.SERVICE_NAME))}</b>",
            parse_mode="HTML",
            reply_markup=get_admin_menu(),
        )

@dp.message(lambda m: m.text and "вручную" in m.text)
async def manual_grant_start(msg: Message, state: FSMContext):
    if msg.from_user.id not in config.ADMIN_IDS: return
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="🔎 Выбрать пользователя в Telegram",
                request_users=KeyboardButtonRequestUsers(
                    request_id=MANUAL_USER_REQUEST_ID,
                    user_is_bot=False,
                    max_quantity=1,
                    request_name=True,
                    request_username=True,
                ),
            )
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await msg.answer(
        "👤 Введите данные пользователя.\n\n"
        "С Telegram: <code>123456789 ivan</code>\n"
        "Без Telegram: <code>ivan</code> — только имя, без ID\n\n"
        "Или выберите человека кнопкой ниже — Telegram передаст настоящий ID автоматически.\n\n"
        "Для пользователя без Telegram доступ создаётся локально, "
        "а введённый ник станет именем клиента в 3x-ui.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await state.set_state(FSMStates.wait_for_manual_username)

@dp.message(F.users_shared, FSMStates.wait_for_manual_username)
async def manual_grant_from_shared_user(msg: Message, state: FSMContext):
    if msg.from_user.id not in config.ADMIN_IDS:
        await state.clear()
        return
    shared = msg.users_shared
    if shared is None or not shared.user_ids:
        await msg.answer("❌ Telegram не передал выбранного пользователя.")
        return
    tg_id = int(shared.user_ids[0])
    username = ""
    try:
        selected = shared.users[0] if shared.users else None
        username = str(getattr(selected, "username", "") or "").strip().lstrip("@")
        if not username:
            first_name = str(getattr(selected, "first_name", "") or "").strip()
            last_name = str(getattr(selected, "last_name", "") or "").strip()
            username = " ".join(x for x in (first_name, last_name) if x)
    except Exception:
        username = ""
    if not username:
        username = f"tg_{tg_id}"
    await state.clear()
    try:
        subscription = await asyncio.to_thread(
            ensure_subscription_sync,
            tg_id,
            username,
            SUBSCRIPTION_DAYS,
            config.DB_PATH,
        )
    except Exception as error:
        logger.exception("Ошибка выдачи по выбранному Telegram пользователю %s", tg_id)
        await msg.answer(
            f"❌ Не удалось создать или продлить подписку: <code>{html.escape(str(error)[:500])}</code>",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    full_sub_url = await asyncio.to_thread(current_subscription_url_sync, subscription.sub_id, fallback_base_url=str(getattr(config, "SUB_BASE_URL", "")))
    action = "создана" if subscription.created else "продлена"
    try:
        await asyncio.to_thread(
            user_events.safe_record_event,
            subscription.tg_id,
            username=subscription.username,
            direction="system",
            event_type="subscription_granted",
            text=f"Администратор выдал доступ на {SUBSCRIPTION_DAYS} дн.",
            actor=f"admin:{msg.from_user.id}",
            metadata={"expiry_time": subscription.expiry_time, "source": "telegram_user_picker"},
            db_path=config.DB_PATH,
        )
    except Exception:
        pass
    await msg.answer(
        f"✅ Подписка {action} для <b>@{html.escape(username)}</b>.\n"
        f"Telegram ID: <code>{tg_id}</code>\n"
        f"Ссылка подписки:\n<code>{full_sub_url}</code>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(FSMStates.wait_for_manual_username)
async def manual_grant_process(msg: Message, state: FSMContext):
    if msg.from_user.id not in config.ADMIN_IDS:
        await state.clear()
        return
    parts = (msg.text or "").strip().split(maxsplit=1)
    raw_first = parts[0] if parts else ""
    target_tg_id = 0
    target_username = ""
    try:
        parsed_id = int(raw_first)
        if parsed_id > 0:
            target_tg_id = parsed_id
            target_username = parts[1].strip().lstrip("@") if len(parts) > 1 else ""
        else:
            raise ValueError
    except (ValueError, IndexError):
        target_username = (msg.text or "").strip().lstrip("@")[:80]

    if not target_username:
        await msg.answer(
            "❌ Укажите ник. Примеры: <code>123456789 ivan</code> или просто <code>ivan</code>.",
            parse_mode="HTML",
        )
        return
    await state.clear()
    try:
        subscription = await asyncio.to_thread(
            ensure_subscription_sync,
            target_tg_id,
            target_username,
            SUBSCRIPTION_DAYS,
            config.DB_PATH,
        )
    except Exception as error:
        logger.exception("Ошибка ручной выдачи для Telegram ID %s", target_tg_id)
        await msg.answer(
            f"❌ Не удалось создать или продлить подписку: <code>{html.escape(str(error)[:500])}</code>",
            parse_mode="HTML",
            reply_markup=get_admin_menu(),
        )
        return

    full_sub_url = await asyncio.to_thread(current_subscription_url_sync, subscription.sub_id, fallback_base_url=str(getattr(config, "SUB_BASE_URL", "")))
    action = "создана" if subscription.created else "продлена"
    await asyncio.to_thread(
        user_events.safe_record_event,
        subscription.tg_id,
        username=subscription.username,
        direction="system",
        event_type="subscription_granted",
        text=f"Администратор выдал доступ на {SUBSCRIPTION_DAYS} дн.",
        actor=f"admin:{msg.from_user.id}",
        metadata={"expiry_time": subscription.expiry_time},
        db_path=config.DB_PATH,
    )
    delivery_note = ""
    if subscription.tg_id > 0:
        try:
            await bot.send_message(
                subscription.tg_id,
                "✅ Администратор активировал вашу VPN-подписку.\n\n"
                f"Ссылка подписки:\n<code>{full_sub_url}</code>",
                parse_mode="HTML",
            )
            delivery_note = "\n📨 Ссылка отправлена пользователю в Telegram."
        except Exception as error:
            logger.info("Не удалось отправить ручную подписку пользователю %s: %s", subscription.tg_id, error)
            delivery_note = "\n⚠️ Бот не смог написать пользователю; передайте ссылку вручную."
        target_label = f"TG ID <code>{subscription.tg_id}</code>"
    else:
        target_label = f"локальный пользователь <b>{html.escape(subscription.username)}</b>"
        delivery_note = "\nℹ️ Telegram-привязки нет, ссылку нужно передать пользователю вручную."
    await msg.answer(
        f"✅ Подписка {action} для {target_label}.\n"
        f"Ссылка подписки:\n<code>{full_sub_url}</code>{delivery_note}",
        parse_mode="HTML",
        reply_markup=get_admin_menu(),
    )

@dp.message(lambda m: m.text and "Выйти" in m.text)
async def back_to_user_menu(msg: Message):
    await send_home(msg, msg.from_user.id)

@dp.message(lambda m: m.text and "Подписки и уведомления" in m.text)
async def admin_subscriptions_report(msg: Message):
    if msg.from_user.id not in config.ADMIN_IDS:
        return
    await send_subscription_report(msg)

async def main():
    global bot
    token = str(getattr(config, "BOT_TOKEN", "") or "").strip()
    if not token:
        logger.error("BOT_TOKEN не задан. Telegram-бот не запускается.")
        return
    bot = JournalBot(token=token)
    await asyncio.to_thread(
        user_events.prune_events,
        keep_days=max(1, int(getattr(config, "USER_EVENT_KEEP_DAYS", 365))),
        max_rows=max(1_000, int(getattr(config, "USER_EVENT_MAX_ROWS", 250_000))),
        db_path=config.DB_PATH,
    )
    inbound_ids = await get_all_inbound_ids()
    if inbound_ids: logger.info(f"{config.SERVICE_NAME}: Связь с 3X-UI установлена! Доступно {len(inbound_ids)} инбаундов.")
    else: logger.warning("⚠ ВНИМАНИЕ: Список инбаундов пуст при старте.")
    await sync_panel_to_db()
    async def unread_admin_notification_loop():
        # Проверяем чаще, чем пользовательский интервал, чтобы изменение
        # настройки вступало в силу без лишней задержки. Само уведомление
        # отправляется не чаще указанного интервала.
        while True:
            await asyncio.sleep(60)
            if not _unread_notification_due():
                continue
            try:
                summary = await asyncio.to_thread(
                    user_events.unread_messages_summary, db_path=config.DB_PATH
                )
                total = int(summary.get("total") or 0)
                if total <= 0:
                    # Clear the previous fingerprint so the next genuinely new
                    # message starts a fresh notification cycle.
                    if _read_unread_notification_state().get("fingerprint"):
                        _clear_unread_notification_fingerprint()
                    continue
                items_all = list(summary.get("items") or [])
                fingerprint_parts = [
                    f"{int(item.get('tg_id') or 0)}:{int(item.get('count') or 0)}:{str(item.get('last_event_id') or '')}"
                    for item in items_all
                ]
                fingerprint = hashlib.sha256("|".join(sorted(fingerprint_parts)).encode("utf-8")).hexdigest()
                if not _unread_notification_due(fingerprint):
                    continue
                items = items_all[:20]
                lines = [
                    f"📨 Непрочитанные сообщения в панели: {total} от {int(summary.get('users') or 0)} пользователей.",
                    "",
                ]
                for item in items:
                    username = str(item.get("username") or "").strip()
                    who = f"@{username}" if username else f"TG ID {int(item.get('tg_id') or 0)}"
                    count = int(item.get("count") or 0)
                    preview = " ".join(str(item.get("preview") or "").split())[:140]
                    suffix = f": {preview}" if preview else ""
                    lines.append(f"• {who} — {count} непрочитанных{suffix}")
                if len(summary.get("items") or []) > len(items):
                    lines.append(f"… и ещё {len(summary['items']) - len(items)} пользователей.")
                lines.append("\nОткройте веб-панель → раздел «Сообщения».")
                text = "\n".join(lines)
                notification_markup = get_messages_notification_markup()
                admin_ids = list(getattr(config, "ADMIN_IDS", []))
                sent_count = 0
                for admin_id in admin_ids:
                    try:
                        await bot.send_message(int(admin_id), text, reply_markup=notification_markup)
                        sent_count += 1
                    except Exception as error:
                        logger.warning("Не удалось отправить уведомление админу %s: %s", admin_id, error)
                if admin_ids and sent_count == len(admin_ids):
                    _mark_unread_notification_sent(fingerprint)
            except Exception:
                logger.exception("Ошибка проверки уведомлений о непрочитанных сообщениях")

    async def auto_sync_loop():
        maintenance_runs = 0
        while True:
            await asyncio.sleep(max(300, int(getattr(config, "BOT_SYNC_INTERVAL_SECONDS", 3600))))
            await sync_panel_to_db()
            maintenance_runs += 1
            if maintenance_runs % 24 == 0:
                await asyncio.to_thread(
                    user_events.prune_events,
                    keep_days=max(1, int(getattr(config, "USER_EVENT_KEEP_DAYS", 365))),
                    max_rows=max(1_000, int(getattr(config, "USER_EVENT_MAX_ROWS", 250_000))),
                    db_path=config.DB_PATH,
                )
    asyncio.create_task(auto_sync_loop())
    asyncio.create_task(unread_admin_notification_loop())
    try:
        me = await bot.get_me()
        username = str(me.username or "").strip()
        if username:
            setattr(config, "BOT_USERNAME", username)
        # The persistent Telegram menu button must not launch a WebView.  The
        # ordinary bot keyboard issues a user-specific website link instead.
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception:
        logger.exception("Не удалось сбросить кнопку Telegram Mini App")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
