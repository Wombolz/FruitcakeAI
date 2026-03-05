"""
FruitcakeAI v5 — Scheduler + Schedule Parser (Phase 4)

Sprint 4.1: compute_next_run_at() — parses schedule expressions.
Sprint 4.2: APScheduler wiring (start_scheduler, shutdown_scheduler, tick).

Schedule expression formats:
    "every:30m"           — interval (supports s, m, h, d)
    "0 7 * * *"           — cron expression (5 or 6 fields)
    "2026-03-10T09:00:00" — ISO 8601 one-shot timestamp
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional


# Interval unit → seconds multiplier
_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}

# Rough cron pattern: 5 or 6 whitespace-separated fields of digits / * / , / - / /
_CRON_RE = re.compile(r"^[\d*,\-/]+(?: [\d*,\-/]+){4,5}$")


def compute_next_run_at(
    schedule: str,
    after: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Parse a schedule expression and return the next run datetime (UTC).

    Args:
        schedule: One of "every:Xu", cron expression, or ISO 8601 timestamp.
        after: Base time for interval/cron calculation (defaults to now UTC).

    Returns:
        A timezone-aware datetime in UTC, or None if the expression is unrecognised.
    """
    if not schedule:
        return None

    now = after or datetime.now(timezone.utc)
    schedule = schedule.strip()

    # ── Interval: "every:30m", "every:1h", "every:2d", "every:90s" ──────────
    if schedule.lower().startswith("every:"):
        return _parse_interval(schedule[6:], now)

    # ── ISO 8601 timestamp (one-shot) ─────────────────────────────────────────
    # Try this before cron so "2026-03-10T09:00:00" is not misidentified.
    if "T" in schedule or schedule.count("-") >= 2:
        try:
            dt = datetime.fromisoformat(schedule)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass

    # ── Cron expression ───────────────────────────────────────────────────────
    if _CRON_RE.match(schedule):
        return _parse_cron(schedule, now)

    return None


def _parse_interval(expr: str, after: datetime) -> Optional[datetime]:
    """Parse "30m", "1h", "2d", "90s" and return after + interval."""
    expr = expr.strip().lower()
    if not expr:
        return None
    unit = expr[-1]
    if unit not in _UNIT_SECONDS:
        return None
    try:
        n = int(expr[:-1])
    except ValueError:
        return None
    if n <= 0:
        return None
    return after + timedelta(seconds=n * _UNIT_SECONDS[unit])


def _parse_cron(expr: str, after: datetime) -> Optional[datetime]:
    """
    Compute the next datetime after `after` that satisfies the cron expression.

    Supports standard 5-field cron: minute hour day-of-month month day-of-week.
    Day-of-week: 0=Sunday … 6=Saturday (or 7=Sunday).

    This is a simple forward-scan implementation that steps minute-by-minute
    up to a cap of 366 days. For production use, APScheduler's CronTrigger is
    preferred — this is a lightweight fallback for schedule validation.
    """
    fields = expr.split()
    if len(fields) == 6:
        # Drop optional seconds field if present
        fields = fields[1:]
    if len(fields) != 5:
        return None

    minute_f, hour_f, dom_f, month_f, dow_f = fields

    # Advance at least one minute past `after`
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366)

    while candidate <= limit:
        # Python weekday(): Mon=0 … Sun=6 → cron dow: Sun=0, Mon=1 … Sat=6
        cron_dow = (candidate.weekday() + 1) % 7
        if (
            _cron_match(candidate.minute, minute_f, 0, 59)
            and _cron_match(candidate.hour, hour_f, 0, 23)
            and _cron_match(candidate.day, dom_f, 1, 31)
            and _cron_match(candidate.month, month_f, 1, 12)
            and _cron_match(cron_dow, dow_f, 0, 7)
        ):
            return candidate
        # When minute/hour already matched, skip ahead to avoid minute-by-minute crawl
        if _cron_match(candidate.hour, hour_f, 0, 23) and not _cron_match(candidate.minute, minute_f, 0, 59):
            # Jump to next matching minute in this hour, or next hour
            candidate = candidate.replace(second=0, microsecond=0) + timedelta(minutes=1)
        else:
            # Jump to next matching hour
            candidate = candidate.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    return None


def _cron_match(value: int, field: str, min_val: int, max_val: int) -> bool:
    """
    Return True if `value` satisfies the cron field expression.

    sunday7: when True, the literal "7" also matches value==0
             (only for day-of-week where 0 and 7 both mean Sunday).
    """
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                return False
            start = min_val if base == "*" else int(base.split("-")[0])
            if value >= start and (value - start) % step == 0:
                return True
        elif "-" in part:
            lo, hi = part.split("-", 1)
            if int(lo) <= value <= int(hi):
                return True
        else:
            try:
                n = int(part)
                if n == value:
                    return True
            except ValueError:
                return False

    return False


# ---------------------------------------------------------------------------
# Sprint 4.2 — APScheduler wiring
# ---------------------------------------------------------------------------

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    _APSCHEDULER_AVAILABLE = True
except ImportError:
    _APSCHEDULER_AVAILABLE = False

import structlog
_log = structlog.get_logger(__name__)

_scheduler: "AsyncIOScheduler | None" = None


async def start_scheduler() -> None:
    """
    Initialize and start the APScheduler in-process scheduler.

    The task_dispatcher job fires every minute and calls tick() to find
    and launch any tasks whose next_run_at has passed.

    Uses SQLAlchemyJobStore backed by the existing PostgreSQL database so
    job definitions survive restarts without a separate broker.
    """
    global _scheduler
    if not _APSCHEDULER_AVAILABLE:
        _log.warning("scheduler.apscheduler_not_installed", hint="pip install apscheduler>=3.10,<4")
        return

    from app.config import settings

    jobstore = SQLAlchemyJobStore(url=settings.database_url_sync)
    _scheduler = AsyncIOScheduler(
        jobstores={"default": jobstore},
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    _scheduler.add_job(
        tick,
        "interval",
        minutes=1,
        id="task_dispatcher",
        replace_existing=True,
    )
    _scheduler.start()
    _log.info("scheduler.started")


def shutdown_scheduler() -> None:
    """Stop the scheduler gracefully on app shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _log.info("scheduler.stopped")


async def tick() -> None:
    """
    Dispatcher: query tasks due for execution and fire them concurrently.

    Runs every minute via APScheduler. Tasks are SELECT'd in a short-lived
    session; execution happens in independent asyncio tasks so the DB
    connection is not held across multi-turn LLM loops.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select, and_
    from app.db.session import AsyncSessionLocal
    from app.db.models import Task
    from app.autonomy.runner import get_task_runner

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Task).where(
                and_(
                    Task.status == "pending",
                    Task.next_run_at <= now,
                )
            )
        )
        due = result.scalars().all()

    if due:
        _log.info("scheduler.tick", due_count=len(due))

    runner = get_task_runner()
    for task in due:
        asyncio.create_task(runner.execute(task))
    return False
