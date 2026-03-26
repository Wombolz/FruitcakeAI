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

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.auth.jwt import hash_password
from app.autonomy.push import get_apns_pusher
from app.config import settings
from app.db.models import (
    AuditLog,
    DeviceToken,
    MemoryEntity,
    MemoryObservation,
    MemoryRelation,
    RSSSource,
    RSSSourceCandidate,
    Skill,
    Task,
    TaskRun,
    User,
)
from app.db.models import TaskRunArtifact
from app.db.session import get_db
from app.metrics import metrics
from app.memory.graph_service import get_graph_memory_service
from app.skills.service import (
    SkillConflictError,
    SkillNotFoundError,
    SkillValidationError,
    get_skill_service,
)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: int
    username: str
    email: str
    full_name: Optional[str]
    role: str
    persona: str
    chat_routing_preference: str
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
    chat_routing_preference: str = "auto"
    library_scopes: List[str] = ["family_docs"]
    calendar_access: List[str] = []


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    persona: Optional[str] = None
    chat_routing_preference: Optional[str] = None
    library_scopes: Optional[List[str]] = None
    calendar_access: Optional[List[str]] = None
    is_active: Optional[bool] = None


class TestPushRequest(BaseModel):
    title: str = "Fruitcake Test Push"
    body: str = "This is a test notification from FruitcakeAI."


class SkillPreviewRequest(BaseModel):
    content: Optional[str] = None
    source_url: Optional[str] = None
    personal_user_id: Optional[int] = None


class SkillPreviewResponse(BaseModel):
    slug: str
    name: str
    description: str
    system_prompt_addition: str
    allowed_tool_additions: List[str]
    scope: str
    personal_user_id: Optional[int]
    source_url: Optional[str]
    is_pinned: bool
    validation_warnings: List[str]
    preview_hash: str


class SkillInstallRequest(BaseModel):
    slug: str
    name: str
    description: str
    system_prompt_addition: str
    allowed_tool_additions: List[str] = []
    scope: str
    personal_user_id: Optional[int] = None
    source_url: Optional[str] = None
    is_pinned: bool = False
    preview_hash: str


class SkillUpdateRequest(BaseModel):
    is_active: Optional[bool] = None
    is_pinned: Optional[bool] = None
    scope: Optional[str] = None
    personal_user_id: Optional[int] = None


class SkillOut(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    allowed_tool_additions: List[str]
    scope: str
    personal_user_id: Optional[int]
    is_active: bool
    is_pinned: bool
    source_url: Optional[str]
    content_hash: str
    installed_at: Optional[datetime]
    installed_by: Optional[int]
    installed_by_username: Optional[str] = None
    supersedes_skill_id: Optional[int] = None


class SkillInjectionPreviewOut(BaseModel):
    skill_id: int
    slug: str
    name: str
    score: float
    included: bool
    reason: str
    estimated_tokens: int
    active: bool
    scope: str
    selection_mode: str


class MemoryGraphEntityAdminOut(BaseModel):
    id: int
    user_id: int
    name: str
    entity_type: str
    aliases: List[str]
    confidence: float
    is_active: bool
    relation_count: int
    observation_count: int


class MemoryGraphDiagnosticsOut(BaseModel):
    total_entities: int
    total_relations: int
    total_observations: int
    entities: List[MemoryGraphEntityAdminOut]


class MemoryGraphRelationAdminOut(BaseModel):
    id: int
    relation_type: str
    confidence: float
    from_entity_id: int
    from_entity_name: str
    to_entity_id: int
    to_entity_name: str
    source_memory_id: Optional[int]
    source_session_id: Optional[int]
    source_task_id: Optional[int]


class MemoryGraphObservationAdminOut(BaseModel):
    id: int
    content: Optional[str]
    observed_at: Optional[datetime]
    confidence: float
    is_active: bool
    source_memory_id: Optional[int]
    source_session_id: Optional[int]
    source_task_id: Optional[int]


class MemoryGraphEntityInspectOut(BaseModel):
    entity: MemoryGraphEntityAdminOut
    relations: List[MemoryGraphRelationAdminOut]
    observations: List[MemoryGraphObservationAdminOut]


_ARTIFACT_ORDER = {
    "prepared_dataset": 0,
    "draft_output": 1,
    "final_output": 2,
    "edition_export": 3,
    "validation_report": 4,
    "run_diagnostics": 5,
}


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
    skills_status = await _check_skills(db)

    overall = "ok"
    if any(s.get("status") == "error" for s in [db_status, llm_status, rag_status, mcp_status]):
        overall = "degraded"
    elif any(s.get("status") == "degraded" for s in [mcp_status]):
        overall = "degraded"

    return {
        "status": overall,
        "database": db_status,
        "llm": llm_status,
        "llm_dispatch_gate": llm_dispatch_gate,
        "embedding_model": rag_status,
        "mcp": mcp_status,
        "skills": skills_status,
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
    registry = get_mcp_registry()
    status = registry.get_status()
    diagnostics = registry.get_diagnostics()

    enabled_servers = [server for server in diagnostics.get("servers", []) if server.get("enabled")]
    required_unavailable = [
        server["server"]
        for server in enabled_servers
        if server.get("type") == "internal_python"
        and server.get("status") not in ("loaded", "connected")
    ]
    optional_unavailable = [
        server["server"]
        for server in enabled_servers
        if server.get("type") == "docker_stdio"
        and server.get("status") not in ("connected",)
    ]

    health = "ok"
    if not status.get("ready") or required_unavailable:
        health = "error"
    elif optional_unavailable:
        health = "degraded"

    return {
        "status": health,
        "ready": status["ready"],
        "tool_count": status["tool_count"],
        "enabled_server_count": len(enabled_servers),
        "required_unavailable_servers": required_unavailable,
        "optional_unavailable_servers": optional_unavailable,
    }


def _check_llm_dispatch_gate() -> Dict[str, Any]:
    from app.autonomy.scheduler import get_llm_dispatch_health

    state = get_llm_dispatch_health()
    return {
        "status": "blocked" if state.get("blocked") else "ready",
        "unhealthy_until": state.get("unhealthy_until"),
        "last_error": state.get("last_error"),
    }


async def _check_skills(db: AsyncSession) -> Dict[str, Any]:
    service = get_skill_service()
    total = (
        await db.execute(select(func.count()).select_from(Skill))
    ).scalar_one()
    active = (
        await db.execute(select(func.count()).select_from(Skill).where(Skill.is_active == True))
    ).scalar_one()
    return {
        "status": "ok",
        "total_count": int(total or 0),
        "active_count": int(active or 0),
        "selection_mode": service.relevance_mode(),
    }


async def _admin_load_graph_counts(
    db: AsyncSession,
    entity_ids: List[int],
) -> Dict[str, Dict[int, int]]:
    if not entity_ids:
        return {"relations": {}, "observations": {}}

    observation_counts_result = await db.execute(
        select(MemoryObservation.entity_id, func.count(MemoryObservation.id))
        .where(and_(MemoryObservation.entity_id.in_(entity_ids), MemoryObservation.is_active == True))
        .group_by(MemoryObservation.entity_id)
    )
    observation_counts = {
        int(entity_id): int(count) for entity_id, count in observation_counts_result.all()
    }

    relation_counts_result = await db.execute(
        select(MemoryRelation.from_entity_id, MemoryRelation.to_entity_id)
        .where(
            or_(
                MemoryRelation.from_entity_id.in_(entity_ids),
                MemoryRelation.to_entity_id.in_(entity_ids),
            )
        )
    )
    relation_counts = {entity_id: 0 for entity_id in entity_ids}
    for from_entity_id, to_entity_id in relation_counts_result.all():
        if from_entity_id in relation_counts:
            relation_counts[int(from_entity_id)] += 1
        if to_entity_id in relation_counts:
            relation_counts[int(to_entity_id)] += 1

    return {"relations": relation_counts, "observations": observation_counts}


async def _admin_load_entity_lookup(
    db: AsyncSession,
    entity_ids: set[int],
) -> Dict[int, MemoryEntity]:
    if not entity_ids:
        return {}
    result = await db.execute(select(MemoryEntity).where(MemoryEntity.id.in_(sorted(entity_ids))))
    return {entity.id: entity for entity in result.scalars().all()}


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


def _skill_to_out(skill: Skill, installer: Optional[User] = None) -> SkillOut:
    return SkillOut(
        id=skill.id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        allowed_tool_additions=skill.allowed_tool_additions,
        scope=skill.scope,
        personal_user_id=skill.personal_user_id,
        is_active=skill.is_active,
        is_pinned=skill.is_pinned,
        source_url=skill.source_url,
        content_hash=skill.content_hash,
        installed_at=skill.installed_at,
        installed_by=skill.installed_by,
        installed_by_username=getattr(installer, "username", None),
        supersedes_skill_id=skill.supersedes_skill_id,
    )


def _decode_artifact_json(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _artifact_sort_key(artifact: TaskRunArtifact) -> tuple[int, datetime, int]:
    created = artifact.created_at or datetime.min
    return (_ARTIFACT_ORDER.get(artifact.artifact_type, 99), created, artifact.id or 0)


def _serialize_artifact(artifact: TaskRunArtifact) -> Dict[str, Any]:
    return {
        "id": artifact.id,
        "artifact_type": artifact.artifact_type,
        "content_json": _decode_artifact_json(artifact.content_json),
        "content_text": artifact.content_text,
        "created_at": artifact.created_at,
    }


def _tool_call_payload(entry: AuditLog) -> Dict[str, Any]:
    return {
        "id": entry.id,
        "tool": entry.tool,
        "arguments": entry.arguments,
        "result_summary": entry.result_summary,
        "called_at": entry.created_at,
    }


def _normalized_diagnostics(artifacts: List[Dict[str, Any]]) -> Dict[str, Any]:
    artifact_map = {artifact["artifact_type"]: artifact for artifact in artifacts}
    run_diagnostics = artifact_map.get("run_diagnostics", {}).get("content_json") or {}
    validation_report = artifact_map.get("validation_report", {}).get("content_json") or {}
    edition_export = artifact_map.get("edition_export", {}).get("content_json") or {}

    if not isinstance(run_diagnostics, dict):
        run_diagnostics = {}
    if not isinstance(validation_report, dict):
        validation_report = {}
    if not isinstance(edition_export, dict):
        edition_export = {}

    return {
        "active_skills": run_diagnostics.get("active_skills", []),
        "skill_selection_mode": run_diagnostics.get("skill_selection_mode", ""),
        "skill_injection_events": run_diagnostics.get("skill_injection_events", []),
        "dataset_stats": run_diagnostics.get("dataset_stats", {}),
        "refresh_stats": run_diagnostics.get("refresh_stats", {}),
        "tool_failure_suppressions": run_diagnostics.get("suppression_events", []),
        "validation_report": validation_report,
        "edition_export": edition_export,
    }


def _duration_seconds(run: TaskRun) -> Optional[float]:
    if run.started_at is None or run.finished_at is None:
        return None
    return round((run.finished_at - run.started_at).total_seconds(), 3)


async def _load_task_run_bundle(
    db: AsyncSession,
    run_id: int,
) -> tuple[TaskRun, Task, List[AuditLog], List[TaskRunArtifact]]:
    run_result = await db.execute(
        select(TaskRun, Task)
        .join(Task, TaskRun.task_id == Task.id)
        .where(TaskRun.id == run_id)
    )
    row = run_result.first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task run not found")

    run, task = row
    logs: List[AuditLog] = []
    if run.session_id is not None:
        log_result = await db.execute(
            select(AuditLog)
            .where(AuditLog.session_id == run.session_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
        logs = list(log_result.scalars().all())

    artifact_result = await db.execute(
        select(TaskRunArtifact)
        .where(TaskRunArtifact.task_run_id == run.id)
        .order_by(TaskRunArtifact.created_at, TaskRunArtifact.id)
    )
    artifacts = list(artifact_result.scalars().all())
    return run, task, logs, artifacts


async def _build_task_run_inspect_payload(db: AsyncSession, run_id: int) -> Dict[str, Any]:
    run, task, logs, artifacts = await _load_task_run_bundle(db, run_id)
    ordered_artifacts = [_serialize_artifact(a) for a in sorted(artifacts, key=_artifact_sort_key)]
    tool_timeline = [_tool_call_payload(entry) for entry in logs]
    diagnostics = _normalized_diagnostics(ordered_artifacts)

    return {
        "run": {
            "id": run.id,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_seconds": _duration_seconds(run),
            "session_id": run.session_id,
            "error": run.error,
            "summary": run.summary,
        },
        "task": {
            "id": task.id,
            "title": task.title,
            "instruction": task.instruction,
            "task_type": task.task_type,
            "profile": task.profile or "default",
            "retry_count": task.retry_count,
        },
        "execution": {
            "active_skills": diagnostics.get("active_skills", []),
            "skill_selection_mode": diagnostics.get("skill_selection_mode", ""),
            "tool_failure_suppressions": diagnostics.get("tool_failure_suppressions", []),
            "refresh_stats": diagnostics.get("refresh_stats", {}),
            "dataset_stats": diagnostics.get("dataset_stats", {}),
            "edition_export": diagnostics.get("edition_export", {}),
        },
        "tool_timeline": tool_timeline,
        "artifacts": ordered_artifacts,
        "diagnostics": diagnostics,
    }


def _edition_pdf_path_from_artifacts(artifacts: List[TaskRunArtifact]) -> Optional[Path]:
    for artifact in artifacts:
        if artifact.artifact_type != "edition_export" or not artifact.content_json:
            continue
        try:
            payload = json.loads(artifact.content_json)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        relative = str(payload.get("pdf_relative_path") or "").strip()
        if not relative:
            continue
        path = Path(settings.storage_dir) / relative
        return path
    return None


@router.post("/skills/preview", response_model=SkillPreviewResponse)
async def preview_skill(
    body: SkillPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> SkillPreviewResponse:
    service = get_skill_service()
    try:
        preview = await service.preview_from_request(
            content=body.content,
            source_url=body.source_url,
            personal_user_id=body.personal_user_id,
        )
    except SkillValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return SkillPreviewResponse(
        slug=preview.slug,
        name=preview.name,
        description=preview.description,
        system_prompt_addition=preview.system_prompt_addition,
        allowed_tool_additions=preview.allowed_tool_additions,
        scope=preview.scope,
        personal_user_id=preview.personal_user_id,
        source_url=preview.source_url,
        is_pinned=preview.is_pinned,
        validation_warnings=preview.validation_warnings,
        preview_hash=preview.preview_hash,
    )


@router.post("/skills/install", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
async def install_skill(
    body: SkillInstallRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> SkillOut:
    service = get_skill_service()
    preview_payload = {
        "slug": body.slug,
        "name": body.name,
        "description": body.description,
        "system_prompt_addition": body.system_prompt_addition,
        "allowed_tool_additions": body.allowed_tool_additions,
        "scope": body.scope,
        "personal_user_id": body.personal_user_id,
        "source_url": body.source_url,
        "is_pinned": body.is_pinned,
    }
    try:
        preview = await service.preview_from_payload(preview_payload)
        if preview.preview_hash != body.preview_hash:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="preview_hash mismatch")
        skill = await service.install_preview(db, preview=preview, installed_by=current_user.id)
        await db.commit()
        await db.refresh(skill)
    except SkillValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except SkillConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return _skill_to_out(skill, current_user)


@router.get("/skills", response_model=List[SkillOut])
async def list_skills(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> List[SkillOut]:
    service = get_skill_service()
    skills = await service.list_skills(db)
    users = {u.id: u for u in (await db.execute(select(User))).scalars().all()}
    return [_skill_to_out(skill, users.get(skill.installed_by)) for skill in skills]


@router.patch("/skills/{skill_id}", response_model=SkillOut)
async def update_skill(
    skill_id: int,
    body: SkillUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> SkillOut:
    service = get_skill_service()
    try:
        skill = await service.update_skill(
            db,
            skill_id=skill_id,
            is_active=body.is_active,
            is_pinned=body.is_pinned,
            scope=body.scope,
            personal_user_id=body.personal_user_id,
        )
        await db.commit()
        await db.refresh(skill)
    except SkillValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return _skill_to_out(skill, current_user)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    service = get_skill_service()
    try:
        await service.delete_skill(db, skill_id=skill_id)
        await db.commit()
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/skills/{skill_id}/preview-injection", response_model=SkillInjectionPreviewOut)
async def preview_skill_injection(
    skill_id: int,
    query: str = Query("", description="Sample query to test skill relevance"),
    user_id: Optional[int] = Query(None, description="Optional user context for scope evaluation"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> SkillInjectionPreviewOut:
    target_user_id = user_id or current_user.id
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="skill not found")
    decisions = await get_skill_service().explain_injection(
        db,
        user_id=target_user_id,
        query=query,
        skill_id=skill_id,
    )
    if not decisions:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="skill not visible for that user")
    decision = decisions[0]
    return SkillInjectionPreviewOut(
        skill_id=decision.skill_id,
        slug=decision.slug,
        name=decision.name,
        score=decision.score,
        included=decision.included,
        reason=decision.reason,
        estimated_tokens=decision.estimated_tokens,
        active=skill.is_active,
        scope=skill.scope,
        selection_mode=decision.selection_mode,
    )


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


@router.get("/memory-graph/diagnostics", response_model=MemoryGraphDiagnosticsOut)
async def memory_graph_diagnostics(
    user_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> MemoryGraphDiagnosticsOut:
    entity_filters = []
    relation_filters = []
    observation_filters = []
    if user_id is not None:
        entity_filters.append(MemoryEntity.user_id == user_id)
        relation_filters.append(MemoryRelation.user_id == user_id)
        observation_filters.append(MemoryObservation.user_id == user_id)
    observation_filters.append(MemoryObservation.is_active == True)
    if q:
        entity_filters.append(func.lower(MemoryEntity.name).like(f"%{q.strip().lower()}%"))

    entities_stmt = (
        select(MemoryEntity)
        .order_by(desc(MemoryEntity.confidence), desc(MemoryEntity.created_at))
        .limit(limit)
    )
    if entity_filters:
        entities_stmt = entities_stmt.where(and_(*entity_filters))
    entities = list((await db.execute(entities_stmt)).scalars().all())

    total_entities_stmt = select(func.count()).select_from(MemoryEntity)
    total_relations_stmt = select(func.count()).select_from(MemoryRelation)
    total_observations_stmt = select(func.count()).select_from(MemoryObservation)
    if entity_filters:
        total_entities_stmt = total_entities_stmt.where(and_(*entity_filters))
    if relation_filters:
        total_relations_stmt = total_relations_stmt.where(and_(*relation_filters))
    if observation_filters:
        total_observations_stmt = total_observations_stmt.where(and_(*observation_filters))

    total_entities = int((await db.execute(total_entities_stmt)).scalar_one() or 0)
    total_relations = int((await db.execute(total_relations_stmt)).scalar_one() or 0)
    total_observations = int((await db.execute(total_observations_stmt)).scalar_one() or 0)
    counts = await _admin_load_graph_counts(db, [entity.id for entity in entities])

    return MemoryGraphDiagnosticsOut(
        total_entities=total_entities,
        total_relations=total_relations,
        total_observations=total_observations,
        entities=[
            MemoryGraphEntityAdminOut(
                id=entity.id,
                user_id=entity.user_id,
                name=entity.name,
                entity_type=entity.entity_type,
                aliases=entity.aliases_list,
                confidence=entity.confidence,
                is_active=entity.is_active,
                relation_count=counts["relations"].get(entity.id, 0),
                observation_count=counts["observations"].get(entity.id, 0),
            )
            for entity in entities
        ],
    )


@router.get("/memory-graph/entities/{entity_id}", response_model=MemoryGraphEntityInspectOut)
async def inspect_memory_graph_entity(
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> MemoryGraphEntityInspectOut:
    entity = await db.get(MemoryEntity, entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory graph entity not found")

    svc = get_graph_memory_service()
    try:
        graph = await svc.open_entity_graph(db=db, user_id=entity.user_id, entity_id=entity_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory graph entity not found")

    relation_entity_ids = {entity_id}
    for rel in graph["relations"]:
        relation_entity_ids.add(rel.from_entity_id)
        relation_entity_ids.add(rel.to_entity_id)
    entity_lookup = await _admin_load_entity_lookup(db, relation_entity_ids)
    counts = await _admin_load_graph_counts(db, [entity_id])

    return MemoryGraphEntityInspectOut(
        entity=MemoryGraphEntityAdminOut(
            id=entity.id,
            user_id=entity.user_id,
            name=entity.name,
            entity_type=entity.entity_type,
            aliases=entity.aliases_list,
            confidence=entity.confidence,
            is_active=entity.is_active,
            relation_count=counts["relations"].get(entity.id, 0),
            observation_count=counts["observations"].get(entity.id, 0),
        ),
        relations=[
            MemoryGraphRelationAdminOut(
                id=rel.id,
                relation_type=rel.relation_type,
                confidence=rel.confidence,
                from_entity_id=rel.from_entity_id,
                from_entity_name=entity_lookup[rel.from_entity_id].name,
                to_entity_id=rel.to_entity_id,
                to_entity_name=entity_lookup[rel.to_entity_id].name,
                source_memory_id=rel.source_memory_id,
                source_session_id=rel.source_session_id,
                source_task_id=rel.source_task_id,
            )
            for rel in graph["relations"]
        ],
        observations=[
            MemoryGraphObservationAdminOut(
                id=obs.id,
                content=obs.content,
                observed_at=obs.observed_at,
                confidence=obs.confidence,
                is_active=obs.is_active,
                source_memory_id=obs.source_memory_id,
                source_session_id=obs.source_session_id,
                source_task_id=obs.source_task_id,
            )
            for obs in graph["observations"]
        ],
    )


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
        chat_routing_preference=body.chat_routing_preference,
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

    if body.chat_routing_preference is not None:
        if body.chat_routing_preference not in {"auto", "fast", "deep"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="chat_routing_preference must be one of: auto, fast, deep",
            )
        user.chat_routing_preference = body.chat_routing_preference

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
        run_artifacts: List[Dict[str, Any]] = []
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
        artifact_result = await db.execute(
            select(TaskRunArtifact).where(TaskRunArtifact.task_run_id == run.id)
        )
        for artifact in artifact_result.scalars().all():
            run_artifacts.append(
                {
                    "artifact_type": artifact.artifact_type,
                    "content_json": artifact.content_json,
                    "content_text": artifact.content_text,
                }
            )

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
                "artifacts": run_artifacts,
            }
        )

    return {"count": len(output), "runs": output}


@router.get("/task-runs/{run_id}/inspect", tags=["admin"])
async def inspect_task_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    return await _build_task_run_inspect_payload(db, run_id)


@router.get("/task-runs/{run_id}/edition.pdf", tags=["admin"])
async def download_task_run_edition_pdf(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    run, task, _logs, artifacts = await _load_task_run_bundle(db, run_id)
    pdf_path = _edition_pdf_path_from_artifacts(artifacts)
    if pdf_path is None or not pdf_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Edition PDF not found")
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"task-{task.id}-run-{run.id}-edition.pdf",
    )
