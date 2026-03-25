from __future__ import annotations

from app.autonomy.profiles.default import DefaultTaskExecutionProfile
from app.autonomy.profiles.morning_briefing import MorningBriefingExecutionProfile
from app.autonomy.profiles.news_magazine import NewsMagazineExecutionProfile
from app.autonomy.profiles.topic_watcher import TopicWatcherExecutionProfile

_RSS_NEWSPAPER_ALIASES = {"rss_newspaper", "news_magazine"}
ALLOWED_TASK_PROFILES = {
    "default",
    "rss_newspaper",
    "news_magazine",
    "morning_briefing",
    "topic_watcher",
}


def resolve_task_profile(task, user=None):
    value = (getattr(task, "profile", None) or "").strip().lower()
    return resolve_task_profile_by_name(value)


def resolve_task_profile_by_name(value: str | None):
    value = (value or "").strip().lower()
    if value in _RSS_NEWSPAPER_ALIASES:
        return NewsMagazineExecutionProfile()
    if value == "morning_briefing":
        return MorningBriefingExecutionProfile()
    if value == "topic_watcher":
        return TopicWatcherExecutionProfile()
    return DefaultTaskExecutionProfile()


def normalize_task_profile(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v not in ALLOWED_TASK_PROFILES:
        raise ValueError(f"Unknown profile '{value}'. Allowed: default, rss_newspaper, news_magazine")
    if v in _RSS_NEWSPAPER_ALIASES:
        return "rss_newspaper"
    return v
