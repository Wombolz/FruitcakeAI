from __future__ import annotations

from app.autonomy.profiles.default import DefaultTaskExecutionProfile
from app.autonomy.profiles.news_magazine import NewsMagazineExecutionProfile

ALLOWED_TASK_PROFILES = {"default", "news_magazine"}


def resolve_task_profile(task, user=None):
    value = (getattr(task, "profile", None) or "").strip().lower()
    return resolve_task_profile_by_name(value)


def resolve_task_profile_by_name(value: str | None):
    value = (value or "").strip().lower()
    if value == "news_magazine":
        return NewsMagazineExecutionProfile()
    return DefaultTaskExecutionProfile()


def normalize_task_profile(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v not in ALLOWED_TASK_PROFILES:
        raise ValueError(f"Unknown profile '{value}'. Allowed: default, news_magazine")
    return v
