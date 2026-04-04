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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

import json
import re

from app.autonomy.planner import create_task_plan_for_user
from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import AuditLog, Task, TaskRun, TaskRunArtifact, TaskStep, User
from app.db.session import get_db
from app.memory.service import get_memory_service
from app.task_service import TaskValidationError, UNSET, create_task_record, update_task_record
from app.time_utils import format_localized_datetime, resolve_effective_timezone

router = APIRouter()


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
    return [
        _to_task_out(task, step_lookup.get((task.id, task.current_step_index)), user_timezone=current_user.active_hours_tz)
        for task in tasks
    ]


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_owned_task(task_id, current_user.id, db)
    step_lookup = await _load_current_steps(db, [task])
    return _to_task_out(task, step_lookup.get((task.id, task.current_step_index)), user_timezone=current_user.active_hours_tz)


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: int,
    body: TaskPatch,
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
        else:
            task.status = "cancelled"
        step_lookup = await _load_current_steps(db, [task])
        return _to_task_out(task, step_lookup.get((task.id, task.current_step_index)), user_timezone=current_user.active_hours_tz)

    try:
        fields_set = getattr(body, "model_fields_set", set())
        await update_task_record(
            db,
            task,
            title=body.title if body.title is not None else UNSET,
            instruction=body.instruction if body.instruction is not None else UNSET,
            persona=body.persona if body.persona is not None else UNSET,
            profile=body.profile if body.profile is not None else UNSET,
            llm_model_override=body.llm_model_override if "llm_model_override" in fields_set else UNSET,
            schedule=body.schedule if body.schedule is not None else UNSET,
            deliver=body.deliver if body.deliver is not None else UNSET,
            requires_approval=body.requires_approval if body.requires_approval is not None else UNSET,
            active_hours_start=body.active_hours_start if body.active_hours_start is not None else UNSET,
            active_hours_end=body.active_hours_end if body.active_hours_end is not None else UNSET,
            active_hours_tz=body.active_hours_tz if body.active_hours_tz is not None else UNSET,
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

    try:
        from app.autonomy.runner import get_task_runner
        runner = get_task_runner()
        background_tasks.add_task(runner.execute, task)
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

class TaskAuditOut(BaseModel):
    task_id: int
    title: str
    result: Optional[str]
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

@router.get("/tasks/{task_id}/audit", response_model=TaskAuditOut)
async def get_task_audit(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskAuditOut:
    """Return the task's result and all tool calls from its last execution session."""
    task = await _get_owned_task(task_id, current_user.id, db)
    tool_calls: List[TaskAuditEntry] = []
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
    return TaskAuditOut(
        task_id=task.id,
        title=task.title,
        result=task.result,
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


def _to_task_out(task: Task, current_step: Optional[TaskStep], *, user_timezone: Optional[str]) -> TaskOut:
    waiting_tool: Optional[str] = None
    if task.status == "waiting_approval" and current_step is not None:
        waiting_tool = current_step.waiting_approval_tool
    effective_timezone = resolve_effective_timezone(task.active_hours_tz, user_timezone)

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
    )


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
