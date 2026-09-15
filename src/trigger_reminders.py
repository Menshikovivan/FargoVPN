#!/usr/bin/env python3
"""Send one subscription reminder per configured calendar-day bucket."""
from __future__ import annotations

import asyncio
import fcntl
import logging
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot

import config
import user_events
from services.xui_api import fetch_and_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vpn-service-reminders")
LOCK_PATH = Path(getattr(config, "REMINDER_LOCK_PATH", "/run/vpn-service-reminders.lock"))


def reminder_day(expiry_ms: int, now_ms: int | None = None) -> int | None:
    """Return calendar days until expiry; expired subscriptions return None."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    expiry_ms = int(expiry_ms or 0)
    if expiry_ms <= now_ms:
        return None
    expiry_date = datetime.fromtimestamp(expiry_ms / 1000).date()
    today = datetime.fromtimestamp(now_ms / 1000).date()
    return max(0, (expiry_date - today).days)


def _reminder_markup():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Продлить подписку", callback_data="menu:buy")
    builder.button(text="📊 Моя статистика", callback_data="menu:stats")
    builder.adjust(1)
    return builder.as_markup()


def _record_reminder(tg_id: int, username: str, days: int, expiry: int) -> None:
    try:
        connection = sqlite3.connect(config.DB_PATH, timeout=20)
        try:
            connection.execute(
                "INSERT INTO audit_log(actor,action,details) VALUES(?,?,?)",
                ("system:reminders", "reminder_sent", json.dumps({"tg_id": int(tg_id), "username": username or "", "days": int(days), "expiry_time": int(expiry)}, ensure_ascii=False)),
            )
            connection.commit()
        finally:
            connection.close()
    except Exception as error:
        logger.debug("Не удалось записать reminder_sent в audit_log: %s", error)


def _reminder_text(days: int) -> str:
    if days == 0:
        remaining = "Подписка заканчивается сегодня."
    elif days == 1:
        remaining = "До окончания подписки остался <b>1 день</b>."
    else:
        remaining = f"До окончания подписки осталось <b>{days} дн.</b>"
    price = int(getattr(config, "PAYMENT_PRICE", 0) or 0)
    period = max(1, int(getattr(config, "SUBSCRIPTION_DAYS", 30) or 30))
    price_line = f"\n💳 Продление: <b>{price} ₽ / {period} дней</b>" if price > 0 else ""
    return (
        f"⏳ <b>{config.SERVICE_NAME}</b>\n\n{remaining}{price_line}\n"
        "Продлите доступ через кнопку ниже, чтобы подключение не прерывалось."
    )


async def run() -> int:
    configured = sorted({int(day) for day in getattr(config, "REMINDER_DAYS", [7, 3, 1, 0]) if int(day) >= 0}, reverse=True)
    snapshot = await asyncio.to_thread(fetch_and_sync, True, config.DB_PATH)
    if snapshot.get("stale"):
        logger.warning(
            "Напоминания пропущены: не удалось подтвердить актуальные данные 3x-ui: %s",
            snapshot.get("error") or "неизвестная ошибка",
        )
        return 0
    bot = Bot(config.BOT_TOKEN)
    sent: list[str] = []
    now_ms = int(time.time() * 1000)
    connection = sqlite3.connect(config.DB_PATH, timeout=20)
    try:
        rows = connection.execute(
            """SELECT tg_id,username,expiry_time,last_reminder_days
               FROM users
               WHERE tg_id>0 AND enable=1 AND expiry_time> ?""",
            (now_ms,),
        ).fetchall()
        for tg_id, username, expiry, last_sent in rows:
            days = reminder_day(int(expiry or 0), now_ms)
            if days is None or days not in configured or int(last_sent if last_sent is not None else -1) == days:
                continue
            try:
                reminder_text = _reminder_text(days)
                await bot.send_message(int(tg_id), reminder_text, parse_mode="HTML", reply_markup=_reminder_markup())
                user_events.safe_record_event(
                    int(tg_id),
                    username=str(username or ""),
                    direction="out",
                    event_type="bot_message",
                    text=reminder_text,
                    actor="telegram_bot:reminders",
                    db_path=config.DB_PATH,
                )
                _record_reminder(int(tg_id), str(username or ""), int(days), int(expiry))
                connection.execute(
                    "UPDATE users SET last_reminder_days=? WHERE tg_id=?",
                    (days, int(tg_id)),
                )
                connection.commit()
                sent.append(f"{username or tg_id}: {days}")
            except Exception as error:
                logger.warning("Не удалось отправить напоминание %s: %s", tg_id, error)

        if sent:
            report = "📊 Отправлены напоминания:\n" + "\n".join(sent)
            for admin in getattr(config, "ADMIN_IDS", []):
                try:
                    await bot.send_message(int(admin), report)
                except Exception as error:
                    logger.warning("Не удалось отправить отчёт администратору %s: %s", admin, error)
        return len(sent)
    finally:
        connection.close()
        await bot.session.close()


def main() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Проверка напоминаний уже выполняется; повторный запуск пропущен")
            return 0
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
