from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def is_valid_timezone_name(name: Optional[str]) -> bool:
    candidate = (name or "").strip()
    if not candidate:
        return False
    try:
        ZoneInfo(candidate)
        return True
    except Exception:
        return False


def resolve_effective_timezone(task_tz: Optional[str], user_tz: Optional[str]) -> str:
    for candidate in (task_tz, user_tz):
        name = (candidate or "").strip()
        if is_valid_timezone_name(name):
            return name
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


def format_localized_iso_datetime(
    value: Optional[datetime],
    *,
    timezone_name: Optional[str],
) -> str:
    localized = localize_datetime(value, timezone_name)
    if localized is None:
        return ""
    return localized.isoformat()


def format_local_and_utc_pair(
    value: Optional[datetime],
    *,
    timezone_name: Optional[str],
    local_fmt: str = "%Y-%m-%d %I:%M %p %Z",
) -> tuple[str, str]:
    normalized = to_utc(value)
    if normalized is None:
        return "", ""
    localized = localize_datetime(normalized, timezone_name)
    return (
        localized.strftime(local_fmt) if localized is not None else "",
        normalized.astimezone(timezone.utc).isoformat(),
    )


def utc_compact_timestamp(value: Optional[datetime]) -> str:
    normalized = to_utc(value) or datetime.now(timezone.utc)
    return normalized.strftime("%Y%m%dT%H%M%SZ")


def utc_day_folder(value: Optional[datetime]) -> str:
    normalized = to_utc(value) or datetime.now(timezone.utc)
    return normalized.strftime("%Y-%m-%d")
