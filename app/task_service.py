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
from app.task_recipes import build_task_recipe_metadata, normalize_task_recipe
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
    recipe_family: Optional[str] = None,
    recipe_params: Optional[dict] = None,
) -> Task:
    if task_type not in {"one_shot", "recurring"}:
        raise TaskValidationError("task_type must be one_shot or recurring.")
    normalized_recipe = normalize_task_recipe(
        title=title,
        instruction=instruction,
        task_type=task_type,
        requested_profile=profile,
        recipe_family=recipe_family,
        recipe_params=recipe_params,
    )
    normalized_title = normalized_recipe.title if normalized_recipe is not None else title
    normalized_instruction = normalized_recipe.instruction if normalized_recipe is not None else instruction
    normalized_task_type = normalized_recipe.task_type if normalized_recipe is not None else task_type
    normalized_profile = normalized_recipe.profile if normalized_recipe is not None and normalized_recipe.profile else profile
    resolved_persona = resolve_task_persona(
        title=normalized_title,
        instruction=normalized_instruction,
        requested_persona=persona,
    )
    inferred = infer_configured_executor(
        title=normalized_title,
        instruction=normalized_instruction,
        task_type=normalized_task_type,
        requested_profile=normalized_profile,
    )
    resolved_profile = inferred.profile if inferred.executor_config else infer_task_profile(
        title=normalized_title,
        instruction=normalized_instruction,
        task_type=normalized_task_type,
        requested_profile=normalized_profile,
    )
    task = Task(
        user_id=user_id,
        title=normalized_title,
        instruction=normalized_instruction,
        persona=resolved_persona,
        profile=resolved_profile,
        executor_config=inferred.executor_config,
        task_recipe=build_task_recipe_metadata(
            normalized_recipe,
            selected_profile=resolved_profile,
            executor_config=inferred.executor_config,
        ),
        llm_model_override=(str(llm_model_override).strip() or None) if llm_model_override is not None else None,
        task_type=normalized_task_type,
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
    recipe_family=UNSET,
    recipe_params=UNSET,
) -> TaskUpdateResult:
    del db  # reserved for future validation that may require queries

    title_changed = False
    instruction_changed = False
    plan_inputs_changed = False
    recipe_inputs_changed = False

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
        plan_inputs_changed = True
    if recipe_family is not UNSET or recipe_params is not UNSET:
        recipe_inputs_changed = True
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

    if title_changed or instruction_changed or profile is not UNSET or recipe_inputs_changed:
        existing_recipe = task.task_recipe if isinstance(task.task_recipe, dict) else {}
        preferred_family = str(existing_recipe.get("family") or "").strip().lower() or None
        requested_recipe_family = recipe_family if recipe_family is not UNSET else preferred_family
        requested_recipe_params = recipe_params if recipe_params is not UNSET else existing_recipe.get("params")
        requested_profile_value = task.profile if profile is UNSET else profile
        normalized_recipe = normalize_task_recipe(
            title=task.title,
            instruction=task.instruction,
            task_type=task.task_type,
            requested_profile=requested_profile_value,
            recipe_family=requested_recipe_family,
            recipe_params=requested_recipe_params if isinstance(requested_recipe_params, dict) else None,
            preferred_family=preferred_family,
        )
        if normalized_recipe is not None:
            if not title_changed:
                task.title = normalized_recipe.title
            task.instruction = normalized_recipe.instruction
            requested_profile_value = normalized_recipe.profile if normalized_recipe.profile else requested_profile_value
        inferred = infer_configured_executor(
            title=task.title,
            instruction=task.instruction,
            task_type=task.task_type,
            requested_profile=requested_profile_value,
        )
        task.profile = inferred.profile if inferred.executor_config else infer_task_profile(
            title=task.title,
            instruction=task.instruction,
            task_type=task.task_type,
            requested_profile=requested_profile_value,
        )
        task.executor_config = inferred.executor_config
        task.task_recipe = build_task_recipe_metadata(
            normalized_recipe,
            selected_profile=task.profile,
            executor_config=inferred.executor_config,
        )

    return TaskUpdateResult(
        title_changed=title_changed,
        instruction_changed=instruction_changed,
        plan_inputs_changed=plan_inputs_changed,
    )
