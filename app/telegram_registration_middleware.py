"""Global Telegram invitation gate for FargoVPN.

The middleware runs at Update level, before all message/callback/inline/etc.
handlers. Only authenticated admins bypass it. All ordinary Telegram users
must be in users.registration_status='active' to reach any handler.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter

import config
import registration_access
import message_journal

logger = logging.getLogger(__name__)

INVITE_PROMPT = (
    "🔐 <b>Регистрация по приглашению</b>\n\n"
    "Введите 4-значный код приглашения, который вам передал уже зарегистрированный пользователь."
)


def _parts(update: Any) -> list[Any]:
    names = (
        "message", "edited_message", "channel_post", "edited_channel_post",
        "business_message", "edited_business_message", "callback_query",
        "inline_query", "chosen_inline_result", "my_chat_member", "chat_member",
        "chat_join_request", "shipping_query", "pre_checkout_query", "poll_answer",
        "business_connection", "business_messages_deleted",
    )
    return [getattr(update, name, None) for name in names if getattr(update, name, None) is not None]


def update_actor(update: Any) -> tuple[Any | None, Any | None, Any | None]:
    """Return (user, chat, source_event) for a Telegram Update-like object."""
    for part in _parts(update):
        user = getattr(part, "from_user", None) or getattr(part, "user", None)
        chat = getattr(part, "chat", None)
        if user is not None:
            return user, chat, part
    return None, None, None


def is_private_source(source: Any, chat: Any) -> bool:
    if chat is not None:
        return str(getattr(chat, "type", "") or "") == "private"
    # Inline/pre-checkout/shipping queries do not expose a chat but originate
    # from the user's private Telegram session.
    return source is not None and any(
        source.__class__.__name__ == name
        for name in ("InlineQuery", "ChosenInlineResult", "PreCheckoutQuery", "ShippingQuery", "PollAnswer", "BusinessConnection")
    )


def invite_code_message(source: Any) -> bool:
    if source is None:
        return False
    return registration_access.normalize_invitation_code(getattr(source, "text", None)) is not None


def prompt_chat_id(update: Any, user: Any, chat: Any, source: Any) -> int | None:
    if chat is not None and str(getattr(chat, "type", "") or "") == "private":
        try:
            return int(chat.id)
        except (TypeError, ValueError):
            return None
    source_name = source.__class__.__name__ if source is not None else ""
    if source_name in {"InlineQuery", "ChosenInlineResult", "CallbackQuery", "PreCheckoutQuery", "ShippingQuery", "PollAnswer", "BusinessConnection"}:
        try:
            return int(getattr(user, "id", 0) or 0) or None
        except (TypeError, ValueError):
            return None
    return None


async def _answer_callback(source: Any) -> None:
    if source is None or source.__class__.__name__ != "CallbackQuery":
        return
    try:
        await source.answer()
    except TelegramBadRequest as error:
        logger.debug("Callback answer skipped during registration gate: %s", error)
    except Exception as error:
        logger.debug("Callback answer failed during registration gate: %s", error)


async def send_registration_prompt(bot: Any, chat_id: int, *, blocked_until: int = 0) -> bool:
    if int(blocked_until or 0) > int(time.time()):
        remaining = max(1, int(blocked_until) - int(time.time()))
        minutes = max(1, (remaining + 59) // 60)
        text = f"⏳ Слишком много неверных кодов. Попробуйте снова примерно через {minutes} мин."
    else:
        text = INVITE_PROMPT
    for attempt in range(3):
        try:
            with message_journal.service_message():
                await bot.send_message(int(chat_id), text, parse_mode="HTML")
            return True
        except TelegramRetryAfter as error:
            delay = max(1.0, min(8.0, float(getattr(error, "retry_after", 1) or 1)))
            if attempt == 2:
                break
            await asyncio.sleep(delay)
        except TelegramForbiddenError as error:
            logger.warning("Registration prompt forbidden for chat %s: %s", chat_id, error)
            return False
        except TelegramBadRequest as error:
            logger.warning("Registration prompt rejected for chat %s: %s", chat_id, error)
            return False
        except Exception as error:
            logger.warning("Registration prompt failed chat=%s attempt=%s: %s", chat_id, attempt + 1, error)
            if attempt == 2:
                break
            await asyncio.sleep(0.5 * (attempt + 1))
    return False


class PendingInviteCodeFilter(BaseFilter):
    async def __call__(self, message: Any) -> bool:
        if not invite_code_message(message):
            return False
        user = getattr(message, "from_user", None)
        tg_id = int(getattr(user, "id", 0) or 0)
        if tg_id <= 0:
            return False
        state = await asyncio.to_thread(registration_access.registration_state_sync, tg_id)
        return bool(state and state.status == registration_access.AWAITING_INVITE and state.blocked_until <= int(time.time()))


class InvitationAccessMiddleware(BaseMiddleware):
    """Deny-by-default access gate executed for every Telegram Update."""

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user, chat, source = update_actor(event)
        if user is None:
            return await handler(event, data)
        try:
            tg_id = int(getattr(user, "id", 0) or 0)
        except (TypeError, ValueError):
            return await handler(event, data)
        if tg_id <= 0:
            return await handler(event, data)

        admins = {int(value) for value in getattr(config, "ADMIN_IDS", []) if str(value).lstrip("-").isdigit()}
        if tg_id in admins:
            return await handler(event, data)

        state = await asyncio.to_thread(
            registration_access.ensure_user_registration_state_sync,
            tg_id,
            getattr(user, "username", None),
            getattr(user, "full_name", None),
        )
        data["registration_state"] = state
        data["registration_status"] = state.status

        if state.status == registration_access.ACTIVE:
            return await handler(event, data)

        if state.status == registration_access.BANNED:
            target = prompt_chat_id(event, user, chat, source)
            await _answer_callback(source)
            if target:
                await send_registration_prompt(data.get("bot"), target, blocked_until=max(state.blocked_until, int(time.time()) + 365 * 24 * 3600))
            logger.info("Blocked Telegram update from banned tg_id=%s event=%s", tg_id, type(source).__name__)
            return None

        now = int(time.time())
        if state.blocked_until > now:
            target = prompt_chat_id(event, user, chat, source)
            await _answer_callback(source)
            if target:
                await send_registration_prompt(data.get("bot"), target, blocked_until=state.blocked_until)
            return None

        # The only ordinary-user update allowed through the gate is a plain
        # 4-digit message. Deep links, callbacks, media, commands, inline
        # queries and all other update types remain blocked until activation.
        if invite_code_message(source) and is_private_source(source, chat):
            return await handler(event, data)

        target = prompt_chat_id(event, user, chat, source)
        await _answer_callback(source)
        if target and data.get("bot") is not None:
            sent = await send_registration_prompt(data["bot"], target)
            if not sent:
                logger.error("Registration prompt could not be delivered tg_id=%s; state remains awaiting_invite", tg_id)
        else:
            logger.info("Telegram update blocked without prompt target tg_id=%s event=%s", tg_id, type(source).__name__)
        return None
