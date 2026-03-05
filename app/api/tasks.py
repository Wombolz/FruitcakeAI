"""
FruitcakeAI v5 — Tasks API (Phase 4)

POST   /tasks              Create task, compute next_run_at
GET    /tasks              List user's tasks (most recent first)
GET    /tasks/{id}         Task detail + last result
PATCH  /tasks/{id}         Update or approve/reject
DELETE /tasks/{id}         Cancel task (status=cancelled)
POST   /tasks/{id}/run     Manual trigger (dev/testing)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

import json

from app.auth.dependencies import get_current_user
from app.db.models import AuditLog, Task, User
from app.db.session import get_db

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    title: str
    instruction: str
    task_type: str = "one_shot"          # "one_shot" | "recurring"
    schedule: Optional[str] = None       # "every:30m" | cron | ISO timestamp
    deliver: bool = True
    requires_approval: bool = False
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    active_hours_tz: Optional[str] = None


class TaskPatch(BaseModel):
    title: Optional[str] = None
    instruction: Optional[str] = None
    schedule: Optional[str] = None
    deliver: Optional[bool] = None
    requires_approval: Optional[bool] = None
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    active_hours_tz: Optional[str] = None
    # Approval flow: set approved=True/False to resume or cancel a waiting_approval task
    approved: Optional[bool] = None


class TaskOut(BaseModel):
    id: int
    title: str
    instruction: str
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
    created_at: Optional[datetime]
    last_run_at: Optional[datetime]
    next_run_at: Optional[datetime]

    class Config:
        from_attributes = True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_next_run_at(schedule: str | None) -> datetime | None:
    """
    Parse a schedule expression and return the next run time.
    Accepts: "every:30m", cron expression, or ISO 8601 timestamp.
    Defers to app.autonomy.scheduler when that module is available.
    """
    if not schedule:
        return None
    try:
        from app.autonomy.scheduler import compute_next_run_at
        return compute_next_run_at(schedule)
    except ImportError:
        # Scheduler not yet wired (Sprint 4.2) — fall back to basic interval parsing
        return _simple_next_run(schedule)


def _simple_next_run(schedule: str) -> datetime | None:
    """Minimal fallback: handle 'every:Xs/m/h/d' and ISO timestamps."""
    now = datetime.now(timezone.utc)
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
    # Try ISO timestamp
    try:
        return datetime.fromisoformat(schedule).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = Task(
        user_id=current_user.id,
        title=body.title,
        instruction=body.instruction,
        task_type=body.task_type,
        status="pending",
        schedule=body.schedule,
        deliver=body.deliver,
        requires_approval=body.requires_approval,
        active_hours_start=body.active_hours_start,
        active_hours_end=body.active_hours_end,
        active_hours_tz=body.active_hours_tz,
        next_run_at=_compute_next_run_at(body.schedule),
    )
    db.add(task)
    await db.flush()
    return task


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
    return result.scalars().all()


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_owned_task(task_id, current_user.id, db)
    return task


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
            # Re-schedule for immediate re-run with pre_approved flag
            task.status = "pending"
            task.next_run_at = datetime.now(timezone.utc)
            # The runner checks a task-level flag; signal via error field temporarily
            task.error = "__pre_approved__"
        else:
            task.status = "cancelled"
        return task

    # Field updates
    if body.title is not None:
        task.title = body.title
    if body.instruction is not None:
        task.instruction = body.instruction
    if body.deliver is not None:
        task.deliver = body.deliver
    if body.requires_approval is not None:
        task.requires_approval = body.requires_approval
    if body.active_hours_start is not None:
        task.active_hours_start = body.active_hours_start
    if body.active_hours_end is not None:
        task.active_hours_end = body.active_hours_end
    if body.active_hours_tz is not None:
        task.active_hours_tz = body.active_hours_tz
    if body.schedule is not None:
        task.schedule = body.schedule
        task.next_run_at = _compute_next_run_at(body.schedule)

    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_owned_task(task_id, current_user.id, db)
    task.status = "cancelled"


@router.post("/tasks/{task_id}/run", response_model=Dict[str, Any])
async def manual_run(
    task_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enqueue the task for immediate execution (dev/testing)."""
    task = await _get_owned_task(task_id, current_user.id, db)
    if task.status == "running":
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

@router.get("/{task_id}/audit", response_model=TaskAuditOut)
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


# ── Internal helper ───────────────────────────────────────────────────────────

async def _get_owned_task(task_id: int, user_id: int, db: AsyncSession) -> Task:
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task
