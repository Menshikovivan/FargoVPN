#!/usr/bin/env python3
"""Send one subscription reminder per configured calendar-day bucket."""
from __future__ import annotations

import db as database_adapter

import asyncio
import fcntl
import html
import logging
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot

import config
from services import billing
import message_journal
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
    builder.button(text="📋 Моя подписка", callback_data="menu:stats")
    builder.adjust(1)
    return builder.as_markup()


def _record_reminder(tg_id: int, username: str, days: int, expiry: int) -> None:
    try:
        connection = database_adapter.connect(config.DB_PATH, timeout=20)
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


def _reminder_text(days: int, tg_id: int = 0) -> str:
    if days == 0:
        remaining = "Подписка заканчивается сегодня."
    elif days == 1:
        remaining = "До окончания подписки остался <b>1 день</b>."
    else:
        remaining = f"До окончания подписки осталось <b>{days} дн.</b>"
    price = billing.expected_payment_amount(int(tg_id), config.DB_PATH) if int(tg_id) > 0 else int(getattr(config, "PAYMENT_PRICE", 0) or 0)
    period = max(1, int(getattr(config, "SUBSCRIPTION_DAYS", 30) or 30))
    price_line = f"\n💳 Продление: <b>{price} ₽ / {period} дней</b>" if price > 0 else ""
    return (
        f"⏳ <b>{html.escape(str(config.SERVICE_NAME))}</b>\n\n{remaining}{price_line}\n"
        "Продлите доступ через кнопку ниже, чтобы подключение не прерывалось."
    )


def _reminder_report_line(tg_id: int, username: str, comment: str, days: int) -> str:
    """Render one admin report entry with the shared Telegram identity policy."""
    try:
        numeric_id = int(tg_id or 0)
    except (TypeError, ValueError, OverflowError):
        numeric_id = 0
    safe_username = str(username or "").strip().lstrip("@")
    if len(safe_username) > 32:
        safe_username = ""
    safe_comment = str(comment or "")
    if len(safe_comment) > 120:
        safe_comment = safe_comment[:120].rstrip() + "…"
    label = billing.telegram_identity_label(numeric_id, safe_username, "", safe_comment)
    safe_label = f"<b>{html.escape(str(label), quote=True)}</b>"
    if numeric_id > 0:
        safe_label = f'<a href="tg://user?id={numeric_id}">{safe_label}</a>'
    return f"{safe_label}: {int(days)} дн."


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _reminder_report_chunks(lines: list[str], max_units: int = 3500) -> list[str]:
    """Pack whole HTML lines into Telegram-safe messages with a header per part."""
    if not lines:
        return []
    header_template = f"📊 Отправлены напоминания (часть {len(lines)}/{len(lines)}):\n"
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        candidate = current + [line]
        if _utf16_units(header_template + "\n".join(candidate)) > max_units:
            if not current:
                raise ValueError("Строка напоминания превышает лимит Telegram")
            groups.append(current)
            current = [line]
            if _utf16_units(header_template + line) > max_units:
                raise ValueError("Строка напоминания превышает лимит Telegram")
        else:
            current = candidate
    if current:
        groups.append(current)
    total = len(groups)
    return [
        f"📊 Отправлены напоминания (часть {index}/{total}):\n" + "\n".join(group)
        for index, group in enumerate(groups, start=1)
    ]


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
    connection = database_adapter.connect(config.DB_PATH, timeout=20)
    try:
        rows = connection.execute(
            """SELECT tg_id,username,expiry_time,last_reminder_days,COALESCE(notes,'')
               FROM users
               WHERE tg_id>0 AND registration_status='active' AND enable=1 AND expiry_time> ?""",
            (now_ms,),
        ).fetchall()
        for tg_id, username, expiry, last_sent, notes in rows:
            days = reminder_day(int(expiry or 0), now_ms)
            if days is None or days not in configured or int(last_sent if last_sent is not None else -1) == days:
                continue
            try:
                reminder_text = _reminder_text(days, int(tg_id))
                delivered = await bot.send_message(int(tg_id), reminder_text, parse_mode="HTML", reply_markup=_reminder_markup())
                message_journal.record_outgoing_sync(
                    int(tg_id), username=str(username or ""), event_type="subscription_reminder", text=reminder_text,
                    actor="telegram_bot:reminders", success=delivered is not None, message_kind="service",
                    delivery_status="delivered" if delivered is not None else "failed",
                    telegram_message_id=int(getattr(delivered, "message_id", 0) or 0) or None,
                )
                if delivered is None:
                    raise RuntimeError("Telegram не подтвердил доставку напоминания")
                _record_reminder(int(tg_id), str(username or ""), int(days), int(expiry))
                connection.execute(
                    "UPDATE users SET last_reminder_days=? WHERE tg_id=?",
                    (days, int(tg_id)),
                )
                connection.commit()
                sent.append(_reminder_report_line(tg_id, username, notes, days))
            except Exception as error:
                logger.warning("Не удалось отправить напоминание %s: %s", tg_id, error)

        if sent:
            reports = _reminder_report_chunks(sent)
            for admin in getattr(config, "ADMIN_IDS", []):
                for report in reports:
                    try:
                        result = await bot.send_message(int(admin), report, parse_mode="HTML")
                        message_journal.record_outgoing_sync(
                            int(admin), event_type="reminder_report", text=report, actor="telegram_bot:reminders",
                            success=result is not None, message_kind="service",
                            delivery_status="delivered" if result is not None else "failed",
                            telegram_message_id=int(getattr(result, "message_id", 0) or 0) or None,
                        )
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
