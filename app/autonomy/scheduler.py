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

import httpx
from app.time_utils import get_timezone, to_utc

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
    timezone_name: Optional[str] = None,
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

    now = to_utc(after) or datetime.now(timezone.utc)
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
                dt = dt.replace(tzinfo=get_timezone(timezone_name))
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    # ── Cron expression ───────────────────────────────────────────────────────
    if _CRON_RE.match(schedule):
        return _parse_cron(schedule, now, timezone_name=timezone_name)

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


def _parse_cron(expr: str, after: datetime, *, timezone_name: Optional[str]) -> Optional[datetime]:
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

    local_tz = get_timezone(timezone_name)
    local_after = after.astimezone(local_tz)
    # Advance at least one minute past `after` in the intended local timezone.
    candidate = local_after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = local_after + timedelta(days=366)

    while candidate <= limit:
        # Python weekday(): Mon=0 … Sun=6 → cron dow: Sun=0, Mon=1 … Sat=6
        cron_dow = (candidate.weekday() + 1) % 7
        if (
            _cron_match(candidate.minute, minute_f, 0, 59)
            and _cron_match(candidate.hour, hour_f, 0, 23)
            and _cron_match(candidate.day, dom_f, 1, 31)
            and _cron_match(candidate.month, month_f, 1, 12)
            and _cron_match(cron_dow, dow_f, 0, 7, sunday7=True)
        ):
            return candidate.astimezone(timezone.utc)
        # When minute/hour already matched, skip ahead to avoid minute-by-minute crawl
        if _cron_match(candidate.hour, hour_f, 0, 23) and not _cron_match(candidate.minute, minute_f, 0, 59):
            # Jump to next matching minute in this hour, or next hour
            candidate = candidate.replace(second=0, microsecond=0) + timedelta(minutes=1)
        else:
            # Jump to next matching hour
            candidate = candidate.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    return None


def _cron_match(value: int, field: str, min_val: int, max_val: int, sunday7: bool = False) -> bool:
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
            if base == "*":
                start, end = min_val, max_val
            elif "-" in base:
                parts = base.split("-", 1)
                start, end = int(parts[0]), int(parts[1])
            else:
                start, end = int(base), max_val
            if start <= value <= end and (value - start) % step == 0:
                return True
        elif "-" in part:
            lo, hi = part.split("-", 1)
            lo_i, hi_i = int(lo), int(hi)
            # Normalise Sunday alias in range bounds before comparing
            if sunday7 and value == 0:
                if lo_i <= 7 <= hi_i or lo_i <= 0 <= hi_i:
                    return True
            elif int(lo) <= value <= int(hi):
                return True
        else:
            try:
                n = int(part)
                # Treat 7 as Sunday (0) when sunday7 is set
                if sunday7 and n == 7:
                    n = 0
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
_llm_unhealthy_until: datetime | None = None
_llm_last_error: str | None = None
_last_dispatch_at: datetime | None = None


def _as_utc_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
    recovered = await recover_stale_running_tasks()
    if recovered:
        _log.info("scheduler.recovered_stale_running", recovered=recovered)
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
    from app.db.models import Task, User
    from app.autonomy.runner import get_task_runner

    global _last_dispatch_at
    now = datetime.now(timezone.utc)
    _last_dispatch_at = now
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

    if due and not await _llm_dispatch_allowed(now):
        recurring_skipped = await _skip_recurring_backlog(due, now)
        if recurring_skipped:
            _log.info("scheduler.recurring_backlog_skipped", count=recurring_skipped)
        return False

    runner = get_task_runner()
    for task in due:
        asyncio.create_task(runner.execute(task))
    return False


async def _llm_dispatch_allowed(now: datetime) -> bool:
    """Return True when scheduler can dispatch tasks to the agent loop."""
    allowed, _, _ = await check_and_mark_llm_dispatch_health(now=now)
    return allowed


async def _is_llm_available() -> tuple[bool, str]:
    """Quick health probe for local/openai_compat LLM backends."""
    from app.config import settings

    if settings.llm_backend not in ("ollama", "openai_compat"):
        return True, "non_local_backend"

    base = settings.local_api_base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]

    try:
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(base + "/api/tags")
        if resp.status_code >= 400:
            return False, f"http_{resp.status_code}"
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def get_llm_dispatch_health(now: datetime | None = None) -> dict[str, object]:
    """
    Return scheduler LLM gate health state for diagnostics/admin endpoints.
    """
    now_utc = now or datetime.now(timezone.utc)
    unhealthy_until = _as_utc_aware(_llm_unhealthy_until)
    blocked = unhealthy_until is not None and now_utc < unhealthy_until
    return {
        "blocked": blocked,
        "unhealthy_until": unhealthy_until.isoformat() if unhealthy_until else None,
        "last_error": _llm_last_error,
        "last_dispatch_at": _as_utc_aware(_last_dispatch_at).isoformat() if _last_dispatch_at else None,
    }


async def check_and_mark_llm_dispatch_health(
    now: datetime | None = None,
    *,
    probe: bool = True,
) -> tuple[bool, int, str]:
    """
    Shared LLM gate check for scheduler and runner.

    Returns:
        (allowed, cooldown_seconds, reason)
    """
    from app.config import settings
    from app.metrics import metrics

    global _llm_unhealthy_until, _llm_last_error

    now_utc = now or datetime.now(timezone.utc)
    cooldown = max(30, int(settings.scheduler_unhealthy_cooldown_seconds or 300))

    if not settings.scheduler_llm_health_gate_enabled:
        _llm_last_error = None
        return True, cooldown, "disabled"

    if _llm_unhealthy_until is not None and now_utc < _llm_unhealthy_until:
        metrics.inc_scheduler_llm_unavailable_ticks()
        metrics.inc_scheduler_dispatch_suppressed_count()
        reason = _llm_last_error or "cooldown_active"
        return False, cooldown, str(reason)

    if not probe:
        _llm_last_error = None
        return True, cooldown, "probe_skipped"

    probe_result = await _is_llm_available()
    if isinstance(probe_result, tuple):
        ok, detail = probe_result
    else:
        # Backward compatibility for tests monkeypatching `_is_llm_available`
        ok = bool(probe_result)
        detail = "ok" if ok else "unavailable"
    if ok:
        _llm_unhealthy_until = None
        _llm_last_error = None
        return True, cooldown, "ok"

    _llm_unhealthy_until = now_utc + timedelta(seconds=cooldown)
    _llm_last_error = detail
    metrics.inc_scheduler_llm_unavailable_ticks()
    metrics.inc_scheduler_dispatch_suppressed_count()
    _log.warning("scheduler.llm_unavailable", cooldown_seconds=cooldown, detail=detail)
    return False, cooldown, str(detail or "unavailable")


async def _skip_recurring_backlog(due: list, now: datetime) -> int:
    """
    Skip overdue recurring intervals while LLM is unavailable.
    One-shot tasks remain due/pending and are not modified.
    """
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.db.models import Task, User
    from app.metrics import metrics

    recurring_ids = [
        t.id
        for t in due
        if getattr(t, "task_type", None) == "recurring" and bool(getattr(t, "schedule", None))
    ]
    if not recurring_ids:
        return 0

    skipped = 0
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(Task).where(Task.id.in_(recurring_ids)))
        for task in rows.scalars().all():
            user_tz = None
            if getattr(task, "user_id", None) is not None:
                user = await db.get(User, task.user_id)
                user_tz = getattr(user, "active_hours_tz", None) if user is not None else None
            nxt = None
            if task.schedule:
                from app.task_service import compute_next_run_at as task_compute_next_run_at

                nxt = task_compute_next_run_at(
                    task.schedule,
                    after=now,
                    task_timezone=getattr(task, "active_hours_tz", None),
                    user_timezone=user_tz,
                )
            task.next_run_at = nxt or (now + timedelta(minutes=1))
            skipped += 1
        await db.commit()

    metrics.inc_scheduler_recurring_backlog_skipped_count(skipped)
    return skipped


async def recover_stale_running_tasks() -> int:
    """
    Recover tasks left in `running` after host sleep/crash/restart.

    Stale tasks are re-queued (`pending`, `next_run_at=now`) and latest
    running TaskRun is closed as failed with a deterministic error string.
    """
    from sqlalchemy import select, and_
    from app.config import settings
    from app.db.session import AsyncSessionLocal
    from app.db.models import Task, TaskRun, TaskStep
    from app.metrics import metrics

    age_minutes = int(settings.scheduler_stale_running_recovery_minutes or 0)
    if age_minutes <= 0:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=age_minutes)
    recovered = 0

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(Task).where(Task.status == "running")
        )
        tasks = rows.scalars().all()

        for task in tasks:
            last_run_at = _as_utc_aware(task.last_run_at)
            if last_run_at is not None and last_run_at > cutoff:
                continue
            run_rows = await db.execute(
                select(TaskRun)
                .where(TaskRun.task_id == task.id, TaskRun.status == "running")
                .order_by(TaskRun.started_at.desc())
            )
            run = run_rows.scalars().first()
            if run is None:
                continue
            run_started = _as_utc_aware(run.started_at)
            if run_started and run_started > cutoff:
                continue

            task.status = "pending"
            task.next_run_at = now
            task.error = "Recovered stale running task after restart/sleep."

            if task.current_step_index is not None:
                step_rows = await db.execute(
                    select(TaskStep).where(
                        TaskStep.task_id == task.id,
                        TaskStep.step_index == task.current_step_index,
                    )
                )
                step = step_rows.scalars().first()
                if step is not None and step.status == "running":
                    step.status = "pending"
                    step.error = "Recovered stale running step after restart/sleep."
                    step.waiting_approval_tool = None

            run.status = "failed"
            run.finished_at = now
            run.error = "recovered_after_restart_or_sleep"
            recovered += 1

        if recovered:
            await db.commit()

    if recovered:
        metrics.inc_scheduler_stale_running_recovered_count(recovered)
    return recovered
