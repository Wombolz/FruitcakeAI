from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.definition_loader import get_agent_definition
from app.agent.persona_loader import list_personas, persona_exists
from app.agent.persona_router import infer_persona_for_task
from app.autonomy.configured_executor import infer_configured_executor
from app.autonomy.profiles import normalize_task_profile
from app.db.models import RSSSource, Task
from app.task_recipes import build_task_recipe_metadata, normalize_task_recipe
from app.time_utils import resolve_effective_timezone


class TaskValidationError(ValueError):
    pass


UNSET = object()
_TASK_TITLE_MAX_LENGTH = 255


def _normalize_task_title(value: str) -> str:
    title = str(value or "").strip()
    if not title:
        raise TaskValidationError("title is required.")
    if len(title) > _TASK_TITLE_MAX_LENGTH:
        raise TaskValidationError(f"title must be {_TASK_TITLE_MAX_LENGTH} characters or fewer.")
    return title


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


def resolve_agent_behavior_persona(
    *,
    requested_persona: Optional[str],
    recipe_family: Optional[str],
    recipe_params: Optional[dict[str, Any]],
) -> Optional[str]:
    explicit = (requested_persona or "").strip()
    if explicit:
        return None
    if _explicit_recipe_family_value(recipe_family) != "agent":
        return None
    params = recipe_params if isinstance(recipe_params, dict) else {}
    agent_role = str(params.get("agent_role") or "").strip()
    if not agent_role:
        return None
    definition = get_agent_definition(agent_role)
    if definition and definition.persona_compatibility and persona_exists(definition.persona_compatibility):
        return definition.persona_compatibility
    return agent_role if persona_exists(agent_role) else None


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


@dataclass(frozen=True)
class TaskDraftPayload:
    title: str
    instruction: str
    persona: Optional[str]
    profile: Optional[str]
    executor_config: dict[str, Any]
    task_recipe: dict[str, Any]
    llm_model_override: Optional[str]
    task_type: str
    schedule: Optional[str]
    deliver: bool
    requires_approval: bool
    active_hours_start: Optional[str]
    active_hours_end: Optional[str]
    active_hours_tz: Optional[str]
    next_run_at: datetime | None
    effective_timezone: str


def _is_explicit_generic_recipe(recipe_family: Optional[str]) -> bool:
    return recipe_family is not None and not str(recipe_family).strip()


def _explicit_recipe_family_value(recipe_family: Optional[str]) -> str | None:
    value = str(recipe_family or "").strip().lower()
    return value or None


async def build_task_draft_payload(
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
) -> TaskDraftPayload:
    title = _normalize_task_title(title)
    if task_type not in {"one_shot", "recurring"}:
        raise TaskValidationError("task_type must be one_shot or recurring.")

    explicit_generic = _is_explicit_generic_recipe(recipe_family)
    normalized_recipe = None
    normalized_title = title
    normalized_instruction = instruction
    normalized_task_type = task_type
    normalized_profile = None if explicit_generic else profile
    inferred_executor_config: dict[str, Any] | None = None
    resolved_profile: str | None = None

    explicit_recipe_family = _explicit_recipe_family_value(recipe_family)
    if not explicit_generic:
        normalized_recipe = normalize_task_recipe(
            title=title,
            instruction=instruction,
            task_type=task_type,
            requested_profile=profile,
            recipe_family=recipe_family,
            recipe_params=recipe_params,
        )
        normalized_recipe = await _align_topic_watcher_sources(
            db,
            user_id=user_id,
            recipe=normalized_recipe,
        )
        if explicit_recipe_family and normalized_recipe is None:
            raise TaskValidationError(
                f"Could not build the selected task family '{explicit_recipe_family}'. Add the required structured details or keep the task generic."
            )
        normalized_title = normalized_recipe.title if normalized_recipe is not None else title
        normalized_instruction = normalized_recipe.instruction if normalized_recipe is not None else instruction
        normalized_task_type = normalized_recipe.task_type if normalized_recipe is not None else task_type
        normalized_profile = normalized_recipe.profile if normalized_recipe is not None and normalized_recipe.profile else profile
    normalized_title = _normalize_task_title(normalized_title)
    resolved_persona = resolve_agent_behavior_persona(
        requested_persona=persona,
        recipe_family=normalized_recipe.family if normalized_recipe is not None else explicit_recipe_family,
        recipe_params=normalized_recipe.params if normalized_recipe is not None else (recipe_params if isinstance(recipe_params, dict) else None),
    ) or resolve_task_persona(
        title=normalized_title,
        instruction=normalized_instruction,
        requested_persona=persona,
    )
    if not explicit_generic:
        inferred = infer_configured_executor(
            title=normalized_title,
            instruction=normalized_instruction,
            task_type=normalized_task_type,
            requested_profile=normalized_profile,
        )
        inferred_executor_config = inferred.executor_config
        resolved_profile = inferred.profile if inferred.executor_config else infer_task_profile(
            title=normalized_title,
            instruction=normalized_instruction,
            task_type=normalized_task_type,
            requested_profile=normalized_profile,
        )

    return TaskDraftPayload(
        title=normalized_title,
        instruction=normalized_instruction,
        persona=resolved_persona,
        profile=resolved_profile,
        executor_config=inferred_executor_config or {},
        task_recipe=build_task_recipe_metadata(
            normalized_recipe,
            selected_profile=resolved_profile,
            executor_config=inferred_executor_config,
        ),
        llm_model_override=(str(llm_model_override).strip() or None) if llm_model_override is not None else None,
        task_type=normalized_task_type,
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
        effective_timezone=effective_task_timezone(
            task_timezone=active_hours_tz,
            user_timezone=user_timezone,
        ),
    )


async def _align_topic_watcher_sources(
    db: AsyncSession,
    *,
    user_id: int,
    recipe,
):
    if recipe is None or recipe.family != "topic_watcher":
        return recipe
    requested_sources = list(recipe.params.get("sources") or [])
    if not requested_sources:
        return recipe

    rows = (
        await db.execute(
            select(RSSSource)
            .where(
                RSSSource.active == True,
                or_(RSSSource.user_id == user_id, RSSSource.user_id.is_(None)),
            )
            .order_by(RSSSource.user_id.is_(None), RSSSource.name.asc())
        )
    ).scalars().all()
    if not rows:
        return recipe

    active_names = [str(row.name or "").strip() for row in rows if str(row.name or "").strip()]
    matched_names: list[str] = []
    for source in requested_sources:
        match = _match_active_source_name(str(source), active_names)
        if match and match not in matched_names:
            matched_names.append(match)

    assumptions = list(recipe.assumptions)
    if requested_sources and not matched_names:
        assumptions.append("dropped watcher source filters because they did not match active RSS sources")
    elif len(matched_names) < len(requested_sources):
        assumptions.append("dropped watcher source filters because only some suggested sources matched active RSS sources")
        matched_names = []

    lines = [f"topic: {recipe.params.get('topic')}", f"threshold: {recipe.params.get('threshold') or 'medium'}"]
    if matched_names:
        lines.append(f"sources: {', '.join(matched_names)}")
    lines.extend(
        [
            "",
            f"Watch my curated RSS feeds for significant updates about {recipe.params.get('topic')}.",
            "Return NOTHING_NEW when there is no meaningful change.",
        ]
    )
    return replace(
        recipe,
        instruction="\n".join(lines).strip(),
        params={**recipe.params, "sources": matched_names},
        assumptions=assumptions,
    )


def _match_active_source_name(requested: str, active_names: list[str]) -> str | None:
    normalized_requested = _normalize_source_name(requested)
    if not normalized_requested:
        return None
    for candidate in active_names:
        normalized_candidate = _normalize_source_name(candidate)
        if normalized_candidate == normalized_requested:
            return candidate
    requested_tokens = set(normalized_requested.split())
    for candidate in active_names:
        normalized_candidate = _normalize_source_name(candidate)
        candidate_tokens = set(normalized_candidate.split())
        if requested_tokens and requested_tokens.issubset(candidate_tokens):
            return candidate
        if candidate_tokens and candidate_tokens.issubset(requested_tokens):
            return candidate
    return None


def _normalize_source_name(value: str) -> str:
    candidate = str(value or "").lower().strip()
    candidate = re.sub(r"[–—:/()]+", " ", candidate)
    candidate = re.sub(r"\brss\b", " ", candidate)
    candidate = re.sub(r"\bofficial\b", " ", candidate)
    candidate = re.sub(r"\bfeeds?\b", " ", candidate)
    candidate = re.sub(r"\bblogs?\b", "blog", candidate)
    candidate = re.sub(r"\bscience\b", " ", candidate)
    candidate = re.sub(r"\bworld news\b", "world", candidate)
    candidate = re.sub(r"[^a-z0-9+& ]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    return candidate


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
    draft = await build_task_draft_payload(
        db,
        user_id=user_id,
        title=title,
        instruction=instruction,
        persona=persona,
        profile=profile,
        recipe_family=recipe_family,
        recipe_params=recipe_params,
        llm_model_override=llm_model_override,
        task_type=task_type,
        schedule=schedule,
        deliver=deliver,
        requires_approval=requires_approval,
        active_hours_start=active_hours_start,
        active_hours_end=active_hours_end,
        active_hours_tz=active_hours_tz,
        user_timezone=user_timezone,
    )
    task = Task(
        user_id=user_id,
        title=draft.title,
        instruction=draft.instruction,
        persona=draft.persona,
        profile=draft.profile,
        executor_config=draft.executor_config,
        task_recipe=draft.task_recipe,
        llm_model_override=draft.llm_model_override,
        task_type=draft.task_type,
        status="pending",
        schedule=draft.schedule,
        deliver=draft.deliver,
        requires_approval=draft.requires_approval,
        active_hours_start=draft.active_hours_start,
        active_hours_end=draft.active_hours_end,
        active_hours_tz=draft.active_hours_tz,
        next_run_at=draft.next_run_at,
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
    task_type=UNSET,
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
    title_changed = False
    instruction_changed = False
    plan_inputs_changed = False
    recipe_inputs_changed = False

    if title is not UNSET:
        task.title = _normalize_task_title(str(title))
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
    if task_type is not UNSET:
        normalized_task_type = str(task_type).strip().lower() if task_type is not None else ""
        if normalized_task_type not in {"one_shot", "recurring"}:
            raise TaskValidationError("task_type must be one_shot or recurring.")
        task.task_type = normalized_task_type
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
    if task.task_type == "one_shot":
        task.schedule = None
    if schedule is not UNSET or active_hours_tz is not UNSET or task_type is not UNSET:
        task.next_run_at = compute_next_run_at(
            task.schedule if task.task_type == "recurring" else None,
            task_timezone=task.active_hours_tz,
            user_timezone=user_timezone,
        )

    if persona is UNSET and (title_changed or instruction_changed) and not task.persona:
        task.persona = resolve_agent_behavior_persona(
            requested_persona=None,
            recipe_family=(task.task_recipe or {}).get("family") if isinstance(task.task_recipe, dict) else None,
            recipe_params=(task.task_recipe or {}).get("params") if isinstance(task.task_recipe, dict) else None,
        )
        if not task.persona:
            inferred, _, _ = infer_persona_for_task(task.title, task.instruction)
            task.persona = inferred
        plan_inputs_changed = True

    if title_changed or instruction_changed or profile is not UNSET or recipe_inputs_changed:
        existing_recipe = task.task_recipe if isinstance(task.task_recipe, dict) else {}
        preferred_family = str(existing_recipe.get("family") or "").strip().lower() or None
        requested_recipe_family = recipe_family if recipe_family is not UNSET else preferred_family
        requested_recipe_params = recipe_params if recipe_params is not UNSET else existing_recipe.get("params")
        requested_profile_value = task.profile if profile is UNSET else profile
        explicit_recipe_family = (
            str(recipe_family).strip().lower()
            if recipe_family is not UNSET and recipe_family is not None
            else ""
        )
        explicit_generic_recipe = _is_explicit_generic_recipe(recipe_family if recipe_family is not UNSET else None)
        if (
            profile is UNSET
            and recipe_family is not UNSET
            and (explicit_generic_recipe or explicit_recipe_family != preferred_family)
        ):
            requested_profile_value = None
        if explicit_generic_recipe:
            task.profile = None
            task.executor_config = {}
            task.task_recipe = {}
        else:
            explicit_requested_family = _explicit_recipe_family_value(recipe_family if recipe_family is not UNSET else None)
            normalized_recipe = normalize_task_recipe(
                title=task.title,
                instruction=task.instruction,
                task_type=task.task_type,
                requested_profile=requested_profile_value,
                recipe_family=requested_recipe_family,
                recipe_params=requested_recipe_params if isinstance(requested_recipe_params, dict) else None,
                preferred_family=preferred_family,
            )
            normalized_recipe = await _align_topic_watcher_sources(
                db,
                user_id=int(task.user_id),
                recipe=normalized_recipe,
            )
            if explicit_requested_family and normalized_recipe is None:
                raise TaskValidationError(
                    f"Could not build the selected task family '{explicit_requested_family}'. Add the required structured details or keep the task generic."
                )
            if normalized_recipe is not None:
                if not title_changed:
                    task.title = _normalize_task_title(normalized_recipe.title)
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
            if persona is UNSET and not task.persona:
                task.persona = resolve_agent_behavior_persona(
                    requested_persona=None,
                    recipe_family=(task.task_recipe or {}).get("family") if isinstance(task.task_recipe, dict) else None,
                    recipe_params=(task.task_recipe or {}).get("params") if isinstance(task.task_recipe, dict) else None,
                )
                if not task.persona:
                    inferred, _, _ = infer_persona_for_task(task.title, task.instruction)
                    task.persona = inferred

    return TaskUpdateResult(
        title_changed=title_changed,
        instruction_changed=instruction_changed,
        plan_inputs_changed=plan_inputs_changed,
    )
