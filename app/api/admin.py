"""
FruitcakeAI v5 — Admin router
All endpoints require admin role.

GET  /admin/health         — dependency health (DB, LLM, embedding, MCP)
GET  /admin/tools          — MCP tool registry status
GET  /admin/users          — list all users
POST /admin/users          — create a new user
PATCH /admin/users/{id}    — update user role / persona / scopes / active flag
GET  /admin/audit          — recent agent tool-call audit log
GET  /admin/task-runs      — Phase 4 debug: task run history with tool calls
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.auth.jwt import hash_password
from app.autonomy.push import get_apns_pusher
from app.config import settings
from app.db.models import AuditLog, DeviceToken, RSSSource, RSSSourceCandidate, Task, TaskRun, User
from app.db.session import get_db
from app.metrics import metrics

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: int
    username: str
    email: str
    full_name: Optional[str]
    role: str
    persona: str
    library_scopes: List[str]
    calendar_access: List[str]
    is_active: bool
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class CreateUserRequest(BaseModel):
    username: str
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    role: str = "parent"
    persona: str = "family_assistant"
    library_scopes: List[str] = ["family_docs"]
    calendar_access: List[str] = []


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    persona: Optional[str] = None
    library_scopes: Optional[List[str]] = None
    calendar_access: Optional[List[str]] = None
    is_active: Optional[bool] = None


class TestPushRequest(BaseModel):
    title: str = "Fruitcake Test Push"
    body: str = "This is a test notification from FruitcakeAI."


# ── GET /admin/metrics ────────────────────────────────────────────────────────

@router.get("/metrics")
async def get_metrics(
    current_user: User = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Return simple in-memory request counters.
    Resets on server restart — no persistence needed for a home server.
    """
    return metrics.snapshot()


# ── GET /admin/health ─────────────────────────────────────────────────────────

@router.get("/health")
async def admin_health(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Check all system dependencies and return their status."""
    db_status = await _check_database(db)
    llm_status = await _check_llm()
    rag_status = _check_rag()
    mcp_status = _check_mcp()
    llm_dispatch_gate = _check_llm_dispatch_gate()

    overall = "ok"
    if any(s.get("status") == "error" for s in [db_status, llm_status, rag_status]):
        overall = "degraded"

    return {
        "status": overall,
        "database": db_status,
        "llm": llm_status,
        "llm_dispatch_gate": llm_dispatch_gate,
        "embedding_model": rag_status,
        "mcp": mcp_status,
    }


async def _check_database(db: AsyncSession) -> Dict[str, str]:
    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def _check_llm() -> Dict[str, Any]:
    try:
        if settings.llm_backend in ("ollama", "openai_compat"):
            base = settings.local_api_base.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(base + "/api/tags")
                if resp.status_code >= 400:
                    return {"status": "error", "error": f"HTTP {resp.status_code}", "model": settings.llm_model, "url": base}
            return {"status": "ok", "model": settings.llm_model, "url": base}
        return {"status": "ok", "note": "cloud backend", "model": settings.llm_model}
    except Exception as e:
        return {"status": "error", "error": str(e), "model": settings.llm_model}


def _check_rag() -> Dict[str, Any]:
    from app.rag.service import get_rag_service
    return get_rag_service().health()


def _check_mcp() -> Dict[str, Any]:
    from app.mcp.registry import get_mcp_registry
    s = get_mcp_registry().get_status()
    return {"status": "ok" if s["ready"] else "not_ready", "tool_count": s["tool_count"]}


def _check_llm_dispatch_gate() -> Dict[str, Any]:
    from app.autonomy.scheduler import get_llm_dispatch_health

    state = get_llm_dispatch_health()
    return {
        "status": "blocked" if state.get("blocked") else "ready",
        "unhealthy_until": state.get("unhealthy_until"),
        "last_error": state.get("last_error"),
    }


# ── GET /admin/tools ──────────────────────────────────────────────────────────

@router.get("/tools")
async def list_tools(
    current_user: User = Depends(require_admin),
) -> Dict[str, Any]:
    """
    List all registered MCP tools and their status.
    Adding a new entry to config/mcp_config.yaml and restarting makes it
    appear here automatically — no code changes required.
    """
    from app.mcp.registry import get_mcp_registry
    return get_mcp_registry().get_status()


@router.get("/mcp/diagnostics")
async def mcp_diagnostics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Detailed MCP diagnostics: server-level connection state, last error,
    stderr tail (docker stdio), registered tools, and duplicate-name conflicts.
    """
    from app.mcp.registry import get_mcp_registry
    diagnostics = get_mcp_registry().get_diagnostics()

    pending_count = (
        await db.execute(
            select(func.count()).select_from(RSSSourceCandidate).where(RSSSourceCandidate.status == "pending")
        )
    ).scalar_one()
    approved_count = (
        await db.execute(
            select(func.count()).select_from(RSSSourceCandidate).where(RSSSourceCandidate.status == "approved")
        )
    ).scalar_one()
    source_count = (await db.execute(select(func.count()).select_from(RSSSource))).scalar_one()
    diagnostics["rss"] = {
        "source_count": int(source_count or 0),
        "candidate_counts": {
            "pending": int(pending_count or 0),
            "approved": int(approved_count or 0),
        },
    }
    return diagnostics


@router.post("/push/test")
async def send_test_push(
    body: TestPushRequest = TestPushRequest(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Send a test APNs notification to the current admin user's registered devices.
    Useful for validating push delivery without running a task.
    """
    result = await db.execute(
        select(DeviceToken).where(DeviceToken.user_id == current_user.id)
    )
    tokens = result.scalars().all()
    if not tokens:
        return {
            "ok": False,
            "attempted": 0,
            "delivered": 0,
            "message": "No registered device tokens for current user.",
        }

    pusher = get_apns_pusher()

    delivered = 0
    for device in tokens:
        ok = await pusher.send(
            device_token=device.token,
            environment=device.environment,
            title=body.title,
            body=body.body,
        )
        if ok:
            delivered += 1

    return {
        "ok": delivered > 0,
        "attempted": len(tokens),
        "delivered": delivered,
        "message": f"Delivered to {delivered}/{len(tokens)} device(s).",
    }


# ── GET /admin/users ──────────────────────────────────────────────────────────

@router.get("/users", response_model=List[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> List[User]:
    """Return all users sorted by creation date."""
    result = await db.execute(select(User).order_by(User.created_at))
    return result.scalars().all()


# ── POST /admin/users ─────────────────────────────────────────────────────────

@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> User:
    """Create a new user account (admin only)."""
    result = await db.execute(
        select(User).where(
            (User.username == body.username) | (User.email == body.email)
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already in use",
        )

    from app.agent.persona_loader import persona_exists, list_personas
    if not persona_exists(body.persona):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown persona '{body.persona}'. Available: {', '.join(list_personas())}",
        )

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
        persona=body.persona,
    )
    user.library_scopes = body.library_scopes
    user.calendar_access = body.calendar_access

    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


# ── PATCH /admin/users/{id} ───────────────────────────────────────────────────

@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> User:
    """Update a user's role, persona, scopes, or active status (admin only)."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.role is not None:
        user.role = body.role

    if body.persona is not None:
        from app.agent.persona_loader import persona_exists, list_personas
        if not persona_exists(body.persona):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown persona '{body.persona}'. Available: {', '.join(list_personas())}",
            )
        user.persona = body.persona

    if body.library_scopes is not None:
        user.library_scopes = body.library_scopes

    if body.calendar_access is not None:
        user.calendar_access = body.calendar_access

    if body.is_active is not None:
        user.is_active = body.is_active

    await db.flush()
    await db.refresh(user)
    return user


# ── GET /admin/audit ──────────────────────────────────────────────────────────

@router.get("/audit")
async def get_audit_log(
    limit: int = Query(50, ge=1, le=500),
    tool: Optional[str] = Query(None, description="Filter by tool name"),
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Return recent agent tool-call audit log entries, newest first."""
    query = (
        select(AuditLog, User.username)
        .join(User, AuditLog.user_id == User.id, isouter=True)
        .order_by(desc(AuditLog.created_at))
    )

    if tool:
        query = query.where(AuditLog.tool == tool)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)

    query = query.limit(limit)
    result = await db.execute(query)
    rows = result.all()

    entries = [
        {
            "id": log_entry.id,
            "user_id": log_entry.user_id,
            "username": username,
            "tool": log_entry.tool,
            "arguments": log_entry.arguments,
            "result_summary": log_entry.result_summary,
            "session_id": log_entry.session_id,
            "created_at": log_entry.created_at,
        }
        for log_entry, username in rows
    ]

    return {"count": len(entries), "entries": entries}


@router.get("/task-runs", tags=["admin"])
async def task_runs(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Debug endpoint — task run history with tool calls.

    Uses task_runs records so each execution attempt is tracked separately.
    """
    run_result = await db.execute(
        select(TaskRun, Task)
        .join(Task, TaskRun.task_id == Task.id)
        .order_by(desc(TaskRun.started_at))
        .limit(limit)
    )
    runs = run_result.all()

    output = []
    for run, task in runs:
        tool_calls: List[Dict[str, Any]] = []
        if run.session_id is not None:
            log_result = await db.execute(
                select(AuditLog)
                .where(AuditLog.session_id == run.session_id)
                .order_by(AuditLog.created_at)
            )
            tool_calls = [
                {
                    "tool": entry.tool,
                    "arguments": entry.arguments,
                    "result_summary": entry.result_summary,
                    "called_at": entry.created_at,
                }
                for entry in log_result.scalars().all()
            ]

        output.append(
            {
                "run_id": run.id,
                "id": task.id,
                "title": task.title,
                "instruction": task.instruction,
                "task_type": task.task_type,
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "session_id": run.session_id,
                "result": run.summary,
                "error": run.error,
                "retry_count": task.retry_count,
                "tool_calls": tool_calls,
            }
        )

    return {"count": len(output), "runs": output}
