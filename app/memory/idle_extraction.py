"""Idle-session memory extraction.

A chat session that has gone quiet for `memory_idle_minutes` is extracted
once, shortly after the conversation ends, instead of waiting for the
nightly pass. Each session carries a high-water mark
(`ChatSession.memory_extracted_at`), so a session that resumes is only
extracted from its new messages, and the nightly job only picks up what
this one missed.

Runs from the scheduler every `memory_idle_check_minutes`. It stays out of
the way of live chat: it skips a tick while any chat turn is running, and
processes at most `memory_idle_max_sessions_per_tick` sessions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import and_, func, or_, select

from app.config import settings
from app.db.models import ChatMessage, ChatRun, ChatSession
from app.memory.extraction import MIN_USER_CHARS, extraction_totals_unusable, run_memory_extraction_for_user

log = structlog.get_logger(__name__)

# idle sessions older than this are left to the nightly job's window
MAX_SESSION_AGE_DAYS = 3


async def find_idle_sessions(db, *, now: datetime | None = None) -> list[tuple[int, int]]:
    """(user_id, session_id) pairs with unextracted activity that has gone
    quiet, oldest first."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.memory_idle_minutes)
    floor = now - timedelta(days=MAX_SESSION_AGE_DAYS)
    last_at = func.max(ChatMessage.created_at).label("last_at")
    rows = await db.execute(
        select(ChatSession.user_id, ChatSession.id, last_at)
        .join(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(
            and_(
                ChatSession.is_incognito == False,
                ChatSession.is_task_session == False,
                ChatMessage.role.in_(["user", "assistant"]),
                or_(
                    ChatSession.memory_extracted_at.is_(None),
                    ChatMessage.created_at > ChatSession.memory_extracted_at,
                ),
            )
        )
        .group_by(ChatSession.user_id, ChatSession.id)
        .having(and_(last_at <= cutoff, last_at >= floor))
        .order_by(last_at.asc())
        .limit(settings.memory_idle_max_sessions_per_tick)
    )
    return [(int(user_id), int(session_id)) for user_id, session_id, _ in rows.all()]


async def _chat_turn_running(db, now: datetime) -> bool:
    # a run that crashed can stay "running" forever; only recent ones count
    row = await db.execute(
        select(ChatRun.id)
        .where(ChatRun.status == "running", ChatRun.updated_at >= now - timedelta(minutes=15))
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


async def run_idle_memory_extraction(*, now: datetime | None = None) -> dict[str, int]:
    """One scheduler tick. Returns totals (zeros when nothing to do)."""
    from app.db.session import AsyncSessionLocal

    totals: dict[str, int] = defaultdict(int)
    if not settings.memory_idle_extraction_enabled:
        return dict(totals)

    now = now or datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        if await _chat_turn_running(db, now):
            log.debug("memory.idle_extraction_deferred", reason="chat_turn_running")
            return dict(totals)
        pairs = await find_idle_sessions(db, now=now)
        if not pairs:
            return dict(totals)

        by_user: dict[int, list[int]] = defaultdict(list)
        for user_id, session_id in pairs:
            by_user[user_id].append(session_id)

        for user_id, session_ids in by_user.items():
            try:
                stats = await run_memory_extraction_for_user(
                    db,
                    user_id,
                    since_hours=MAX_SESSION_AGE_DAYS * 24,
                    session_ids=session_ids,
                    respect_markers=True,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                log.warning("memory.idle_extraction_failed", user_id=user_id, exc_info=True)
                continue
            for key, value in stats.items():
                totals[key] += int(value)
            totals["users"] += 1

    unusable = extraction_totals_unusable(dict(totals))
    if unusable:
        log.warning("memory.idle_extraction_unusable", reason=unusable)
    if totals:
        log.info("memory.idle_extraction_completed", min_user_chars=MIN_USER_CHARS, **dict(totals))
    return dict(totals)
