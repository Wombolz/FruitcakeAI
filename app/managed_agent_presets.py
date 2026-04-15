from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.definition_loader import get_agent_preset
from app.db.models import ManagedAgentPreset, Task, TaskStep, User
from app.task_service import compute_next_run_at, create_task_record, update_task_record
from app.time_utils import resolve_effective_timezone


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentInstanceSeedSpec:
    preset_id: str
    display_name: str
    title: str
    instruction: str
    schedule: str
    active_hours_start: str | None = None
    active_hours_end: str | None = None
    context_paths: tuple[str, ...] = ()
    params: dict[str, Any] | None = None
    auto_maintain_task: bool = True
    requires_approval: bool = False
    deliver: bool = False


_AGENT_INSTANCE_SEEDS: tuple[AgentInstanceSeedSpec, ...] = (
    AgentInstanceSeedSpec(
        preset_id="document_sync_manager",
        display_name="Main Library Sync",
        title="Main Library Sync",
        instruction=(
            "Review linked source freshness and document sync state. Identify stale, failed, or blocked sources, "
            "trigger or queue rescan work when appropriate, and finish with a compact freshness summary."
        ),
        schedule="every:1d",
        active_hours_start="01:00",
        active_hours_end="05:00",
        params={
            "included_scopes": ["linked_sources"],
            "stale_threshold_hours": 24,
            "auto_rescan_linked_sources": True,
            "summary_verbosity": "compact",
        },
    ),
    AgentInstanceSeedSpec(
        preset_id="repo_map_manager",
        display_name="Primary Repo Map",
        title="Primary Repo Map",
        instruction=(
            "Refresh a current repo map and workspace orientation artifact for the configured roots. "
            "Summarize notable structure changes and any blocker that prevented refresh."
        ),
        schedule="every:1d",
        active_hours_start="02:00",
        active_hours_end="06:00",
        params={
            "output_path": "reports/repo_map.md",
            "included_roots": [],
            "ignored_paths": [".venv", "__pycache__"],
            "refresh_after_sync_only": False,
        },
    ),
    AgentInstanceSeedSpec(
        preset_id="recent_run_analyzer",
        display_name="Run Health Check",
        title="Run Health Check",
        instruction=(
            "Inspect recent non-self task and agent runs. Summarize failures, cancellations, missing artifacts, or suspicious states, "
            "and keep direct observations clearly separated from inference."
        ),
        schedule="every:6h",
        params={
            "lookback_hours": 24,
            "max_runs": 8,
            "problematic_only": True,
            "emit_all_clear": True,
        },
    ),
)


def list_agent_instance_seed_specs() -> list[AgentInstanceSeedSpec]:
    return list(_AGENT_INSTANCE_SEEDS)


def get_agent_instance_seed_for_display_name(display_name: str) -> AgentInstanceSeedSpec | None:
    key = str(display_name or "").strip().lower()
    for spec in _AGENT_INSTANCE_SEEDS:
        if spec.display_name.lower() == key:
            return spec
    return None


async def list_agent_instances(db: AsyncSession, *, user_id: int) -> list[ManagedAgentPreset]:
    rows = await db.execute(
        select(ManagedAgentPreset)
        .where(ManagedAgentPreset.user_id == user_id)
        .order_by(ManagedAgentPreset.display_name.asc(), ManagedAgentPreset.id.asc())
    )
    return list(rows.scalars().all())


async def get_agent_instance(db: AsyncSession, *, user_id: int, instance_id: int) -> ManagedAgentPreset | None:
    rows = await db.execute(
        select(ManagedAgentPreset).where(
            ManagedAgentPreset.user_id == user_id,
            ManagedAgentPreset.id == instance_id,
        )
    )
    return rows.scalar_one_or_none()


async def ensure_seed_agent_instances(
    db: AsyncSession,
    *,
    user: User,
) -> list[ManagedAgentPreset]:
    existing = await list_agent_instances(db, user_id=int(user.id))
    by_name = {row.display_name: row for row in existing}
    legacy_by_preset = {
        row.preset_id: row
        for row in existing
        if row.preset_id and row.display_name in {None, "", row.preset_id}
    }
    ensured: list[ManagedAgentPreset] = []
    user_timezone = resolve_effective_timezone(None, user.active_hours_tz)
    for spec in list_agent_instance_seed_specs():
        row = by_name.get(spec.display_name)
        if row is None:
            row = legacy_by_preset.get(spec.preset_id)
            if row is not None:
                row.display_name = spec.display_name
                if not row.schedule:
                    row.schedule = spec.schedule
                if not row.active_hours_start:
                    row.active_hours_start = spec.active_hours_start
                if not row.active_hours_end:
                    row.active_hours_end = spec.active_hours_end
                if not row.active_hours_tz:
                    row.active_hours_tz = user_timezone
                if not row.context_paths:
                    row.context_paths = list(spec.context_paths)
                if not isinstance(row.params, dict) or not row.params:
                    row.params = dict(spec.params or {})
        else:
            await _cleanup_legacy_duplicates(
                db,
                user_id=int(user.id),
                preset_id=spec.preset_id,
                keep_instance_id=int(row.id),
            )
        if row is None:
            row = ManagedAgentPreset(
                user_id=int(user.id),
                preset_id=spec.preset_id,
                display_name=spec.display_name,
                enabled=True,
                auto_maintain_task=spec.auto_maintain_task,
                schedule=spec.schedule,
                active_hours_start=spec.active_hours_start,
                active_hours_end=spec.active_hours_end,
                active_hours_tz=user_timezone,
            )
            row.context_paths = list(spec.context_paths)
            row.params = dict(spec.params or {})
            db.add(row)
            await db.flush()
        try:
            await reconcile_agent_instance(db, agent_instance=row, user=user)
        except Exception:
            log.exception(
                "Failed to reconcile seeded agent instance preset_id=%s display_name=%s user_id=%s",
                spec.preset_id,
                spec.display_name,
                user.id,
            )
            continue
        ensured.append(row)
    return ensured


async def _cleanup_legacy_duplicates(
    db: AsyncSession,
    *,
    user_id: int,
    preset_id: str,
    keep_instance_id: int,
) -> None:
    rows = await db.execute(
        select(ManagedAgentPreset).where(
            ManagedAgentPreset.user_id == user_id,
            ManagedAgentPreset.preset_id == preset_id,
        )
    )
    duplicates = [
        row
        for row in rows.scalars().all()
        if int(row.id) != keep_instance_id and row.display_name in {None, "", preset_id}
    ]
    for duplicate in duplicates:
        linked_task_id = getattr(duplicate, "linked_task_id", None)
        if linked_task_id is not None:
            await db.execute(delete(Task).where(Task.id == int(linked_task_id)))
        await db.delete(duplicate)


async def create_agent_instance(
    db: AsyncSession,
    *,
    user: User,
    preset_id: str,
    display_name: str,
    schedule: str | None = None,
    active_hours_start: str | None = None,
    active_hours_end: str | None = None,
    active_hours_tz: str | None = None,
    llm_model_override: str | None = None,
    context_paths: list[str] | None = None,
    params: dict[str, Any] | None = None,
    enabled: bool = True,
    auto_maintain_task: bool = True,
) -> ManagedAgentPreset:
    preset = get_agent_preset(preset_id)
    if preset is None:
        raise ValueError(f"Unknown preset '{preset_id}'")
    clean_display_name = str(display_name or "").strip()
    if not clean_display_name:
        raise ValueError("display_name is required")
    existing = await db.execute(
        select(ManagedAgentPreset).where(
            ManagedAgentPreset.user_id == int(user.id),
            ManagedAgentPreset.display_name == clean_display_name,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ValueError(f"An agent instance named '{clean_display_name}' already exists")

    row = ManagedAgentPreset(
        user_id=int(user.id),
        preset_id=preset_id,
        display_name=clean_display_name,
        enabled=bool(enabled),
        auto_maintain_task=bool(auto_maintain_task),
        schedule=(str(schedule).strip() if schedule is not None else None) or _default_schedule_for_preset(preset_id),
        active_hours_start=(str(active_hours_start).strip() if active_hours_start is not None else None) or _default_active_start_for_preset(preset_id),
        active_hours_end=(str(active_hours_end).strip() if active_hours_end is not None else None) or _default_active_end_for_preset(preset_id),
        active_hours_tz=(str(active_hours_tz).strip() if active_hours_tz is not None else None) or resolve_effective_timezone(None, user.active_hours_tz),
        llm_model_override=(str(llm_model_override).strip() or None) if llm_model_override is not None else None,
    )
    row.context_paths = [str(item).strip() for item in (context_paths or []) if str(item).strip()]
    row.params = dict(params or _default_params_for_preset(preset_id))
    db.add(row)
    await db.flush()
    await reconcile_agent_instance(db, agent_instance=row, user=user)
    return row


async def update_agent_instance(
    db: AsyncSession,
    *,
    agent_instance: ManagedAgentPreset,
    user: User,
    display_name: Optional[str] = None,
    enabled: Optional[bool] = None,
    auto_maintain_task: Optional[bool] = None,
    schedule: Optional[str] = None,
    active_hours_start: Optional[str] = None,
    active_hours_end: Optional[str] = None,
    active_hours_tz: Optional[str] = None,
    llm_model_override: Optional[str] = None,
    context_paths: Optional[list[str]] = None,
    params: Optional[dict[str, Any]] = None,
) -> ManagedAgentPreset:
    if display_name is not None:
        clean_name = str(display_name).strip()
        if not clean_name:
            raise ValueError("display_name is required")
        rows = await db.execute(
            select(ManagedAgentPreset).where(
                ManagedAgentPreset.user_id == int(user.id),
                ManagedAgentPreset.display_name == clean_name,
                ManagedAgentPreset.id != int(agent_instance.id),
            )
        )
        if rows.scalar_one_or_none() is not None:
            raise ValueError(f"An agent instance named '{clean_name}' already exists")
        agent_instance.display_name = clean_name
    if enabled is not None:
        agent_instance.enabled = bool(enabled)
    if auto_maintain_task is not None:
        agent_instance.auto_maintain_task = bool(auto_maintain_task)
    if schedule is not None:
        agent_instance.schedule = str(schedule).strip() or None
    if active_hours_start is not None:
        agent_instance.active_hours_start = str(active_hours_start).strip() or None
    if active_hours_end is not None:
        agent_instance.active_hours_end = str(active_hours_end).strip() or None
    if active_hours_tz is not None:
        agent_instance.active_hours_tz = str(active_hours_tz).strip() or None
    if llm_model_override is not None:
        agent_instance.llm_model_override = str(llm_model_override).strip() or None
    if context_paths is not None:
        agent_instance.context_paths = [str(item).strip() for item in context_paths if str(item).strip()]
    if params is not None:
        agent_instance.params = params
    await reconcile_agent_instance(db, agent_instance=agent_instance, user=user)
    return agent_instance


async def reconcile_agent_instance(
    db: AsyncSession,
    *,
    agent_instance: ManagedAgentPreset,
    user: User,
    recreate_missing: bool = True,
) -> ManagedAgentPreset:
    preset = get_agent_preset(agent_instance.preset_id)
    if preset is None:
        raise ValueError(f"Preset '{agent_instance.preset_id}' is not registered")

    linked_task = await _load_linked_task(db, agent_instance)
    if linked_task is None and agent_instance.linked_task_id is not None:
        agent_instance.linked_task_id = None

    if linked_task is None and agent_instance.enabled and agent_instance.auto_maintain_task and recreate_missing:
        linked_task = await create_task_record(
            db,
            user_id=int(user.id),
            title=agent_instance.display_name,
            instruction=_instruction_for_instance(agent_instance),
            recipe_family="agent",
            recipe_params=_build_recipe_params(agent_instance),
            llm_model_override=agent_instance.llm_model_override,
            task_type="recurring",
            schedule=agent_instance.schedule,
            deliver=False,
            requires_approval=False,
            active_hours_start=agent_instance.active_hours_start,
            active_hours_end=agent_instance.active_hours_end,
            active_hours_tz=agent_instance.active_hours_tz or resolve_effective_timezone(None, user.active_hours_tz),
            user_timezone=user.active_hours_tz,
        )
        agent_instance.linked_task_id = int(linked_task.id)

    if linked_task is not None:
        await _sync_linked_task(
            db,
            linked_task=linked_task,
            agent_instance=agent_instance,
            user=user,
        )
    return agent_instance


async def _sync_linked_task(
    db: AsyncSession,
    *,
    linked_task: Task,
    agent_instance: ManagedAgentPreset,
    user: User,
) -> None:
    await update_task_record(
        db,
        linked_task,
        title=agent_instance.display_name,
        instruction=_instruction_for_instance(agent_instance),
        task_type="recurring",
        llm_model_override=agent_instance.llm_model_override,
        schedule=agent_instance.schedule,
        deliver=False,
        requires_approval=False,
        active_hours_start=agent_instance.active_hours_start,
        active_hours_end=agent_instance.active_hours_end,
        active_hours_tz=agent_instance.active_hours_tz or resolve_effective_timezone(None, user.active_hours_tz),
        recipe_family="agent",
        recipe_params=_build_recipe_params(agent_instance),
        user_timezone=user.active_hours_tz,
    )
    await _clear_idle_agent_plan_state(db, linked_task=linked_task)

    is_active_run = str(linked_task.status or "").strip().lower() in {"running", "waiting_approval"}
    if agent_instance.enabled and agent_instance.auto_maintain_task:
        if is_active_run:
            linked_task.error = None
            return
        linked_task.status = "pending"
        linked_task.error = None
        linked_task.next_run_at = compute_next_run_at(
            linked_task.schedule,
            task_timezone=linked_task.active_hours_tz,
            user_timezone=user.active_hours_tz,
        )
    else:
        linked_task.status = "cancelled"
        linked_task.next_run_at = None
        linked_task.next_retry_at = None
        linked_task.error = "Agent instance disabled" if not agent_instance.enabled else "Agent instance auto-maintain disabled"


async def _clear_idle_agent_plan_state(db: AsyncSession, *, linked_task: Task) -> None:
    recipe = linked_task.task_recipe if isinstance(linked_task.task_recipe, dict) else {}
    if str(recipe.get("family") or "").strip().lower() != "agent":
        return
    if str(linked_task.status or "").strip().lower() in {"running", "waiting_approval"}:
        return
    rows = await db.execute(select(TaskStep.id).where(TaskStep.task_id == int(linked_task.id)).limit(1))
    has_steps = rows.scalar_one_or_none() is not None
    if not linked_task.has_plan and linked_task.current_step_index is None and not has_steps:
        return
    await db.execute(delete(TaskStep).where(TaskStep.task_id == int(linked_task.id)))
    linked_task.has_plan = False
    linked_task.current_step_index = None


async def _load_linked_task(db: AsyncSession, agent_instance: ManagedAgentPreset) -> Task | None:
    linked_task_id = getattr(agent_instance, "linked_task_id", None)
    if not linked_task_id:
        return None
    rows = await db.execute(select(Task).where(Task.id == int(linked_task_id)))
    return rows.scalar_one_or_none()


def _build_recipe_params(agent_instance: ManagedAgentPreset) -> dict[str, Any]:
    params: dict[str, Any] = {
        "agent_role": agent_instance.preset_id,
        "agent_instance_id": int(agent_instance.id),
        "display_name": agent_instance.display_name,
        "context_paths": agent_instance.context_paths,
    }
    stored_params = agent_instance.params if isinstance(agent_instance.params, dict) else {}
    params.update(stored_params)
    return params


def _instruction_for_instance(agent_instance: ManagedAgentPreset) -> str:
    params = agent_instance.params if isinstance(agent_instance.params, dict) else {}
    preset_id = agent_instance.preset_id
    if preset_id == "document_sync_manager":
        return (
            "Review linked source freshness and document sync state for the configured scopes and sources. "
            "Identify stale, failed, or blocked sources, trigger or queue rescan work when appropriate, and finish with a compact freshness summary."
        )
    if preset_id == "repo_map_manager":
        return (
            "Refresh a current repo map and workspace orientation artifact for the configured roots and ignored paths. "
            "Summarize notable structure changes and any blocker that prevented refresh."
        )
    if preset_id == "recent_run_analyzer":
        return (
            "Inspect recent non-self task and agent runs using the configured lookback window and run count. "
            "Summarize failures, cancellations, missing artifacts, or suspicious states, and clearly separate direct observation from inference."
        )
    return f"Run agent preset '{preset_id}' for the configured instance settings."


def _default_schedule_for_preset(preset_id: str) -> str:
    for spec in _AGENT_INSTANCE_SEEDS:
        if spec.preset_id == preset_id:
            return spec.schedule
    return "every:1d"


def _default_active_start_for_preset(preset_id: str) -> str | None:
    for spec in _AGENT_INSTANCE_SEEDS:
        if spec.preset_id == preset_id:
            return spec.active_hours_start
    return None


def _default_active_end_for_preset(preset_id: str) -> str | None:
    for spec in _AGENT_INSTANCE_SEEDS:
        if spec.preset_id == preset_id:
            return spec.active_hours_end
    return None


def _default_params_for_preset(preset_id: str) -> dict[str, Any]:
    for spec in _AGENT_INSTANCE_SEEDS:
        if spec.preset_id == preset_id:
            return dict(spec.params or {})
    return {}
