"""
FruitcakeAI v5 — Tasks API (Phase 4)

POST   /tasks              Create task, compute next_run_at
GET    /tasks              List user's tasks (most recent first)
GET    /tasks/{id}         Task detail + last result
PATCH  /tasks/{id}         Update or approve/reject
DELETE /tasks/{id}         Cancel task (status=cancelled)
POST   /tasks/{id}/run     Manual trigger (dev/testing)
POST   /tasks/{id}/reset   Recover a task stuck in 'running' after a restart
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

import json
import re

from app.agent.definition_loader import FruitcakeAgentPreset, get_agent_preset
from app.autonomy.planner import create_task_plan_for_user
from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import AuditLog, ManagedAgentPreset, Task, TaskRun, TaskRunArtifact, TaskStep, User
from app.db.session import get_db
from app.managed_agent_presets import (
    create_agent_instance,
    ensure_seed_agent_instances,
    get_agent_instance,
    list_agent_instances,
    reconcile_agent_instance,
    update_agent_instance,
)
from app.memory.service import get_memory_service
from app.mcp.servers.filesystem import resolve_workspace_path_for_user, write_workspace_text
from app.task_service import TaskValidationError, UNSET, create_task_record, update_task_record
from app.time_utils import format_localized_datetime, resolve_effective_timezone

router = APIRouter()
log = logging.getLogger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    title: str
    instruction: str
    persona: Optional[str] = None
    profile: Optional[str] = None
    llm_model_override: Optional[str] = None
    task_type: str = "one_shot"          # "one_shot" | "recurring"
    schedule: Optional[str] = None       # "every:30m" | cron | ISO timestamp
    deliver: bool = True
    requires_approval: bool = True
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    active_hours_tz: Optional[str] = None
    recipe_family: Optional[str] = None
    recipe_params: Optional[Dict[str, Any]] = None


class TaskPatch(BaseModel):
    title: Optional[str] = None
    instruction: Optional[str] = None
    persona: Optional[str] = None
    profile: Optional[str] = None
    task_type: Optional[str] = None
    llm_model_override: Optional[str] = None
    schedule: Optional[str] = None
    deliver: Optional[bool] = None
    requires_approval: Optional[bool] = None
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    active_hours_tz: Optional[str] = None
    recipe_family: Optional[str] = None
    recipe_params: Optional[Dict[str, Any]] = None
    # Approval flow: set approved=True/False to resume or cancel a waiting_approval task
    approved: Optional[bool] = None


class AgentInstanceCreate(BaseModel):
    preset_id: str
    display_name: str
    enabled: bool = True
    auto_maintain_task: bool = True
    schedule: Optional[str] = None
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    active_hours_tz: Optional[str] = None
    llm_model_override: Optional[str] = None
    context_paths: Optional[List[str]] = None
    params: Optional[Dict[str, Any]] = None


class AgentInstancePatch(BaseModel):
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    auto_maintain_task: Optional[bool] = None
    schedule: Optional[str] = None
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    active_hours_tz: Optional[str] = None
    llm_model_override: Optional[str] = None
    context_paths: Optional[List[str]] = None
    params: Optional[Dict[str, Any]] = None


class AgentInstanceReconcileRequest(BaseModel):
    recreate_missing: bool = True


class AgentInstanceTaskSummary(BaseModel):
    id: int
    title: str
    status: str
    schedule: Optional[str] = None
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None


class AgentInstanceLatestRunSummary(BaseModel):
    id: int
    status: str
    run_kind: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    summary: Optional[str] = None
    error: Optional[str] = None


class AgentInstanceOut(BaseModel):
    id: int
    preset_id: str
    display_name: str
    category: str
    category_display_name: str
    when_to_use: str
    execution_mode: str
    background: bool
    enabled: bool
    auto_maintain_task: bool
    schedule: Optional[str] = None
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    active_hours_tz: Optional[str] = None
    llm_model_override: Optional[str] = None
    context_paths: List[str]
    params: Dict[str, Any]
    linked_task: Optional[AgentInstanceTaskSummary] = None
    latest_run: Optional[AgentInstanceLatestRunSummary] = None


class TaskOut(BaseModel):
    id: int
    title: str
    instruction: str
    persona: Optional[str]
    profile: Optional[str]
    llm_model_override: Optional[str]
    task_type: str
    status: str
    schedule: Optional[str]
    deliver: bool
    requires_approval: bool
    result: Optional[str]
    result_markdown: Optional[str]
    result_format: Optional[str]
    result_sections: List[Dict[str, Any]]
    error: Optional[str]
    active_hours_start: Optional[str]
    active_hours_end: Optional[str]
    active_hours_tz: Optional[str]
    retry_count: int
    current_step_index: Optional[int]
    current_step_title: Optional[str]
    waiting_approval_tool: Optional[str]
    has_plan: bool
    plan_version: int
    created_at: Optional[datetime]
    last_run_at: Optional[datetime]
    next_run_at: Optional[datetime]
    effective_timezone: str
    created_at_localized: Optional[str]
    last_run_at_localized: Optional[str]
    next_run_at_localized: Optional[str]
    task_recipe: Optional[Dict[str, Any]]
    resolved_agent: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class ApprovedMemoryOut(BaseModel):
    id: int
    memory_type: str
    content: str
    importance: float
    tags: List[str]
    expires_at: Optional[datetime]
    created_at: Optional[datetime]


class MemoryCandidateApprovalOut(BaseModel):
    task_id: int
    run_id: int
    candidate_index: int
    candidate: Dict[str, Any]
    memory: ApprovedMemoryOut


# ── Helpers ───────────────────────────────────────────────────────────────────

# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        task = await create_task_record(
            db,
            user_id=current_user.id,
            title=body.title,
            instruction=body.instruction,
            persona=body.persona,
            profile=body.profile,
            llm_model_override=body.llm_model_override,
            task_type=body.task_type,
            schedule=body.schedule,
            deliver=body.deliver,
            requires_approval=body.requires_approval,
            active_hours_start=body.active_hours_start,
            active_hours_end=body.active_hours_end,
            active_hours_tz=body.active_hours_tz,
            recipe_family=body.recipe_family,
            recipe_params=body.recipe_params,
            user_timezone=current_user.active_hours_tz,
        )
    except TaskValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_task_out(task, None, user_timezone=current_user.active_hours_tz)


@router.get("/tasks", response_model=List[TaskOut])
async def list_tasks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Task)
        .where(Task.user_id == current_user.id)
        .order_by(desc(Task.created_at))
    )
    tasks = result.scalars().all()
    step_lookup = await _load_current_steps(db, tasks)
    final_output_lookup = await _load_latest_final_outputs(db, tasks)
    return [
        _to_task_out(
            task,
            step_lookup.get((task.id, task.current_step_index)),
            user_timezone=current_user.active_hours_tz,
            final_output=final_output_lookup.get(task.id),
        )
        for task in tasks
    ]


@router.post("/tasks/agent-instances/ensure-defaults", response_model=List[AgentInstanceOut])
async def ensure_agent_instances_defaults(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        rows = await ensure_seed_agent_instances(db, user=current_user)
    except Exception:
        log.exception("Failed to ensure default agent instances for user_id=%s", current_user.id)
        rows = await list_agent_instances(db, user_id=int(current_user.id))
    return await _serialize_agent_instances(db, rows)


@router.get("/tasks/agent-instances", response_model=List[AgentInstanceOut])
async def list_agent_instances_endpoint(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_agent_instances(db, user_id=int(current_user.id))
    return await _serialize_agent_instances(db, rows)


@router.post("/tasks/agent-instances", response_model=AgentInstanceOut, status_code=status.HTTP_201_CREATED)
async def create_agent_instance_endpoint(
    body: AgentInstanceCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        agent_instance = await create_agent_instance(
            db,
            user=current_user,
            preset_id=body.preset_id,
            display_name=body.display_name,
            enabled=body.enabled,
            auto_maintain_task=body.auto_maintain_task,
            schedule=body.schedule,
            active_hours_start=body.active_hours_start,
            active_hours_end=body.active_hours_end,
            active_hours_tz=body.active_hours_tz,
            llm_model_override=body.llm_model_override,
            context_paths=body.context_paths,
            params=body.params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    payload = await _serialize_agent_instances(db, [agent_instance])
    return payload[0]


@router.patch("/tasks/agent-instances/{instance_id}", response_model=AgentInstanceOut)
async def update_agent_instance_endpoint(
    instance_id: int,
    body: AgentInstancePatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent_instance = await get_agent_instance(db, user_id=int(current_user.id), instance_id=instance_id)
    if agent_instance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent instance not found")
    try:
        await update_agent_instance(
            db,
            agent_instance=agent_instance,
            user=current_user,
            display_name=body.display_name,
            enabled=body.enabled,
            auto_maintain_task=body.auto_maintain_task,
            schedule=body.schedule,
            active_hours_start=body.active_hours_start,
            active_hours_end=body.active_hours_end,
            active_hours_tz=body.active_hours_tz,
            llm_model_override=body.llm_model_override,
            context_paths=body.context_paths,
            params=body.params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    payload = await _serialize_agent_instances(db, [agent_instance])
    return payload[0]


@router.post("/tasks/agent-instances/{instance_id}/reconcile", response_model=AgentInstanceOut)
async def reconcile_agent_instance_endpoint(
    instance_id: int,
    body: AgentInstanceReconcileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent_instance = await get_agent_instance(db, user_id=int(current_user.id), instance_id=instance_id)
    if agent_instance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent instance not found")
    await reconcile_agent_instance(
        db,
        agent_instance=agent_instance,
        user=current_user,
        recreate_missing=body.recreate_missing,
    )
    payload = await _serialize_agent_instances(db, [agent_instance])
    return payload[0]


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_owned_task(task_id, current_user.id, db)
    step_lookup = await _load_current_steps(db, [task])
    final_output_lookup = await _load_latest_final_outputs(db, [task])
    return _to_task_out(
        task,
        step_lookup.get((task.id, task.current_step_index)),
        user_timezone=current_user.active_hours_tz,
        final_output=final_output_lookup.get(task.id),
    )


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: int,
    body: TaskPatch,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_owned_task(task_id, current_user.id, db)
    # Approval flow
    if body.approved is not None:
        if task.status != "waiting_approval":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Task is not waiting for approval (status={task.status})",
            )
        if body.approved:
            task.status = "pending"
            task.next_run_at = datetime.now(timezone.utc)
            task.pre_approved = True
            await db.commit()
            try:
                from app.autonomy.runner import get_task_runner
                runner = get_task_runner()
                background_tasks.add_task(runner.execute, task)
            except ImportError:
                pass
        else:
            task.status = "cancelled"
        step_lookup = await _load_current_steps(db, [task])
        return _to_task_out(task, step_lookup.get((task.id, task.current_step_index)), user_timezone=current_user.active_hours_tz)

    try:
        fields_set = getattr(body, "model_fields_set", set())
        await update_task_record(
            db,
            task,
            title=body.title if "title" in fields_set else UNSET,
            instruction=body.instruction if "instruction" in fields_set else UNSET,
            persona=body.persona if "persona" in fields_set else UNSET,
            profile=body.profile if "profile" in fields_set else UNSET,
            task_type=body.task_type if "task_type" in fields_set else UNSET,
            llm_model_override=body.llm_model_override if "llm_model_override" in fields_set else UNSET,
            schedule=body.schedule if "schedule" in fields_set else UNSET,
            deliver=body.deliver if "deliver" in fields_set else UNSET,
            requires_approval=body.requires_approval if "requires_approval" in fields_set else UNSET,
            active_hours_start=body.active_hours_start if "active_hours_start" in fields_set else UNSET,
            active_hours_end=body.active_hours_end if "active_hours_end" in fields_set else UNSET,
            active_hours_tz=body.active_hours_tz if "active_hours_tz" in fields_set else UNSET,
            recipe_family=body.recipe_family if "recipe_family" in fields_set else UNSET,
            recipe_params=body.recipe_params if "recipe_params" in fields_set else UNSET,
            user_timezone=current_user.active_hours_tz,
        )
    except TaskValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    step_lookup = await _load_current_steps(db, [task])
    return _to_task_out(task, step_lookup.get((task.id, task.current_step_index)), user_timezone=current_user.active_hours_tz)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_owned_task(task_id, current_user.id, db)
    if task.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is running. Stop it before deleting.",
        )
    await db.delete(task)


@router.post("/tasks/{task_id}/stop", response_model=TaskOut)
async def stop_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_owned_task(task_id, current_user.id, db)

    if task.status in {"completed", "failed", "cancelled"}:
        step_lookup = await _load_current_steps(db, [task])
        return _to_task_out(task, step_lookup.get((task.id, task.current_step_index)), user_timezone=current_user.active_hours_tz)

    from app.autonomy.runner import get_task_runner

    await get_task_runner().request_stop(task.id)

    now = datetime.now(timezone.utc)
    task.status = "cancelled"
    task.next_run_at = None
    task.next_retry_at = None
    task.pre_approved = False
    task.last_run_at = now
    task.error = "Stopped by user"

    if task.current_step_index is not None:
        rows = await db.execute(
            select(TaskStep).where(
                TaskStep.task_id == task.id,
                TaskStep.step_index == task.current_step_index,
            )
        )
        step = rows.scalar_one_or_none()
        if step is not None and step.status in {"pending", "running", "waiting_approval"}:
            step.status = "skipped"
            step.error = "Stopped by user"
            step.waiting_approval_tool = None

    run_rows = await db.execute(
        select(TaskRun)
        .where(
            TaskRun.task_id == task.id,
            TaskRun.status.in_(["running", "waiting_approval"]),
        )
        .order_by(desc(TaskRun.started_at))
    )
    run = run_rows.scalars().first()
    if run is not None:
        run.status = "cancelled"
        run.finished_at = now
        run.error = "Stopped by user"

    step_lookup = await _load_current_steps(db, [task])
    return _to_task_out(task, step_lookup.get((task.id, task.current_step_index)), user_timezone=current_user.active_hours_tz)


@router.post("/tasks/{task_id}/run", response_model=Dict[str, Any])
async def manual_run(
    task_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enqueue the task for immediate execution (dev/testing)."""
    task = await _get_owned_task(task_id, current_user.id, db)
    run_rows = await db.execute(
        select(TaskRun)
        .where(
            TaskRun.task_id == task.id,
            TaskRun.status.in_(["running", "waiting_approval"]),
        )
        .order_by(desc(TaskRun.started_at))
    )
    if task.status == "running" or run_rows.scalars().first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is already running",
        )

    task.next_run_at = datetime.now(timezone.utc)
    task.status = "pending"
    await db.commit()
    await db.refresh(task)

    try:
        from app.autonomy.runner import get_task_runner
        runner = get_task_runner()
        background_tasks.add_task(runner.execute, task, trigger_source="manual")
    except ImportError:
        pass  # Runner not yet wired (Sprint 4.2) — next scheduler tick will pick it up

    return {"queued": True, "task_id": task_id}


# ── POST /tasks/{id}/reset ───────────────────────────────────────────────────

class TaskResetRequest(BaseModel):
    requeue: bool = True   # True → reset to pending; False → cancel


@router.post("/tasks/{task_id}/reset", response_model=TaskOut)
async def reset_task(
    task_id: int,
    body: TaskResetRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Recover a task that is stuck in 'running' after a server restart.

    requeue=true  (default) — sets status back to 'pending' so the scheduler
                               picks it up on the next tick.
    requeue=false            — marks the task 'cancelled' to abandon it.

    Returns 409 if the task is not currently in the 'running' state.
    """
    task = await _get_owned_task(task_id, current_user.id, db)
    if task.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task is not stuck (status={task.status}). Only 'running' tasks can be reset.",
        )
    if body.requeue:
        task.status = "pending"
        task.next_run_at = datetime.now(timezone.utc)
    else:
        task.status = "cancelled"
    step_lookup = await _load_current_steps(db, [task])
    return _to_task_out(task, step_lookup.get((task.id, task.current_step_index)), user_timezone=current_user.active_hours_tz)


# ── GET /tasks/{id}/audit ─────────────────────────────────────────────────────

class TaskAuditEntry(BaseModel):
    tool: str
    arguments: Dict[str, Any]
    result_summary: str
    created_at: datetime

class TaskAuditRunSummary(BaseModel):
    id: int
    status: str
    summary: Optional[str] = None
    error: Optional[str] = None
    run_kind: str
    agent_role: Optional[str] = None
    trigger_source: Optional[str] = None
    source_context: Optional[Dict[str, Any] | str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    artifact_count: int = 0
    artifact_types: List[str] = []

class TaskAuditOut(BaseModel):
    task_id: int
    title: str
    result: Optional[str]
    resolved_agent: Optional[Dict[str, Any]] = None
    latest_run: Optional[TaskAuditRunSummary] = None
    tool_calls: List[TaskAuditEntry]


class TaskStepOut(BaseModel):
    id: int
    step_index: int
    title: str
    instruction: str
    status: str
    requires_approval: bool
    output_summary: Optional[str]
    error: Optional[str]
    waiting_approval_tool: Optional[str]

    class Config:
        from_attributes = True


class TaskStepPatch(BaseModel):
    title: Optional[str] = None
    instruction: Optional[str] = None
    status: Optional[str] = None
    requires_approval: Optional[bool] = None


class TaskPlanRequest(BaseModel):
    goal: str
    max_steps: int = settings.task_plan_default_steps
    notes: Optional[str] = ""
    style: Optional[str] = "concise"


class TaskPlanOut(BaseModel):
    task_id: int
    steps_created: int
    titles: List[str]
    plan_version: int


class TaskResultExportRequest(BaseModel):
    path: str
    artifact_type: Optional[str] = "final_output"
    overwrite: bool = True


class TaskResultExportOut(BaseModel):
    exported: bool
    task_id: int
    run_id: int
    path: str
    source_artifact_type: str


def _artifact_export_text(artifact: TaskRunArtifact | None) -> str:
    if artifact is None:
        return ""
    if artifact.content_text:
        return artifact.content_text
    if artifact.content_json:
        try:
            payload = json.loads(artifact.content_json)
        except Exception:
            return artifact.content_json
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, indent=2, sort_keys=True)
    return ""


@router.post("/tasks/{task_id}/export-result-file", response_model=TaskResultExportOut)
async def export_task_result_file(
    task_id: int,
    body: TaskResultExportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskResultExportOut:
    task = await _get_owned_task(task_id, current_user.id, db)

    run_rows = await db.execute(
        select(TaskRun)
        .where(TaskRun.task_id == task.id)
        .order_by(desc(TaskRun.started_at), desc(TaskRun.id))
    )
    run = run_rows.scalars().first()
    if run is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task has no runs to export")

    artifact_type = str(body.artifact_type or "final_output").strip() or "final_output"
    artifact_rows = await db.execute(
        select(TaskRunArtifact)
        .where(TaskRunArtifact.task_run_id == run.id, TaskRunArtifact.artifact_type == artifact_type)
        .order_by(desc(TaskRunArtifact.created_at), desc(TaskRunArtifact.id))
    )
    artifact = artifact_rows.scalars().first()

    content = _artifact_export_text(artifact)
    if not content:
        content = str(run.summary or task.result or "").strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No exportable content found for artifact_type={artifact_type}",
        )

    workspace_root, workspace_path = resolve_workspace_path_for_user(task.user_id, body.path)
    if workspace_path.exists() and not body.overwrite:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Target file already exists")

    write_workspace_text(task.user_id, body.path, content.rstrip() + "\n")

    relative_path = str(workspace_path.relative_to(workspace_root)).replace("\\", "/")
    db.add(
        TaskRunArtifact(
            task_run_id=run.id,
            artifact_type="workspace_export",
            content_json=json.dumps(
                {
                    "path": relative_path,
                    "source_artifact_type": artifact_type,
                    "overwrite": bool(body.overwrite),
                }
            ),
        )
    )
    await db.commit()

    return TaskResultExportOut(
        exported=True,
        task_id=task.id,
        run_id=run.id,
        path=relative_path,
        source_artifact_type=artifact_type,
    )

@router.get("/tasks/{task_id}/audit", response_model=TaskAuditOut)
async def get_task_audit(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskAuditOut:
    """Return the task's result and all tool calls from its last execution session."""
    task = await _get_owned_task(task_id, current_user.id, db)
    tool_calls: List[TaskAuditEntry] = []
    latest_run: Optional[TaskAuditRunSummary] = None
    if task.last_session_id:
        rows = await db.execute(
            select(AuditLog)
            .where(AuditLog.session_id == task.last_session_id)
            .order_by(AuditLog.created_at)
        )
        tool_calls = [
            TaskAuditEntry(
                tool=r.tool,
                arguments=json.loads(r.arguments or "{}"),
                result_summary=r.result_summary or "",
                created_at=r.created_at,
            )
            for r in rows.scalars().all()
        ]
    run = (
        await db.execute(
            select(TaskRun)
            .where(TaskRun.task_id == task.id)
            .order_by(desc(TaskRun.started_at), desc(TaskRun.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is not None:
        artifact_rows = await db.execute(
            select(TaskRunArtifact.artifact_type)
            .where(TaskRunArtifact.task_run_id == run.id)
            .order_by(desc(TaskRunArtifact.created_at), desc(TaskRunArtifact.id))
        )
        artifact_types = [str(item) for item in artifact_rows.scalars().all() if str(item)]
        latest_run = TaskAuditRunSummary(
            id=run.id,
            status=run.status,
            summary=run.summary,
            error=run.error,
            run_kind=(run.run_kind or "task"),
            agent_role=run.agent_role,
            trigger_source=run.trigger_source,
            source_context=run.source_context,
            started_at=run.started_at,
            finished_at=run.finished_at,
            artifact_count=len(artifact_types),
            artifact_types=artifact_types,
        )
    return TaskAuditOut(
        task_id=task.id,
        title=task.title,
        result=task.result,
        resolved_agent=_resolved_agent_summary_for_task(task),
        latest_run=latest_run,
        tool_calls=tool_calls,
    )


@router.get("/tasks/{task_id}/steps", response_model=List[TaskStepOut])
async def list_task_steps(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[TaskStep]:
    await _get_owned_task(task_id, current_user.id, db)
    rows = await db.execute(
        select(TaskStep)
        .where(TaskStep.task_id == task_id)
        .order_by(TaskStep.step_index)
    )
    return rows.scalars().all()


@router.patch("/tasks/{task_id}/steps/{step_id}", response_model=TaskStepOut)
async def update_task_step(
    task_id: int,
    step_id: int,
    body: TaskStepPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskStep:
    await _get_owned_task(task_id, current_user.id, db)
    rows = await db.execute(
        select(TaskStep).where(TaskStep.id == step_id, TaskStep.task_id == task_id)
    )
    step = rows.scalar_one_or_none()
    if step is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")

    if body.title is not None:
        step.title = body.title
    if body.instruction is not None:
        step.instruction = body.instruction
    if body.requires_approval is not None:
        step.requires_approval = body.requires_approval
    if body.status is not None:
        allowed = {"pending", "running", "waiting_approval", "succeeded", "failed", "skipped"}
        if body.status not in allowed:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"status must be one of: {', '.join(sorted(allowed))}",
            )
        step.status = body.status
    await db.flush()
    return step


@router.post("/tasks/{task_id}/plan", response_model=TaskPlanOut)
async def create_task_plan(
    task_id: int,
    body: TaskPlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskPlanOut:
    try:
        out = await create_task_plan_for_user(
            db,
            task_id=task_id,
            user_id=current_user.id,
            goal=body.goal,
            max_steps=body.max_steps,
            notes=body.notes or "",
            style=body.style or "concise",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return TaskPlanOut(**out)


@router.post(
    "/tasks/{task_id}/runs/{run_id}/memory-candidates/{candidate_index}/approve",
    response_model=MemoryCandidateApprovalOut,
)
async def approve_task_run_memory_candidate(
    task_id: int,
    run_id: int,
    candidate_index: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_owned_task(task_id, current_user.id, db)
    run = await _get_owned_task_run(task_id, run_id, current_user.id, db)
    artifact = await _get_memory_candidate_artifact(run.id, db)
    payload = _decode_memory_candidate_artifact(artifact)
    candidates = payload.get("candidates") or []
    if candidate_index < 0 or candidate_index >= len(candidates):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory candidate not found")

    candidate = candidates[candidate_index]
    if not isinstance(candidate, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Memory candidate payload is malformed")

    status_value = str(candidate.get("status") or "pending").strip().lower()
    if status_value == "approved":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Memory candidate already approved")
    if status_value != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Memory candidate is not approvable (status={status_value})")

    memory_type = str(candidate.get("memory_type") or "").strip()
    content = str(candidate.get("content") or "").strip()
    if memory_type not in {"semantic", "procedural", "episodic"} or not content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Memory candidate payload is malformed")

    expires_at = candidate.get("expires_at")
    if expires_at is not None and not isinstance(expires_at, str):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Memory candidate payload is malformed")

    svc = get_memory_service()
    result = await svc.create(
        db=db,
        user_id=current_user.id,
        memory_type=memory_type,
        content=content,
        importance=0.65,
        tags=_topic_watcher_candidate_tags(candidate),
        expires_at=_parse_optional_iso_datetime(expires_at) if expires_at else None,
    )
    if isinstance(result, str):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result)

    approved_at = datetime.now(timezone.utc)
    candidate["status"] = "approved"
    candidate["approved_memory_id"] = int(result.id)
    candidate["approved_at"] = approved_at.isoformat()
    candidate["approved_by_user_id"] = int(current_user.id)
    artifact.content_json = json.dumps(payload)
    await db.flush()

    return MemoryCandidateApprovalOut(
        task_id=task.id,
        run_id=run.id,
        candidate_index=candidate_index,
        candidate=candidate,
        memory=ApprovedMemoryOut(
            id=result.id,
            memory_type=result.memory_type,
            content=result.content,
            importance=result.importance,
            tags=result.tags_list,
            expires_at=result.expires_at,
            created_at=result.created_at,
        ),
    )


# ── Internal helper ───────────────────────────────────────────────────────────

async def _get_owned_task(task_id: int, user_id: int, db: AsyncSession) -> Task:
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


async def _get_owned_task_run(task_id: int, run_id: int, user_id: int, db: AsyncSession) -> TaskRun:
    result = await db.execute(
        select(TaskRun)
        .join(Task, TaskRun.task_id == Task.id)
        .where(TaskRun.id == run_id, TaskRun.task_id == task_id, Task.user_id == user_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task run not found")
    return run


async def _get_memory_candidate_artifact(task_run_id: int, db: AsyncSession) -> TaskRunArtifact:
    result = await db.execute(
        select(TaskRunArtifact)
        .where(TaskRunArtifact.task_run_id == task_run_id, TaskRunArtifact.artifact_type == "memory_candidates")
        .order_by(desc(TaskRunArtifact.id))
        .limit(1)
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory candidates not found for task run")
    return artifact


def _decode_memory_candidate_artifact(artifact: TaskRunArtifact) -> Dict[str, Any]:
    try:
        payload = json.loads(artifact.content_json or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Memory candidate payload is malformed",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Memory candidate payload is malformed")
    return payload


def _topic_watcher_candidate_tags(candidate: Dict[str, Any]) -> List[str]:
    tags = ["topic_watcher"]
    topic = str(candidate.get("topic") or "").strip().lower()
    if topic:
        slug = re.sub(r"[^a-z0-9]+", "_", topic).strip("_")
        if slug:
            tags.append(slug)
    return tags


def _parse_optional_iso_datetime(raw: str | None) -> Optional[datetime]:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Memory candidate payload is malformed") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _to_task_out(
    task: Task,
    current_step: Optional[TaskStep],
    *,
    user_timezone: Optional[str],
    final_output: Optional[str] = None,
) -> TaskOut:
    waiting_tool: Optional[str] = None
    if task.status == "waiting_approval" and current_step is not None:
        waiting_tool = current_step.waiting_approval_tool
    effective_timezone = resolve_effective_timezone(task.active_hours_tz, user_timezone)
    result_markdown = _normalize_result_markdown(final_output or task.result)
    result_format = "markdown" if result_markdown else None
    resolved_agent = _resolved_agent_summary_for_task(task)

    return TaskOut(
        id=task.id,
        title=task.title,
        instruction=task.instruction,
        persona=task.persona,
        profile=task.profile,
        llm_model_override=task.llm_model_override,
        task_type=task.task_type,
        status=task.status,
        schedule=task.schedule,
        deliver=task.deliver,
        requires_approval=task.requires_approval,
        result=task.result,
        result_markdown=result_markdown,
        result_format=result_format,
        result_sections=_split_result_sections(result_markdown),
        error=task.error,
        active_hours_start=task.active_hours_start,
        active_hours_end=task.active_hours_end,
        active_hours_tz=task.active_hours_tz,
        retry_count=task.retry_count,
        current_step_index=task.current_step_index,
        current_step_title=current_step.title if current_step is not None else None,
        waiting_approval_tool=waiting_tool,
        has_plan=task.has_plan,
        plan_version=task.plan_version,
        created_at=task.created_at,
        last_run_at=task.last_run_at,
        next_run_at=task.next_run_at,
        effective_timezone=effective_timezone,
        created_at_localized=format_localized_datetime(task.created_at, timezone_name=effective_timezone) or None,
        last_run_at_localized=format_localized_datetime(task.last_run_at, timezone_name=effective_timezone) or None,
        next_run_at_localized=format_localized_datetime(task.next_run_at, timezone_name=effective_timezone) or None,
        task_recipe=(task.task_recipe or None) if hasattr(task, "task_recipe") else None,
        resolved_agent=resolved_agent,
    )


def _resolved_agent_summary(preset: FruitcakeAgentPreset | None) -> Optional[Dict[str, Any]]:
    if preset is None:
        return None
    return {
        "id": preset.preset_id,
        "display_name": preset.display_name,
        "category": preset.category_id,
        "category_display_name": preset.category_display_name,
        "execution_mode": preset.execution_mode,
        "background": preset.background,
        "memory_scope": preset.memory_scope,
        "persona_compatibility": preset.persona_compatibility,
        "when_to_use": preset.when_to_use,
    }


def _resolved_agent_summary_for_task(task: Task) -> Optional[Dict[str, Any]]:
    recipe = task.task_recipe if isinstance(task.task_recipe, dict) else {}
    family = str(recipe.get("family") or "").strip().lower()
    if family != "agent":
        return None
    params = recipe.get("params") if isinstance(recipe.get("params"), dict) else {}
    agent_role = str(params.get("agent_role") or "").strip()
    if not agent_role:
        return None
    return _resolved_agent_summary(get_agent_preset(agent_role))


async def _serialize_agent_instances(
    db: AsyncSession,
    rows: List[ManagedAgentPreset],
) -> List[AgentInstanceOut]:
    out: List[AgentInstanceOut] = []
    for row in rows:
        preset = get_agent_preset(row.preset_id)
        if preset is None:
            continue
        linked_task: Optional[Task] = None
        if row.linked_task_id is not None:
            linked_task = (
                await db.execute(select(Task).where(Task.id == int(row.linked_task_id)))
            ).scalar_one_or_none()
            if linked_task is None:
                row.linked_task_id = None
        latest_run: Optional[AgentInstanceLatestRunSummary] = None
        linked_task_summary: Optional[AgentInstanceTaskSummary] = None
        if linked_task is not None:
            linked_task_summary = AgentInstanceTaskSummary(
                id=int(linked_task.id),
                title=linked_task.title,
                status=linked_task.status,
                schedule=linked_task.schedule,
                last_run_at=linked_task.last_run_at,
                next_run_at=linked_task.next_run_at,
            )
            run = (
                await db.execute(
                    select(TaskRun)
                    .where(TaskRun.task_id == linked_task.id)
                    .order_by(desc(TaskRun.started_at), desc(TaskRun.id))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if run is not None:
                latest_run = AgentInstanceLatestRunSummary(
                    id=int(run.id),
                    status=run.status,
                    run_kind=run.run_kind or "task",
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    summary=run.summary,
                    error=run.error,
                )
        out.append(
            AgentInstanceOut(
                id=int(row.id),
                preset_id=preset.preset_id,
                display_name=row.display_name,
                category=preset.category_id,
                category_display_name=preset.category_display_name,
                when_to_use=preset.when_to_use,
                execution_mode=preset.execution_mode,
                background=preset.background,
                enabled=row.enabled,
                auto_maintain_task=row.auto_maintain_task,
                schedule=row.schedule,
                active_hours_start=row.active_hours_start,
                active_hours_end=row.active_hours_end,
                active_hours_tz=row.active_hours_tz,
                llm_model_override=row.llm_model_override,
                context_paths=row.context_paths,
                params=row.params if isinstance(row.params, dict) else {},
                linked_task=linked_task_summary,
                latest_run=latest_run,
            )
        )
    return out


async def _load_current_steps(db: AsyncSession, tasks: List[Task]) -> Dict[tuple[int, int], TaskStep]:
    keys = [
        (task.id, task.current_step_index)
        for task in tasks
        if task.current_step_index is not None
    ]
    if not keys:
        return {}

    rows = await db.execute(
        select(TaskStep).where(tuple_(TaskStep.task_id, TaskStep.step_index).in_(keys))
    )
    steps = rows.scalars().all()
    return {(step.task_id, step.step_index): step for step in steps}


async def _load_latest_final_outputs(db: AsyncSession, tasks: List[Task]) -> Dict[int, str]:
    task_ids = [int(task.id) for task in tasks if getattr(task, "id", None) is not None]
    if not task_ids:
        return {}

    rows = await db.execute(
        select(TaskRun.task_id, TaskRunArtifact.content_text, TaskRunArtifact.content_json)
        .join(TaskRun, TaskRunArtifact.task_run_id == TaskRun.id)
        .where(
            TaskRun.task_id.in_(task_ids),
            TaskRunArtifact.artifact_type == "final_output",
        )
        .order_by(TaskRun.task_id, desc(TaskRun.started_at), desc(TaskRun.id), desc(TaskRunArtifact.id))
    )

    out: Dict[int, str] = {}
    for task_id, content_text, content_json in rows.all():
        task_id = int(task_id)
        if task_id in out:
            continue
        text = _coerce_artifact_text(content_text, content_json)
        if text:
            out[task_id] = text
    return out


def _coerce_artifact_text(content_text: Any, content_json: Any) -> str:
    text = str(content_text or "").strip()
    if text:
        return text
    if content_json is None:
        return ""
    if isinstance(content_json, str):
        return content_json.strip()
    try:
        return json.dumps(content_json, ensure_ascii=False, indent=2).strip()
    except Exception:
        return str(content_json).strip()


def _normalize_result_markdown(text: Optional[str]) -> Optional[str]:
    value = str(text or "").strip()
    return value or None


def _split_result_sections(text: Optional[str]) -> List[Dict[str, Any]]:
    value = str(text or "").strip()
    if not value:
        return []

    heading_re = re.compile(r"^##\s+(.+?)\s*$")
    sections: List[Dict[str, Any]] = []
    current_heading: Optional[str] = None
    current_lines: List[str] = []

    def flush() -> None:
        nonlocal current_heading, current_lines
        body = "\n".join(current_lines).strip()
        if current_heading is None and not body:
            current_lines = []
            return
        if current_heading is not None or body:
            lowered = body.lower()
            sections.append(
                {
                    "heading": current_heading,
                    "body": body,
                    "is_empty_state": any(
                        marker in lowered
                        for marker in (
                            "no events scheduled",
                            "no update available",
                            "unavailable",
                            "no items",
                        )
                    ),
                }
            )
        current_lines = []

    for line in value.splitlines():
        match = heading_re.match(line.strip())
        if match:
            flush()
            current_heading = match.group(1).strip()
            continue
        current_lines.append(line)

    flush()
    return sections
