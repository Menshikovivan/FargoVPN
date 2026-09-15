#!/usr/bin/env python3
"""Centralized timezone helpers for FargoVPN human-facing timestamps."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import config

DEFAULT_TIMEZONE = "Asia/Almaty"


def configured_timezone_name() -> str:
    raw = str(getattr(config, "WEB_TIMEZONE", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE).strip()
    if not raw:
        raw = DEFAULT_TIMEZONE
    return raw


def local_tz() -> ZoneInfo | timezone:
    try:
        return ZoneInfo(configured_timezone_name())
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=5))


def now_local() -> datetime:
    return datetime.now(tz=local_tz())


def from_timestamp(value: float | int) -> datetime:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(local_tz())


def local_date_from_timestamp(value: float | int):
    return from_timestamp(value).date()


def utc_sql_day_start_for_local(day: datetime | None = None) -> str:
    """Return naive UTC YYYY-MM-DD HH:MM:SS suitable for SQLite UTC text columns."""
    current = day.astimezone(local_tz()) if day and day.tzinfo else (day or now_local())
    local_midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_value = local_midnight.astimezone(timezone.utc)
    return utc_value.strftime("%Y-%m-%d %H:%M:%S")


def format_timestamp(value: float | int, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return from_timestamp(value).strftime(fmt)
