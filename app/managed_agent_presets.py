from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.definition_loader import get_agent_preset
from app.db.models import ManagedAgentPreset, Task, User
from app.task_service import compute_next_run_at, create_task_record, update_task_record
from app.time_utils import resolve_effective_timezone


@dataclass(frozen=True)
class ManagedPresetSpec:
    preset_id: str
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


_MANAGED_PRESET_SPECS: dict[str, ManagedPresetSpec] = {
    "document_sync_manager": ManagedPresetSpec(
        preset_id="document_sync_manager",
        title="Document Sync Manager",
        instruction=(
            "Review internal document freshness and linked-source sync state. "
            "Identify stale, failed, or blocked sources, trigger or queue rescan work when appropriate, "
            "and finish with a compact freshness summary containing synced, stale, failed, and blocked counts."
        ),
        schedule="every:1d",
        active_hours_start="01:00",
        active_hours_end="05:00",
        context_paths=(
            "Docs/_internal/MASTER_ROADMAP.md",
            "Docs/_internal/PLANNING_CONTEXT.md",
        ),
        params={
            "included_scopes": ["internal_docs", "linked_sources"],
            "stale_threshold_hours": 24,
            "auto_rescan_linked_sources": True,
            "summary_verbosity": "compact",
        },
    ),
    "repo_map_manager": ManagedPresetSpec(
        preset_id="repo_map_manager",
        title="Repo Map Manager",
        instruction=(
            "Refresh the current repo map and workspace orientation artifact. Focus on key roots, important internal docs, "
            "and notable entrypoints that help other agents start grounded. Summarize what changed and any blocker that prevented refresh."
        ),
        schedule="every:1d",
        active_hours_start="02:00",
        active_hours_end="06:00",
        context_paths=(
            "Docs/_internal/agent_context_preload_and_repo_map_plan.md",
            "Docs/_internal/FruitcakeAi Roadmap.md",
        ),
        params={
            "output_path": "workspace/1/reports/repo_map.md",
            "included_roots": ["app", "config", "Docs/_internal", "tests"],
            "ignored_paths": [".venv", "__pycache__"],
            "refresh_after_sync_only": False,
        },
    ),
    "recent_run_analyzer": ManagedPresetSpec(
        preset_id="recent_run_analyzer",
        title="Recent Run Analyzer",
        instruction=(
            "Inspect the most recent non-self task and agent runs. Summarize failures, cancellations, missing artifacts, or suspicious states, "
            "and keep direct observations clearly separated from inference. Include a compact all-clear summary only when the recent runs are healthy."
        ),
        schedule="every:6h",
        context_paths=(
            "app/api/tasks.py",
            "app/api/admin.py",
        ),
        params={
            "lookback_hours": 24,
            "max_runs": 8,
            "problematic_only": True,
            "emit_all_clear": True,
        },
    ),
}


MANAGED_PRESET_IDS: tuple[str, ...] = tuple(_MANAGED_PRESET_SPECS.keys())


def list_managed_preset_specs() -> list[ManagedPresetSpec]:
    return list(_MANAGED_PRESET_SPECS.values())


def get_managed_preset_spec(preset_id: str) -> ManagedPresetSpec | None:
    return _MANAGED_PRESET_SPECS.get(str(preset_id or "").strip())


async def list_managed_preset_rows(db: AsyncSession, *, user_id: int) -> list[ManagedAgentPreset]:
    rows = await db.execute(
        select(ManagedAgentPreset)
        .where(ManagedAgentPreset.user_id == user_id)
        .order_by(ManagedAgentPreset.preset_id.asc())
    )
    return list(rows.scalars().all())


async def get_managed_preset_row(db: AsyncSession, *, user_id: int, preset_id: str) -> ManagedAgentPreset | None:
    rows = await db.execute(
        select(ManagedAgentPreset).where(
            ManagedAgentPreset.user_id == user_id,
            ManagedAgentPreset.preset_id == str(preset_id or "").strip(),
        )
    )
    return rows.scalar_one_or_none()


async def ensure_default_managed_presets(
    db: AsyncSession,
    *,
    user: User,
) -> list[ManagedAgentPreset]:
    existing = {row.preset_id: row for row in await list_managed_preset_rows(db, user_id=int(user.id))}
    created_or_existing: list[ManagedAgentPreset] = []
    user_timezone = resolve_effective_timezone(None, user.active_hours_tz)
    for spec in list_managed_preset_specs():
        row = existing.get(spec.preset_id)
        if row is None:
            row = ManagedAgentPreset(
                user_id=int(user.id),
                preset_id=spec.preset_id,
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
        await reconcile_managed_preset(db, managed_preset=row, user=user)
        created_or_existing.append(row)
    return created_or_existing


async def update_managed_preset_row(
    db: AsyncSession,
    *,
    managed_preset: ManagedAgentPreset,
    user: User,
    enabled: Optional[bool] = None,
    auto_maintain_task: Optional[bool] = None,
    schedule: Optional[str] = None,
    active_hours_start: Optional[str] = None,
    active_hours_end: Optional[str] = None,
    active_hours_tz: Optional[str] = None,
    context_paths: Optional[list[str]] = None,
    params: Optional[dict[str, Any]] = None,
) -> ManagedAgentPreset:
    if enabled is not None:
        managed_preset.enabled = bool(enabled)
    if auto_maintain_task is not None:
        managed_preset.auto_maintain_task = bool(auto_maintain_task)
    if schedule is not None:
        managed_preset.schedule = str(schedule).strip() or None
    if active_hours_start is not None:
        managed_preset.active_hours_start = str(active_hours_start).strip() or None
    if active_hours_end is not None:
        managed_preset.active_hours_end = str(active_hours_end).strip() or None
    if active_hours_tz is not None:
        managed_preset.active_hours_tz = str(active_hours_tz).strip() or None
    if context_paths is not None:
        managed_preset.context_paths = [str(item).strip() for item in context_paths if str(item).strip()]
    if params is not None:
        managed_preset.params = params
    await reconcile_managed_preset(db, managed_preset=managed_preset, user=user)
    return managed_preset


async def reconcile_managed_preset(
    db: AsyncSession,
    *,
    managed_preset: ManagedAgentPreset,
    user: User,
    recreate_missing: bool = True,
) -> ManagedAgentPreset:
    spec = get_managed_preset_spec(managed_preset.preset_id)
    if spec is None:
        raise ValueError(f"Unknown managed preset '{managed_preset.preset_id}'")
    preset = get_agent_preset(managed_preset.preset_id)
    if preset is None:
        raise ValueError(f"Preset '{managed_preset.preset_id}' is not registered")

    linked_task = await _load_linked_task(db, managed_preset)
    if linked_task is None and managed_preset.linked_task_id is not None:
        managed_preset.linked_task_id = None

    if linked_task is None and managed_preset.enabled and managed_preset.auto_maintain_task and recreate_missing:
        linked_task = await create_task_record(
            db,
            user_id=int(user.id),
            title=spec.title,
            instruction=spec.instruction,
            recipe_family="agent",
            recipe_params=_build_recipe_params(managed_preset, spec),
            task_type="recurring",
            schedule=managed_preset.schedule or spec.schedule,
            deliver=spec.deliver,
            requires_approval=spec.requires_approval,
            active_hours_start=managed_preset.active_hours_start,
            active_hours_end=managed_preset.active_hours_end,
            active_hours_tz=managed_preset.active_hours_tz or resolve_effective_timezone(None, user.active_hours_tz),
            user_timezone=user.active_hours_tz,
        )
        managed_preset.linked_task_id = int(linked_task.id)

    if linked_task is not None:
        await _sync_linked_task(
            db,
            linked_task=linked_task,
            managed_preset=managed_preset,
            spec=spec,
            user=user,
        )
    return managed_preset


async def _sync_linked_task(
    db: AsyncSession,
    *,
    linked_task: Task,
    managed_preset: ManagedAgentPreset,
    spec: ManagedPresetSpec,
    user: User,
) -> None:
    await update_task_record(
        db,
        linked_task,
        title=spec.title,
        instruction=spec.instruction,
        task_type="recurring",
        schedule=managed_preset.schedule or spec.schedule,
        deliver=spec.deliver,
        requires_approval=spec.requires_approval,
        active_hours_start=managed_preset.active_hours_start,
        active_hours_end=managed_preset.active_hours_end,
        active_hours_tz=managed_preset.active_hours_tz or resolve_effective_timezone(None, user.active_hours_tz),
        recipe_family="agent",
        recipe_params=_build_recipe_params(managed_preset, spec),
        user_timezone=user.active_hours_tz,
    )
    if managed_preset.enabled and managed_preset.auto_maintain_task:
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
        linked_task.error = "Managed preset disabled" if not managed_preset.enabled else "Managed preset auto-maintain disabled"


async def _load_linked_task(db: AsyncSession, managed_preset: ManagedAgentPreset) -> Task | None:
    linked_task_id = getattr(managed_preset, "linked_task_id", None)
    if not linked_task_id:
        return None
    rows = await db.execute(select(Task).where(Task.id == int(linked_task_id)))
    return rows.scalar_one_or_none()


def _build_recipe_params(managed_preset: ManagedAgentPreset, spec: ManagedPresetSpec) -> dict[str, Any]:
    params: dict[str, Any] = {
        "agent_role": managed_preset.preset_id,
        "managed_preset_id": managed_preset.preset_id,
        "context_paths": managed_preset.context_paths or list(spec.context_paths),
    }
    stored_params = managed_preset.params if isinstance(managed_preset.params, dict) else {}
    params.update(stored_params)
    return params
