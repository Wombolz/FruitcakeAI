from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.persona_loader import list_personas, persona_exists
from app.agent.persona_router import infer_persona_for_task
from app.autonomy.configured_executor import infer_configured_executor
from app.autonomy.profiles import normalize_task_profile
from app.db.models import Task
from app.time_utils import resolve_effective_timezone


class TaskValidationError(ValueError):
    pass


UNSET = object()


def compute_next_run_at(
    schedule: str | None,
    *,
    task_timezone: Optional[str] = None,
    user_timezone: Optional[str] = None,
    after: datetime | None = None,
) -> datetime | None:
    if not schedule:
        return None
    try:
        from app.autonomy.scheduler import compute_next_run_at as _compute

        return _compute(
            schedule,
            after=after,
            timezone_name=resolve_effective_timezone(task_timezone, user_timezone),
        )
    except ImportError:
        return _simple_next_run(schedule, after=after)


def effective_task_timezone(*, task_timezone: Optional[str], user_timezone: Optional[str]) -> str:
    return resolve_effective_timezone(task_timezone, user_timezone)


def _simple_next_run(schedule: str, *, after: datetime | None = None) -> datetime | None:
    now = after or datetime.now(timezone.utc)
    if schedule.startswith("every:"):
        expr = schedule[6:].strip()
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        unit = expr[-1].lower()
        if unit in multipliers:
            try:
                n = int(expr[:-1])
                from datetime import timedelta

                return now + timedelta(seconds=n * multipliers[unit])
            except ValueError:
                pass
    try:
        dt = datetime.fromisoformat(schedule)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def resolve_task_persona(*, title: str, instruction: str, requested_persona: Optional[str]) -> str:
    explicit = (requested_persona or "").strip()
    if explicit:
        if not persona_exists(explicit):
            available = ", ".join(list_personas().keys())
            raise TaskValidationError(f"Unknown persona '{explicit}'. Available: {available}")
        return explicit

    inferred, _, _ = infer_persona_for_task(title, instruction)
    return inferred


def resolve_task_profile(requested_profile: Optional[str]) -> Optional[str]:
    try:
        return normalize_task_profile(requested_profile)
    except ValueError as exc:
        raise TaskValidationError(str(exc)) from exc


def infer_task_profile(
    *,
    title: str,
    instruction: str,
    task_type: str,
    requested_profile: Optional[str],
) -> Optional[str]:
    explicit = resolve_task_profile(requested_profile)
    if explicit is not None:
        return explicit
    if task_type != "recurring":
        return None

    instruction_lower = instruction.lower()
    haystack = f"{title} {instruction}".lower()
    if "topic:" in instruction_lower:
        return "topic_watcher"

    watch_markers = ("watch", "watcher", "monitor", "track", "follow")
    source_markers = ("news", "rss", "feed", "feeds", "headline", "headlines", "topic")
    if any(marker in haystack for marker in watch_markers) and any(marker in haystack for marker in source_markers):
        return "topic_watcher"
    return None


@dataclass(frozen=True)
class TaskUpdateResult:
    title_changed: bool
    instruction_changed: bool
    plan_inputs_changed: bool


async def create_task_record(
    db: AsyncSession,
    *,
    user_id: int,
    title: str,
    instruction: str,
    persona: Optional[str] = None,
    profile: Optional[str] = None,
    llm_model_override: Optional[str] = None,
    task_type: str = "one_shot",
    schedule: Optional[str] = None,
    deliver: bool = True,
    requires_approval: bool = True,
    active_hours_start: Optional[str] = None,
    active_hours_end: Optional[str] = None,
    active_hours_tz: Optional[str] = None,
    user_timezone: Optional[str] = None,
) -> Task:
    if task_type not in {"one_shot", "recurring"}:
        raise TaskValidationError("task_type must be one_shot or recurring.")
    resolved_persona = resolve_task_persona(
        title=title,
        instruction=instruction,
        requested_persona=persona,
    )
    inferred = infer_configured_executor(
        title=title,
        instruction=instruction,
        task_type=task_type,
        requested_profile=profile,
    )
    task = Task(
        user_id=user_id,
        title=title,
        instruction=instruction,
        persona=resolved_persona,
        profile=inferred.profile if inferred.executor_config else infer_task_profile(
            title=title,
            instruction=instruction,
            task_type=task_type,
            requested_profile=profile,
        ),
        executor_config=inferred.executor_config,
        llm_model_override=(str(llm_model_override).strip() or None) if llm_model_override is not None else None,
        task_type=task_type,
        status="pending",
        schedule=schedule,
        deliver=deliver,
        requires_approval=requires_approval,
        active_hours_start=active_hours_start,
        active_hours_end=active_hours_end,
        active_hours_tz=active_hours_tz,
        next_run_at=compute_next_run_at(
            schedule,
            task_timezone=active_hours_tz,
            user_timezone=user_timezone,
        ),
    )
    db.add(task)
    await db.flush()
    return task


async def update_task_record(
    db: AsyncSession,
    task: Task,
    *,
    title=UNSET,
    instruction=UNSET,
    persona=UNSET,
    profile=UNSET,
    llm_model_override=UNSET,
    schedule=UNSET,
    deliver=UNSET,
    requires_approval=UNSET,
    active_hours_start=UNSET,
    active_hours_end=UNSET,
    active_hours_tz=UNSET,
    user_timezone: Optional[str] = None,
) -> TaskUpdateResult:
    del db  # reserved for future validation that may require queries

    title_changed = False
    instruction_changed = False
    plan_inputs_changed = False

    if title is not UNSET:
        task.title = str(title)
        title_changed = True
        plan_inputs_changed = True
    if instruction is not UNSET:
        task.instruction = str(instruction)
        instruction_changed = True
        plan_inputs_changed = True
    if persona is not UNSET:
        task.persona = resolve_task_persona(
            title=task.title,
            instruction=task.instruction,
            requested_persona=persona,
        )
        plan_inputs_changed = True
    if profile is not UNSET:
        inferred = infer_configured_executor(
            title=task.title,
            instruction=task.instruction,
            task_type=task.task_type,
            requested_profile=profile,
        )
        task.profile = inferred.profile if inferred.executor_config else infer_task_profile(
            title=task.title,
            instruction=task.instruction,
            task_type=task.task_type,
            requested_profile=profile,
        )
        task.executor_config = inferred.executor_config
        plan_inputs_changed = True
    elif title_changed or instruction_changed:
        inferred = infer_configured_executor(
            title=task.title,
            instruction=task.instruction,
            task_type=task.task_type,
            requested_profile=None,
        )
        if inferred.executor_config:
            task.profile = inferred.profile
            task.executor_config = inferred.executor_config
            plan_inputs_changed = True
        elif task.executor_config:
            task.executor_config = {}
            task.profile = infer_task_profile(
                title=task.title,
                instruction=task.instruction,
                task_type=task.task_type,
                requested_profile=task.profile,
            )
            plan_inputs_changed = True
    if llm_model_override is not UNSET:
        task.llm_model_override = (str(llm_model_override).strip() or None) if llm_model_override is not None else None
        plan_inputs_changed = True
    if deliver is not UNSET:
        task.deliver = bool(deliver)
    if requires_approval is not UNSET:
        task.requires_approval = bool(requires_approval)
    if active_hours_start is not UNSET:
        task.active_hours_start = active_hours_start
    if active_hours_end is not UNSET:
        task.active_hours_end = active_hours_end
    if active_hours_tz is not UNSET:
        task.active_hours_tz = active_hours_tz
    if schedule is not UNSET:
        task.schedule = schedule
        plan_inputs_changed = True
    if schedule is not UNSET or active_hours_tz is not UNSET:
        task.next_run_at = compute_next_run_at(
            task.schedule,
            task_timezone=task.active_hours_tz,
            user_timezone=user_timezone,
        )

    if persona is UNSET and (title_changed or instruction_changed) and not task.persona:
        inferred, _, _ = infer_persona_for_task(task.title, task.instruction)
        task.persona = inferred
        plan_inputs_changed = True

    return TaskUpdateResult(
        title_changed=title_changed,
        instruction_changed=instruction_changed,
        plan_inputs_changed=plan_inputs_changed,
    )
