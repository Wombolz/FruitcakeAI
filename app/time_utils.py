from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def resolve_effective_timezone(task_tz: Optional[str], user_tz: Optional[str]) -> str:
    for candidate in (task_tz, user_tz):
        name = (candidate or "").strip()
        if not name:
            continue
        try:
            ZoneInfo(name)
            return name
        except Exception:
            continue
    return "UTC"


def get_timezone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo((name or "").strip() or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def to_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def localize_datetime(value: Optional[datetime], timezone_name: Optional[str]) -> Optional[datetime]:
    normalized = to_utc(value)
    if normalized is None:
        return None
    return normalized.astimezone(get_timezone(timezone_name))


def format_localized_datetime(
    value: Optional[datetime],
    *,
    timezone_name: Optional[str],
    fmt: str = "%B %d, %Y %I:%M %p %Z",
) -> str:
    localized = localize_datetime(value, timezone_name)
    if localized is None:
        return ""
    return localized.strftime(fmt)


def utc_compact_timestamp(value: Optional[datetime]) -> str:
    normalized = to_utc(value) or datetime.now(timezone.utc)
    return normalized.strftime("%Y%m%dT%H%M%SZ")


def utc_day_folder(value: Optional[datetime]) -> str:
    normalized = to_utc(value) or datetime.now(timezone.utc)
    return normalized.strftime("%Y-%m-%d")
