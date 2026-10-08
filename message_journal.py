"""Non-blocking asynchronous journal for Telegram API sends.

The main bot enqueues journal records and returns to aiogram immediately. A single
background task batches database writes in a worker thread. Detached HTTP workers
can use ``record_outgoing_sync`` when no asyncio loop exists.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from contextlib import contextmanager
from typing import Any

import config
import user_events
from message_sanitize import redact_message_text

logger = logging.getLogger(__name__)

_current_tg_id: contextvars.ContextVar[int] = contextvars.ContextVar("journal_tg_id", default=0)
_current_username: contextvars.ContextVar[str] = contextvars.ContextVar("journal_username", default="")
_current_kind: contextvars.ContextVar[str] = contextvars.ContextVar("journal_kind", default="message")
_suppressed: contextvars.ContextVar[bool] = contextvars.ContextVar("journal_suppressed", default=False)

_queue: asyncio.Queue[dict[str, Any]] | None = None
_worker_task: asyncio.Task[Any] | None = None
_fallback_tasks: set[asyncio.Task[Any]] = set()


def set_actor(tg_id: int, username: str = ""):
    return (_current_tg_id.set(int(tg_id or 0)), _current_username.set(str(username or "")[:100]))


def reset_actor(tokens: tuple[contextvars.Token[int], contextvars.Token[str]]) -> None:
    tg_token, user_token = tokens
    _current_tg_id.reset(tg_token)
    _current_username.reset(user_token)


def current_actor() -> tuple[int, str]:
    return int(_current_tg_id.get() or 0), str(_current_username.get() or "")


@contextmanager
def service_message():
    token = _current_kind.set("service")
    try:
        yield
    finally:
        _current_kind.reset(token)


@contextmanager
def suppress():
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


def journaling_suppressed() -> bool:
    return bool(_suppressed.get())


def _kind(explicit: str | None = None) -> str:
    value = str(explicit or _current_kind.get() or "message").strip().lower()
    return value if value in {"message", "service"} else "message"


def _status(success: bool, explicit: str | None = None) -> str:
    if explicit:
        value = str(explicit).strip().lower()
        if value in {"delivered", "failed", "received", "unknown"}:
            return value
    return "delivered" if success else "failed"


def _safe_error(error: object) -> str:
    return redact_message_text(str(error or ""))[:1200]


def make_entry(
    tg_id: int,
    *,
    username: str = "",
    direction: str = "out",
    event_type: str = "bot_message",
    text: object = "",
    actor: str = "telegram_bot",
    success: bool = True,
    metadata: Any = None,
    telegram_update_id: int | None = None,
    telegram_chat_id: int | None = None,
    telegram_message_id: int | None = None,
    message_kind: str | None = None,
    delivery_status: str | None = None,
    delivery_error: object = "",
) -> dict[str, Any] | None:
    try:
        normalized_tg = int(tg_id or 0)
    except (TypeError, ValueError):
        normalized_tg = 0
    if not normalized_tg:
        return None
    payload = dict(metadata or {})
    if delivery_error:
        payload.setdefault("delivery_error", _safe_error(delivery_error))
    return {
        "tg_id": normalized_tg,
        "username": str(username or "")[:100],
        "direction": direction if direction in {"in", "out", "system"} else "system",
        "event_type": str(event_type or "event")[:80],
        "text": text,
        "actor": str(actor or "telegram_bot")[:120],
        "success": bool(success),
        "metadata": payload or None,
        "telegram_update_id": telegram_update_id,
        "telegram_chat_id": telegram_chat_id,
        "telegram_message_id": telegram_message_id,
        "message_kind": _kind(message_kind),
        "delivery_status": _status(bool(success), delivery_status),
        "delivery_error": _safe_error(delivery_error),
    }


def enqueue(entry: dict[str, Any] | None) -> None:
    if entry is None or journaling_suppressed():
        return
    global _queue, _worker_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _queue is None:
        _queue = asyncio.Queue(maxsize=max(1000, int(getattr(config, "USER_EVENT_JOURNAL_QUEUE_SIZE", 5000))))
    if _worker_task is None or _worker_task.done():
        _worker_task = loop.create_task(_worker(), name="fargovpn-message-journal")
    try:
        _queue.put_nowait(entry)
    except asyncio.QueueFull:
        # Never block a Telegram handler just because the journal queue is full.
        # Persist the overflow row in a detached worker task instead of dropping it.
        task = loop.create_task(
            asyncio.to_thread(user_events.safe_record_event, **entry, db_path=config.DB_PATH),
            name="fargovpn-message-journal-overflow",
        )
        _fallback_tasks.add(task)
        task.add_done_callback(_fallback_tasks.discard)
        logger.warning("Журнал сообщений переполнен: строка tg_id=%s перенесена в overflow worker", entry.get("tg_id"))


def enqueue_outgoing(
    tg_id: int,
    *,
    username: str = "",
    event_type: str = "bot_message",
    text: object = "",
    actor: str = "telegram_bot",
    success: bool = True,
    metadata: Any = None,
    telegram_message_id: int | None = None,
    message_kind: str | None = None,
    delivery_status: str | None = None,
    delivery_error: object = "",
) -> None:
    entry = make_entry(
        tg_id,
        username=username,
        direction="out",
        event_type=event_type,
        text=text,
        actor=actor,
        success=success,
        metadata=metadata,
        telegram_chat_id=tg_id,
        telegram_message_id=telegram_message_id,
        message_kind=message_kind,
        delivery_status=delivery_status,
        delivery_error=delivery_error,
    )
    enqueue(entry)


def record_outgoing_sync(
    tg_id: int,
    *,
    username: str = "",
    event_type: str = "bot_message",
    text: object = "",
    actor: str = "telegram_bot",
    success: bool = True,
    metadata: Any = None,
    telegram_message_id: int | None = None,
    message_kind: str | None = None,
    delivery_status: str | None = None,
    delivery_error: object = "",
) -> int | None:
    entry = make_entry(
        tg_id,
        username=username,
        direction="out",
        event_type=event_type,
        text=text,
        actor=actor,
        success=success,
        metadata=metadata,
        telegram_chat_id=tg_id,
        telegram_message_id=telegram_message_id,
        message_kind=message_kind,
        delivery_status=delivery_status,
        delivery_error=delivery_error,
    )
    if entry is None:
        return None
    return user_events.safe_record_event(**entry, db_path=config.DB_PATH)


async def _worker() -> None:
    global _queue
    assert _queue is not None
    while True:
        first = await _queue.get()
        batch = [first]
        deadline = time.monotonic() + 0.25
        while len(batch) < 100 and time.monotonic() < deadline:
            try:
                batch.append(await asyncio.wait_for(_queue.get(), timeout=max(0.01, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                break
        try:
            last_error: Exception | None = None
            for attempt in range(4):
                try:
                    await asyncio.to_thread(user_events.record_events_batch, batch, config.DB_PATH)
                    last_error = None
                    break
                except Exception as error:
                    last_error = error
                    if attempt < 3:
                        await asyncio.sleep(0.5 * (2 ** attempt))
            if last_error is not None:
                # Preserve the non-blocking guarantee and make one final per-row
                # attempt. safe_record_event itself is best-effort and logs failures.
                logger.error("Пакетный журнал Telegram не записан после повторов: %s", last_error)
                for entry in batch:
                    task = asyncio.create_task(
                        asyncio.to_thread(user_events.safe_record_event, **entry, db_path=config.DB_PATH),
                        name="fargovpn-message-journal-retry",
                    )
                    _fallback_tasks.add(task)
                    task.add_done_callback(_fallback_tasks.discard)
        finally:
            for _ in batch:
                _queue.task_done()


async def flush() -> None:
    if _queue is not None:
        try:
            await asyncio.wait_for(_queue.join(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Журнал Telegram не успел полностью записаться при остановке")


async def shutdown() -> None:
    global _worker_task, _queue, _fallback_tasks
    await flush()
    if _fallback_tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*list(_fallback_tasks), return_exceptions=True), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Overflow-очередь журнала Telegram не успела завершить запись")
        _fallback_tasks.clear()
    task = _worker_task
    _worker_task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _queue = None


def _result_messages(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, (list, tuple)):
        return [item for item in result if getattr(item, "message_id", None) is not None]
    return [result] if getattr(result, "message_id", None) is not None else []


def _message_text(message: Any, fallback: object = "") -> str:
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    if text:
        return str(text)
    return str(fallback or "[Telegram сообщение]")


def describe_method(method: Any) -> tuple[int, str, str, str]:
    api = str(getattr(method, "__api_method__", "") or "")
    target = getattr(method, "chat_id", None)
    try:
        tg_id = int(target or 0)
    except (TypeError, ValueError):
        tg_id = 0
    text = getattr(method, "text", None) or getattr(method, "caption", None)
    if not text:
        media_labels = {
            "sendPhoto": "Фото", "sendDocument": "Документ", "sendVideo": "Видео",
            "sendAudio": "Аудио", "sendVoice": "Голосовое сообщение", "sendAnimation": "Анимация",
            "sendVideoNote": "Видеосообщение", "sendSticker": "Стикер", "sendLocation": "Локация",
            "sendVenue": "Место", "sendContact": "Контакт", "sendPoll": "Опрос", "sendDice": "Кубик",
            "sendPaidMedia": "Медиа", "sendGame": "Игра", "sendChecklist": "Чек-лист",
        }
        text = f"[{media_labels.get(api, 'Telegram сообщение')}]"
    kind = "service" if api in {"answerCallbackQuery", "editMessageText", "editMessageCaption", "editMessageMedia", "editMessageReplyMarkup"} else "message"
    return tg_id, str(text), api, kind
