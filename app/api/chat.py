"""
FruitcakeAI v5 — Chat API
POST /chat/sessions               — create session
GET  /chat/sessions               — list sessions
GET  /chat/sessions/{id}          — session + history
POST /chat/sessions/{id}/messages — send message (REST, non-streaming)
WS   /chat/sessions/{id}/ws       — send message (WebSocket, streaming)
GET  /chat/personas               — list available personas
"""

from __future__ import annotations

import json
import asyncio
import contextlib
import re
import time
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select

import structlog

from app.agent.context import UserContext
from app.agent.chat_intents import (
    is_library_detail_or_excerpt_intent,
    is_library_lookup_intent,
    is_library_summary_intent,
)
from app.agent.chat_orchestration import build_orchestrated_chat_history
from app.agent.chat_routing import ChatComplexityDecision, classify_chat_complexity
from app.agent.chat_validation import (
    build_chat_retry_instruction,
    should_validate_chat_response,
    validate_chat_response,
)
from app.agent.compaction import (
    COMPACTION_MARKER_KIND,
    boundary_message as _shared_boundary_message,
    build_boundary_payload,
    condense_carried_recap as _condense_carried_recap,
    estimate_message_tokens as _shared_estimate_message_tokens,
    recap_lines_from_marker as _marker_recap_lines,
    recap_summaries,
    render_boundary_text,
)
from app.agent.core import (
    build_local_document_summary_digest,
    get_agent_runtime_history,
    get_task_handoff_payload,
    reset_agent_runtime_history,
    reset_task_handoff_payload,
    restore_task_handoff_payload,
    restore_agent_runtime_history,
    run_agent,
    stream_agent,
)
from app.agent.runtime import AgentEvent, AgentEventEmitter, AgentEventType
from app.autonomy.approval import ApprovalRequired, _approval_armed
from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import ChatMessage, ChatRun, ChatSession, Task, User
from app.db.session import get_db
from app.chat_runtime import (
    apply_chat_run_event,
    create_chat_run,
    get_chat_run_manager,
    latest_chat_run,
    mark_chat_run_terminal,
    owned_chat_run,
    serialize_chat_approval,
    serialize_chat_run,
)
from app.llm_registry import available_llm_models, is_configured_model
from app.llm_usage import bind_llm_usage_context, reset_llm_usage_context
from app.metrics import metrics
from app.mcp.servers.filesystem import resolve_workspace_path_for_user
from app.memory.service import get_memory_service
from app.skills.service import hydrate_user_context
from app.agent.tools import (
    normalize_document_name_query,
    resolve_document_name,
    get_tool_execution_records,
    reset_tool_execution_records,
    restore_tool_execution_records,
    replay_waiting_approval_tool,
)
from app.task_service import TaskValidationError, create_task_record

log = structlog.get_logger(__name__)

router = APIRouter()
_CHAT_COMPACTION_MARKER_KIND = COMPACTION_MARKER_KIND
_ASSISTANT_MESSAGE_METADATA_KIND = "assistant_message_metadata"
_CHAT_LIVE_STATE_VALUES = {
    "thinking",
    "tool_active",
    "tool_completed",
    "image_rendering",
    "waiting_approval",
    "validating",
    "retrying",
    "completed",
}
_WORKSPACE_FILE_TOOL_NAMES = {"read_file", "write_file", "append_file", "stat_file"}
_WORKSPACE_CONTEXT_TOOL_NAMES = _WORKSPACE_FILE_TOOL_NAMES | {"find_files", "list_directory", "make_directory"}
_LIBRARY_EVIDENCE_TOOL_NAMES = {"search_library", "summarize_document", "list_library_documents"}
_RSS_EVIDENCE_TOOL_NAMES = {
    "get_feed_items",
    "search_feeds",
    "list_recent_feed_items",
    "search_my_feeds",
    "search_my_feeds_timeline",
}
_WEB_EVIDENCE_TOOL_NAMES = {
    "web_search",
    "fetch_page",
    "api_request",
    "get_daily_market_data",
    "get_intraday_market_data",
    "search_places",
}
_IMAGE_EVIDENCE_TOOL_NAMES = {"generate_image", "describe_image"}
_WORKSPACE_EXPLICIT_HINTS = (
    "workspace",
    "working on",
    "just created",
    "you were just working on",
    "you were working on",
    "that file",
    "this file",
    "the file",
    "that doc",
    "this doc",
    "the doc",
    "that document",
    "this document",
    "the document",
    "again",
)
_WORKSPACE_ACTION_HINTS = {
    "read",
    "open",
    "append",
    "update",
    "edit",
    "write",
    "continue",
    "add",
}


@dataclass
class RecentWorkspaceArtifactContext:
    active_path: str | None
    recent_read_path: str | None
    recent_write_path: str | None
    recent_paths: list[str]
    recent_actions: list[str]
    recent_document_names: list[str]

    @property
    def has_context(self) -> bool:
        return bool(self.active_path or self.recent_paths)


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    title: Optional[str] = None
    is_incognito: bool = False


class SendMessageRequest(BaseModel):
    content: str
    client_send_id: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    blocked_tools: Optional[List[str]] = None
    approval_mode: bool = False


class StopChatResponse(BaseModel):
    stopped: bool
    session_id: int


class ChatSessionStatusResponse(BaseModel):
    session_id: int
    active: bool
    run_id: Optional[str] = None
    status: Optional[str] = None
    phase: Optional[str] = None
    mode: Optional[str] = None
    stage: Optional[str] = None
    model: Optional[str] = None
    last_event_sequence: int = 0
    waiting_approval: Optional[Dict[str, Any]] = None
    error_classification: Optional[str] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    user_message_id: Optional[int] = None
    assistant_message_id: Optional[int] = None


class ChatApprovalDecision(BaseModel):
    approved: bool


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class UpdateSessionPersonaRequest(BaseModel):
    persona: str = Field(min_length=1, max_length=100)


class UpdateSessionModelRequest(BaseModel):
    llm_model: str = Field(min_length=1, max_length=200)


class ReorderSessionsRequest(BaseModel):
    session_ids: List[int]


class MessageOut(BaseModel):
    id: int
    role: str
    content: str

    class Config:
        from_attributes = True


class AcceptTaskDraftResponse(BaseModel):
    created: bool
    reused_existing: bool = False
    task_id: int
    title: str
    metadata: Dict[str, Any]


class DenyTaskDraftResponse(BaseModel):
    denied: bool
    metadata: Dict[str, Any]


class AcceptTaskDraftRequest(BaseModel):
    existing_task_id: Optional[int] = None


class SessionOut(BaseModel):
    id: int
    title: Optional[str]
    persona: str
    llm_model: Optional[str]
    sort_order: Optional[int]
    is_incognito: bool = False

    class Config:
        from_attributes = True


@dataclass
class ChatSocketPayload:
    raw: Dict[str, Any]
    content: str
    client_send_id: Optional[str]
    allowed_tools: Optional[List[str]]
    blocked_tools: Optional[List[str]]
    approval_mode: bool = False


# ── GET /chat/personas ────────────────────────────────────────────────────────

@router.get("/personas")
async def list_personas() -> Dict[str, Any]:
    """Return all available personas and their descriptions."""
    from app.agent.persona_loader import list_personas as _list
    personas = _list()
    return {
        name: {
            "display_name": cfg.get("display_name", ""),
            "description": cfg.get("description", ""),
            "tone": cfg.get("tone", ""),
            "blocked_tools": cfg.get("blocked_tools", []),
            "content_filter": cfg.get("content_filter", ""),
        }
        for name, cfg in personas.items()
    }


@router.get("/agents")
async def list_agents() -> Dict[str, Any]:
    """Return all available Fruitcake agent presets grouped by category."""
    from app.agent.definition_loader import list_agent_categories, list_agent_presets

    categories = list_agent_categories()
    presets = list_agent_presets()

    grouped: list[dict[str, Any]] = []
    for category_id, category in categories.items():
        category_presets = [
            {
                "id": preset.preset_id,
                "display_name": preset.display_name,
                "category": preset.category_id,
                "category_display_name": preset.category_display_name,
                "when_to_use": preset.when_to_use,
                "execution_mode": preset.execution_mode,
                "background": preset.background,
                "memory_scope": preset.memory_scope,
                "persona_compatibility": preset.persona_compatibility or "",
                "required_context_sources": list(preset.required_context_sources),
                "output_contract": list(preset.output_contract),
            }
            for preset in presets.values()
            if preset.category_id == category_id and not preset.hidden_from_picker
        ]
        if not category_presets:
            continue
        grouped.append(
            {
                "id": category.category_id,
                "display_name": category.display_name,
                "when_to_use": category.when_to_use,
                "presets": category_presets,
            }
        )

    return {"categories": grouped}


# ── GET /chat/tools ───────────────────────────────────────────────────────────

@router.get("/tools")
async def list_tools(
    persona: Optional[str] = Query(None, description="Optional persona override"),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return available tool names for the current user/persona."""
    from app.agent.tools import get_tools_for_user
    user_context = UserContext.from_user(current_user, persona_name=persona)
    tools = get_tools_for_user(user_context)
    names = sorted({tool["function"]["name"] for tool in tools})
    return {
        "persona": user_context.persona,
        "tools": names,
        "blocked_tools": sorted(set(user_context.blocked_tools)),
    }


@router.get("/models")
async def list_models(
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    del current_user
    return {"models": available_llm_models()}


# ── POST /chat/sessions ───────────────────────────────────────────────────────

@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest = CreateSessionRequest(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSession:
    if body.is_incognito and current_user.role not in settings.admin_roles:
        raise HTTPException(
            status_code=403,
            detail="Incognito sessions are limited to admin users.",
        )
    session = ChatSession(
        user_id=current_user.id,
        title=body.title or ("Incognito session" if body.is_incognito else "New conversation"),
        persona=current_user.persona or "family_assistant",
        llm_model=settings.llm_model,
        sort_order=0,
        is_incognito=body.is_incognito,
    )
    db.add(session)
    await db.flush()
    await db.execute(
        sa.update(ChatSession)
        .where(
            ChatSession.user_id == current_user.id,
            ChatSession.is_active == True,
            ChatSession.is_task_session == False,
            ChatSession.id != session.id,
        )
        .values(sort_order=func.coalesce(ChatSession.sort_order, 0) + 1)
    )
    await db.refresh(session)
    return session


# ── GET /chat/sessions ────────────────────────────────────────────────────────

@router.get("/sessions", response_model=List[SessionOut])
async def list_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[ChatSession]:
    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.user_id == current_user.id,
            ChatSession.is_active == True,
            ChatSession.is_task_session == False,
        )
        .order_by(
            ChatSession.sort_order.asc().nullslast(),
            ChatSession.id.desc(),
        )
    )
    return result.scalars().all()


# ── GET /chat/sessions/{id} ───────────────────────────────────────────────────

@router.get("/sessions/{session_id:int}")
async def get_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    session = await _get_session_or_404(session_id, current_user.id, db)

    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    messages = result.scalars().all()
    compaction_events = [
        event
        for event in (_serialize_chat_compaction_event(message) for message in messages)
        if event is not None
    ]

    return {
        "id": session.id,
        "title": session.title,
        "persona": session.persona,
        "llm_model": session.llm_model,
        "compaction_events": compaction_events,
        "messages": [
            _session_message_payload(m)
            for m in messages
            if not _is_hidden_runtime_message(m)
        ],
    }


@router.post("/messages/{message_id:int}/task-draft/accept", response_model=AcceptTaskDraftResponse)
async def accept_task_draft(
    message_id: int,
    body: Optional[AcceptTaskDraftRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AcceptTaskDraftResponse:
    message = await _get_chat_message_or_404(message_id, current_user.id, db)
    await _reject_if_incognito_session(message.session_id, db)
    metadata = _assistant_message_metadata(message)
    if not metadata:
        raise HTTPException(status_code=404, detail="Task draft message not found")

    draft = metadata.get("task_draft")
    if not isinstance(draft, dict) or not draft:
        raise HTTPException(status_code=409, detail="Message does not contain a task draft")

    normalized_metadata = _normalize_assistant_metadata_payload(metadata)
    current_status = str(normalized_metadata.get("task_draft_status") or "").strip().lower()
    if current_status == "denied":
        raise HTTPException(status_code=409, detail="Task draft has already been denied")

    created_task_id = metadata.get("created_task_id")
    if created_task_id is not None:
        existing = await db.get(Task, int(created_task_id))
        if existing is None or int(existing.user_id) != int(current_user.id):
            raise HTTPException(status_code=409, detail="Stored created task reference is no longer valid")
        message.tool_results = _encode_assistant_message_metadata(normalized_metadata)
        await db.commit()
        return AcceptTaskDraftResponse(
            created=True,
            reused_existing=True,
            task_id=int(existing.id),
            title=str(existing.title or ""),
            metadata=normalized_metadata,
        )

    if body is not None and body.existing_task_id is not None:
        existing = await db.get(Task, int(body.existing_task_id))
        if existing is None or int(existing.user_id) != int(current_user.id):
            raise HTTPException(status_code=404, detail="Existing task not found")
        metadata["task_draft_status"] = "accepted"
        metadata["created_task_id"] = int(existing.id)
        normalized_metadata = _normalize_assistant_metadata_payload(metadata)
        message.tool_results = _encode_assistant_message_metadata(normalized_metadata)
        await db.commit()
        return AcceptTaskDraftResponse(
            created=True,
            reused_existing=True,
            task_id=int(existing.id),
            title=str(existing.title or ""),
            metadata=normalized_metadata,
        )

    task_recipe = draft.get("task_recipe")
    recipe_family = None
    recipe_params = None
    if isinstance(task_recipe, dict):
        recipe_family = str(task_recipe.get("family") or "").strip() or None
        params = task_recipe.get("params")
        if isinstance(params, dict):
            recipe_params = params

    try:
        task = await create_task_record(
            db,
            user_id=current_user.id,
            title=str(draft.get("title") or ""),
            instruction=str(draft.get("instruction") or ""),
            persona=str(draft.get("persona") or "").strip() or None,
            profile=str(draft.get("profile") or "").strip() or None,
            llm_model_override=str(draft.get("llm_model_override") or "").strip() or None,
            task_type=str(draft.get("task_type") or "one_shot").strip() or "one_shot",
            schedule=str(draft.get("schedule") or "").strip() or None,
            deliver=bool(draft.get("deliver", True)),
            requires_approval=bool(draft.get("requires_approval", True)),
            active_hours_start=str(draft.get("active_hours_start") or "").strip() or None,
            active_hours_end=str(draft.get("active_hours_end") or "").strip() or None,
            active_hours_tz=str(draft.get("active_hours_tz") or "").strip() or None,
            user_timezone=getattr(current_user, "active_hours_tz", None),
            recipe_family=recipe_family,
            recipe_params=recipe_params,
        )
    except TaskValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    metadata["task_draft_status"] = "accepted"
    metadata["created_task_id"] = int(task.id)
    normalized_metadata = _normalize_assistant_metadata_payload(metadata)
    message.tool_results = _encode_assistant_message_metadata(normalized_metadata)
    await db.commit()

    return AcceptTaskDraftResponse(
        created=True,
        reused_existing=False,
        task_id=int(task.id),
        title=str(task.title or ""),
        metadata=normalized_metadata,
    )


@router.post("/messages/{message_id:int}/task-draft/deny", response_model=DenyTaskDraftResponse)
async def deny_task_draft(
    message_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DenyTaskDraftResponse:
    message = await _get_chat_message_or_404(message_id, current_user.id, db)
    await _reject_if_incognito_session(message.session_id, db)
    metadata = _assistant_message_metadata(message)
    if not metadata:
        raise HTTPException(status_code=404, detail="Task draft message not found")

    draft = metadata.get("task_draft")
    if not isinstance(draft, dict) or not draft:
        raise HTTPException(status_code=409, detail="Message does not contain a task draft")

    normalized_metadata = _normalize_assistant_metadata_payload(metadata)
    current_status = str(normalized_metadata.get("task_draft_status") or "").strip().lower()
    if current_status == "accepted":
        raise HTTPException(status_code=409, detail="Task draft has already been accepted")

    metadata.pop("created_task_id", None)
    metadata["task_draft_status"] = "denied"
    normalized_metadata = _normalize_assistant_metadata_payload(metadata)
    message.tool_results = _encode_assistant_message_metadata(normalized_metadata)
    await db.commit()
    return DenyTaskDraftResponse(denied=True, metadata=normalized_metadata)


# ── PATCH /chat/sessions/{id} ────────────────────────────────────────────────

@router.patch("/sessions/{session_id:int}", response_model=SessionOut)
async def rename_session(
    session_id: int,
    body: RenameSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSession:
    """Rename a chat session (owner only)."""
    session = await _get_session_or_404(session_id, current_user.id, db)
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="title must not be blank",
        )
    session.title = new_title
    await db.flush()
    await db.refresh(session)
    return session


# ── PATCH /chat/sessions/{id}/persona ────────────────────────────────────────

@router.patch("/sessions/{session_id:int}/persona", response_model=SessionOut)
async def update_session_persona(
    session_id: int,
    body: UpdateSessionPersonaRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSession:
    """Update the active persona for a session (owner only)."""
    from app.agent.persona_loader import persona_exists

    persona_name = body.persona.strip().lower().replace(" ", "_")
    if not persona_exists(persona_name):
        raise HTTPException(status_code=400, detail=f"Unknown persona '{persona_name}'")

    session = await _get_session_or_404(session_id, current_user.id, db)
    session.persona = persona_name
    await db.flush()
    await db.refresh(session)
    return session


@router.patch("/sessions/{session_id:int}/model", response_model=SessionOut)
async def update_session_model(
    session_id: int,
    body: UpdateSessionModelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSession:
    requested = body.llm_model.strip()
    if not is_configured_model(requested):
        raise HTTPException(status_code=400, detail=f"Unknown or unavailable model '{requested}'")

    session = await _get_session_or_404(session_id, current_user.id, db)
    session.llm_model = requested
    await db.flush()
    await db.refresh(session)
    return session


@router.patch("/sessions/order", response_model=List[SessionOut])
async def reorder_sessions(
    body: ReorderSessionsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[ChatSession]:
    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.user_id == current_user.id,
            ChatSession.is_active == True,
            ChatSession.is_task_session == False,
        )
        .order_by(ChatSession.sort_order.asc().nullslast(), ChatSession.id.asc())
    )
    ordered_sessions = result.scalars().all()

    current_ids = [session.id for session in ordered_sessions]
    requested_ids = body.session_ids
    if sorted(current_ids) != sorted(requested_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Session IDs must match the current user's sessions exactly",
        )

    session_by_id = {session.id: session for session in ordered_sessions}
    for idx, session_id in enumerate(requested_ids):
        session_by_id[session_id].sort_order = idx

    await db.flush()

    refreshed = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.user_id == current_user.id,
            ChatSession.is_active == True,
            ChatSession.is_task_session == False,
        )
        .order_by(
            ChatSession.sort_order.asc().nullslast(),
            ChatSession.id.desc(),
        )
    )
    return refreshed.scalars().all()


# ── DELETE /chat/sessions/{id} ───────────────────────────────────────────────

@router.delete("/sessions/{session_id:int}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Delete a chat session and all its messages (owner only)."""
    session = await _get_session_or_404(session_id, current_user.id, db)
    await db.delete(session)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── POST /chat/sessions/{id}/messages (REST, non-streaming) ──────────────────

@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: int,
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Send a message and get the full response (no streaming)."""
    request_started = time.perf_counter()
    stage_timings_ms: Dict[str, float] = {}
    session = await _get_session_or_404(session_id, current_user.id, db)
    request_fingerprint = str(abs(hash(" ".join(str(body.content or "").split()))))[:12]
    prompt_claimed = False

    # Handle /persona command before touching history or running the agent
    persona_name = _parse_persona_command(body.content)
    if persona_name is not None:
        return await _switch_persona(session_id, persona_name, session, db)

    claimed, duplicate_active, claimed_fingerprint = await get_chat_run_manager().claim_prompt(
        session_id,
        body.content,
    )
    if not claimed:
        log.info(
            "chat.rest_duplicate_prompt_rejected",
            session_id=session_id,
            user_id=current_user.id,
            client_send_id=body.client_send_id or "",
            prompt_fingerprint=claimed_fingerprint or request_fingerprint,
            active_run=duplicate_active,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A matching chat request is already running."
                if duplicate_active
                else "That message was already sent a moment ago."
            ),
        )
    prompt_claimed = True
    request_fingerprint = claimed_fingerprint or request_fingerprint

    # Load conversation history
    stage_started = time.perf_counter()
    history = await _load_history(session_id, db)
    _record_chat_stage_timing(stage_timings_ms, "history_load", stage_started)
    workspace_context = await _load_recent_workspace_context(
        session_id,
        user_id=current_user.id,
        db=db,
    )

    # Store user message
    user_msg = ChatMessage(session_id=session_id, role="user", content=body.content)
    db.add(user_msg)
    await db.flush()

    history.append({"role": "user", "content": body.content})

    # Build context from the session's current persona (may differ from user default)
    user_context = UserContext.from_user(current_user, persona_name=session.persona)
    _apply_tool_overrides(
        user_context,
        allowed_tools=body.allowed_tools,
        blocked_tools=body.blocked_tools,
    )
    stage_started = time.perf_counter()
    user_context = await hydrate_user_context(db, user_context, query=body.content)
    _record_chat_stage_timing(stage_timings_ms, "context_hydration", stage_started)
    user_context.session_id = session_id
    user_context.is_incognito = bool(session.is_incognito)
    stage_started = time.perf_counter()
    history, _memory_ids = await _apply_memory_context(
        history,
        db,
        current_user.id,
        body.content,
    )
    _record_chat_stage_timing(stage_timings_ms, "memory_context", stage_started)
    workspace_followup = _is_recent_workspace_followup_prompt(body.content, workspace_context)
    history = _apply_workspace_followup_grounding(
        history,
        user_prompt=body.content,
        context=workspace_context,
    )
    library_list_intent = (not workspace_followup) and is_library_lookup_intent(body.content)
    library_summary_intent = (not workspace_followup) and is_library_summary_intent(body.content)
    library_detail_intent = (not workspace_followup) and is_library_detail_or_excerpt_intent(body.content)
    library_intent = library_list_intent or library_summary_intent or library_detail_intent
    stage_started = time.perf_counter()
    history = await _apply_required_library_grounding(
        history,
        user_context,
        user_prompt=body.content,
        intent_type=(
            "summary"
            if library_summary_intent
            else (
                "detail_or_excerpt"
                if library_detail_intent
                else ("list_documents" if library_list_intent else None)
            )
        ),
        selected_model=session.llm_model,
    )
    _record_chat_stage_timing(stage_timings_ms, "library_grounding", stage_started)

    decision = classify_chat_complexity(
        body.content,
        threshold=settings.chat_complexity_threshold,
        routing_enabled=settings.chat_complexity_routing_enabled,
    )
    execution_mode, effective_complex = _resolve_chat_execution(
        auto_complex=(decision.is_complex or library_intent),
        preference=getattr(current_user, "chat_routing_preference", None),
    )
    _log_chat_routing_decision(
        decision=decision,
        preference=getattr(current_user, "chat_routing_preference", None),
        execution_mode=execution_mode,
        effective_complex=effective_complex,
        library_intent=library_intent,
        session_id=session_id,
        transport="rest",
    )
    should_validate = should_validate_chat_response(
        user_prompt=body.content,
        effective_complex=effective_complex,
    ) or workspace_followup
    stage_started = time.perf_counter()
    execution_history = build_orchestrated_chat_history(
        history,
        enabled=(execution_mode == "chat_orchestrated" and settings.chat_orchestration_enabled),
        max_steps=settings.chat_orchestration_max_steps,
    )
    _record_chat_stage_timing(stage_timings_ms, "orchestration_build", stage_started)
    if decision.is_complex:
        metrics.inc_chat_complexity_complex_count()
        if effective_complex:
            metrics.inc_chat_complexity_routed_complex_count()
    else:
        metrics.inc_chat_complexity_simple_count()

    usage_token = None
    record_token = None
    handoff_token = None
    runtime_history_token = None
    runtime_history_messages: List[Dict[str, Any]] = []
    handoff_metadata: Dict[str, Any] = {}
    assistant_metadata: Dict[str, Any] | None = None
    pending_tool_calls: List[Dict[str, Any]] = []
    chat_run_manager = get_chat_run_manager()
    current_task = asyncio.current_task()
    run_stage = "chat_complex" if effective_complex else "chat_simple"
    chat_run = await create_chat_run(
        db,
        session_id=session_id,
        user_id=current_user.id,
        user_message_id=int(user_msg.id),
        client_send_id=body.client_send_id,
        model=session.llm_model,
        mode=execution_mode,
        stage=run_stage,
    )
    await db.commit()

    async def _record_run_event(event: AgentEvent) -> None:
        await apply_chat_run_event(db, chat_run, event)
        if event.type != AgentEventType.TEXT_DELTA:
            await db.commit()

    async def _capture_pre_tool(tool_calls: List[Dict[str, Any]]) -> None:
        pending_tool_calls.clear()
        pending_tool_calls.extend(tool_calls)

    event_emitter = AgentEventEmitter(
        run_id=chat_run.id,
        session_id=session_id,
        callback=_record_run_event,
    )
    approval_token = None
    try:
        if current_task is not None:
            await chat_run_manager.register(session_id, current_task, run_id=chat_run.id)
        approval_token = _approval_armed.set(body.approval_mode and not bool(session.is_incognito))
        record_token = reset_tool_execution_records()
        handoff_token = reset_task_handoff_payload()
        runtime_history_token = reset_agent_runtime_history()
        usage_token = bind_llm_usage_context(
            user_id=current_user.id,
            session_id=session_id,
            source="chat_rest",
        )

        _flush_runtime_messages, _flush_pending_runtime_history, _get_consumed_runtime_message_count = (
            _build_runtime_history_flush_helpers(
                session_id=session_id,
                db=db,
                get_runtime_history=get_agent_runtime_history,
            )
        )

        stage_started = time.perf_counter()
        try:
            reply = await _execute_chat_turn(
                execution_history,
                user_context,
                user_prompt=body.content,
                mode=execution_mode,
                model_override=session.llm_model,
                stage=run_stage,
                enable_validation=should_validate,
                runtime_message_callback=_flush_runtime_messages,
                pre_tool_callback=_capture_pre_tool,
                event_emitter=event_emitter,
            )
        except ApprovalRequired as exc:
            pending_message = await _persist_chat_approval_wait(
                db=db,
                run=chat_run,
                exc=exc,
                pending_tool_calls=pending_tool_calls,
            )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=_chat_approval_response(chat_run, pending_message),
            )
        except Exception as e:
            runtime_history_messages = await _flush_pending_runtime_history()
            handoff_metadata = get_task_handoff_payload() or {}
            if _is_local_chat_model(session.llm_model) and _runtime_history_has_completed_tool_turn(runtime_history_messages):
                reply = _build_local_post_tool_synthesis_recovery_message(runtime_history_messages)
                log.warning(
                    "chat.local_post_tool_synthesis_recovered",
                    session_id=session_id,
                    user_id=current_user.id,
                    model=session.llm_model,
                    mode=execution_mode,
                    stage="chat_complex" if effective_complex else "chat_simple",
                    tool_names=_runtime_history_tool_names(runtime_history_messages),
                    runtime_history_message_count=len(runtime_history_messages),
                    persisted_runtime_messages=_get_consumed_runtime_message_count(),
                    error=str(e),
                    failure_phase="post_tool_synthesis",
                )
            else:
                log.exception("Agent error in REST handler", session_id=session_id)
                raise HTTPException(status_code=500, detail="Agent error — check server logs for details")
        _record_chat_stage_timing(stage_timings_ms, "model_execution", stage_started)
        reply = _enforce_calendar_mutation_integrity(
            body.content,
            reply,
            get_tool_execution_records(),
        )
        reply = _ensure_generated_image_markdown_references(reply, get_tool_execution_records())
        runtime_history_messages = await _flush_pending_runtime_history()
        handoff_metadata = get_task_handoff_payload() or {}
        assistant_metadata = _build_assistant_message_metadata(
            handoff_metadata=handoff_metadata,
            executed_tools=get_tool_execution_records(),
            recalled_memory_ids=_memory_ids,
        )

        assistant_msg = ChatMessage(
            session_id=session_id,
            role="assistant",
            content=reply,
            tool_results=_encode_assistant_message_metadata(assistant_metadata) if assistant_metadata else None,
        )
        db.add(assistant_msg)
        await _mark_recalled_memories_materialized(db, _memory_ids)
        await db.flush()
        await mark_chat_run_terminal(
            db,
            chat_run,
            status="completed",
            phase="completed",
            assistant_message_id=int(assistant_msg.id),
        )
        await db.commit()
        _log_chat_latency_breakdown(
            session_id=session_id,
            mode=execution_mode,
            total_started=request_started,
            stage_timings_ms=stage_timings_ms,
            transport="rest",
        )

        return {
            "role": "assistant",
            "content": reply,
            "message_id": int(assistant_msg.id),
            "session_id": session_id,
            "run_id": chat_run.id,
            "metadata": {
                "active_skills": list(user_context.active_skill_slugs or []),
                "skill_selection_mode": user_context.skill_selection_mode or "",
                **(assistant_metadata or {}),
                **handoff_metadata,
            },
        }
    except asyncio.CancelledError:
        await db.rollback()
        persisted_run = await db.get(ChatRun, chat_run.id)
        if persisted_run is not None:
            await mark_chat_run_terminal(
                db,
                persisted_run,
                status="cancelled",
                phase="cancelled",
                error_classification="user_cancelled",
            )
            await db.commit()
        log.info("Chat REST run stopped", session_id=session_id, user_id=current_user.id)
        return JSONResponse(status_code=409, content={"detail": "Chat stopped by user"})
    except Exception as exc:
        await db.rollback()
        persisted_run = await db.get(ChatRun, chat_run.id)
        if persisted_run is not None and persisted_run.status == "running":
            await mark_chat_run_terminal(
                db,
                persisted_run,
                status="failed",
                phase="failed",
                error_classification=type(exc).__name__,
            )
            await db.commit()
        raise
    finally:
        if prompt_claimed:
            await chat_run_manager.mark_prompt_finished(session_id, body.content)
        if current_task is not None:
            await chat_run_manager.clear(session_id, current_task)
        if approval_token is not None:
            _approval_armed.reset(approval_token)
        try:
            if record_token is not None:
                restore_tool_execution_records(record_token)
        except Exception:
            pass
        if usage_token is not None:
            try:
                reset_llm_usage_context(usage_token)
            except Exception:
                pass
        if handoff_token is not None:
            try:
                restore_task_handoff_payload(handoff_token)
            except Exception:
                pass
        if runtime_history_token is not None:
            try:
                restore_agent_runtime_history(runtime_history_token)
            except Exception:
                pass


@router.post("/sessions/{session_id}/stop", response_model=StopChatResponse)
async def stop_chat_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StopChatResponse:
    await _get_session_or_404(session_id, current_user.id, db)
    stopped = await get_chat_run_manager().request_stop(session_id)
    return StopChatResponse(stopped=stopped, session_id=session_id)


@router.get(
    "/sessions/{session_id}/status",
    response_model=ChatSessionStatusResponse,
    response_model_exclude_none=True,
    response_model_exclude_defaults=True,
)
async def get_chat_session_status(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSessionStatusResponse:
    await _get_session_or_404(session_id, current_user.id, db)
    manager = get_chat_run_manager()
    active = await manager.is_active(session_id)
    run = await latest_chat_run(db, session_id)
    if run is None:
        return ChatSessionStatusResponse(session_id=session_id, active=active)
    active = (await manager.active_run_id(session_id)) == run.id
    if run.status == "running" and not active:
        await mark_chat_run_terminal(
            db,
            run,
            status="failed",
            phase="interrupted",
            error_classification="process_interrupted",
        )
        await db.commit()
    return ChatSessionStatusResponse(**serialize_chat_run(run, active=active))


@router.get("/runs/{run_id}/status")
async def get_chat_run_status(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    run = await owned_chat_run(db, run_id=run_id, user_id=current_user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="Chat run not found")
    active = (await get_chat_run_manager().active_run_id(run.session_id)) == run.id
    if run.status == "running" and not active:
        await mark_chat_run_terminal(
            db,
            run,
            status="failed",
            phase="interrupted",
            error_classification="process_interrupted",
        )
        await db.commit()
    return serialize_chat_run(run, active=active)


async def _persist_chat_approval_wait(
    *,
    db: AsyncSession,
    run: ChatRun,
    exc: ApprovalRequired,
    pending_tool_calls: List[Dict[str, Any]],
) -> ChatMessage:
    blocked_arguments = dict((exc.payload or {}).get("arguments") or {})
    matching_call = next(
        (
            call
            for call in pending_tool_calls
            if _tool_call_name_from_payload(call) == exc.tool_name
            and (
                not blocked_arguments
                or _tool_call_arguments_from_payload(call) == blocked_arguments
            )
        ),
        pending_tool_calls[0] if pending_tool_calls else None,
    )
    if not isinstance(matching_call, dict):
        raise RuntimeError("Approval was requested without a captured tool call")
    call_id = str(matching_call.get("id") or "").strip()
    payload = dict(exc.payload or {})
    payload.update(
        {
            "reason": exc.reason,
            "approval_kind": exc.approval_kind,
            "tool_call_id": call_id,
        }
    )
    pending_message = ChatMessage(
        session_id=run.session_id,
        role="assistant",
        content="",
        tool_calls=json.dumps([matching_call], ensure_ascii=True, sort_keys=True),
    )
    db.add(pending_message)
    run.status = "waiting_approval"
    run.phase = "waiting_approval"
    run.updated_at = datetime.now(timezone.utc)
    run.approval_kind = exc.approval_kind
    run.approval_payload = payload
    await db.flush()
    await db.commit()
    return pending_message


def _chat_approval_response(run: ChatRun, pending_message: ChatMessage) -> Dict[str, Any]:
    return {
        "role": "assistant",
        "content": "Approval is required before I can perform that action.",
        "message_id": int(pending_message.id),
        "session_id": run.session_id,
        "run_id": run.id,
        "state": "waiting_approval",
        "waiting_approval": serialize_chat_approval(run),
        "metadata": {},
    }


@router.post("/runs/{run_id}/approval")
async def decide_chat_run_approval(
    run_id: str,
    body: ChatApprovalDecision,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    run = await owned_chat_run(db, run_id=run_id, user_id=current_user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="Chat run not found")
    if run.status != "waiting_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Chat run is not waiting for approval (status={run.status})",
        )
    session = await _get_session_or_404(run.session_id, current_user.id, db)
    if session.is_incognito:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Persistent actions are disabled in incognito sessions.",
        )
    payload = run.approval_payload
    if not isinstance(payload, dict):
        raise HTTPException(status_code=409, detail="Chat run has no replayable approval payload")

    decision_payload = dict(payload)
    decision_payload["decision"] = "approved" if body.approved else "denied"
    decision_payload["decided_at"] = datetime.now(timezone.utc).isoformat()
    run.approval_payload = decision_payload
    if not body.approved:
        await mark_chat_run_terminal(
            db,
            run,
            status="cancelled",
            phase="approval_denied",
            error_classification="approval_denied",
        )
        await db.commit()
        return serialize_chat_run(run, active=False)

    run.status = "running"
    run.phase = "replaying_approved_tool"
    await db.commit()
    manager = get_chat_run_manager()
    current_task = asyncio.current_task()
    if current_task is not None:
        await manager.register(run.session_id, current_task, run_id=run.id)

    record_token = reset_tool_execution_records()
    runtime_history_token = reset_agent_runtime_history()
    handoff_token = reset_task_handoff_payload()
    approval_token = None
    try:
        user_context = UserContext.from_user(current_user, persona_name=session.persona)
        prompt_message = await db.get(ChatMessage, run.user_message_id) if run.user_message_id else None
        user_prompt = str(getattr(prompt_message, "content", "") or "")
        user_context = await hydrate_user_context(db, user_context, query=user_prompt)
        user_context.session_id = run.session_id
        user_context.is_incognito = False

        replay_token = _approval_armed.set(False)
        try:
            replay_result, replay_details = await replay_waiting_approval_tool(payload, user_context)
        finally:
            _approval_armed.reset(replay_token)
        tool_call_id = str(payload.get("tool_call_id") or "").strip()
        tool_message = ChatMessage(
            session_id=run.session_id,
            role="tool",
            content=str(replay_result or ""),
            tool_results=json.dumps(
                {"tool_call_id": tool_call_id},
                ensure_ascii=True,
                sort_keys=True,
            ),
        )
        db.add(tool_message)
        run.phase = "approved_tool_completed"
        await db.commit()

        history = await _load_history(run.session_id, db)
        pending_tool_calls: List[Dict[str, Any]] = []

        async def _capture_pre_tool(tool_calls: List[Dict[str, Any]]) -> None:
            pending_tool_calls.clear()
            pending_tool_calls.extend(tool_calls)

        async def _record_run_event(event: AgentEvent) -> None:
            await apply_chat_run_event(db, run, event)
            if event.type != AgentEventType.TEXT_DELTA:
                await db.commit()

        event_emitter = AgentEventEmitter(
            run_id=run.id,
            session_id=run.session_id,
            callback=_record_run_event,
            starting_sequence=int(run.last_event_sequence or 0),
        )
        _flush_runtime_messages, _flush_pending_runtime_history, _ = (
            _build_runtime_history_flush_helpers(
                session_id=run.session_id,
                db=db,
                get_runtime_history=get_agent_runtime_history,
            )
        )
        approval_token = _approval_armed.set(True)
        try:
            reply = await _execute_chat_turn(
                history,
                user_context,
                user_prompt=user_prompt,
                mode=str(run.mode or "chat"),
                model_override=run.model,
                stage=str(run.stage or "chat_simple"),
                enable_validation=False,
                runtime_message_callback=_flush_runtime_messages,
                pre_tool_callback=_capture_pre_tool,
                event_emitter=event_emitter,
            )
        except ApprovalRequired as exc:
            pending_message = await _persist_chat_approval_wait(
                db=db,
                run=run,
                exc=exc,
                pending_tool_calls=pending_tool_calls,
            )
            return _chat_approval_response(run, pending_message)

        await _flush_pending_runtime_history()
        reply = _ensure_generated_image_markdown_references(reply, get_tool_execution_records())
        assistant_metadata = _build_assistant_message_metadata(
            handoff_metadata=get_task_handoff_payload() or {},
            executed_tools=get_tool_execution_records(),
            recalled_memory_ids=[],
        )
        assistant_message = ChatMessage(
            session_id=run.session_id,
            role="assistant",
            content=reply,
            tool_results=(
                _encode_assistant_message_metadata(assistant_metadata)
                if assistant_metadata
                else None
            ),
        )
        db.add(assistant_message)
        await db.flush()
        run.approval_kind = None
        await mark_chat_run_terminal(
            db,
            run,
            status="completed",
            phase="completed",
            assistant_message_id=int(assistant_message.id),
        )
        await db.commit()
        return {
            "role": "assistant",
            "content": reply,
            "message_id": int(assistant_message.id),
            "session_id": run.session_id,
            "run_id": run.id,
            "state": "completed",
            "replayed_tool": replay_details,
            "metadata": assistant_metadata or {},
        }
    except Exception as exc:
        await db.rollback()
        persisted_run = await db.get(ChatRun, run.id)
        if persisted_run is not None and persisted_run.status == "running":
            await mark_chat_run_terminal(
                db,
                persisted_run,
                status="failed",
                phase="failed",
                error_classification=type(exc).__name__,
            )
            await db.commit()
        raise
    finally:
        if approval_token is not None:
            _approval_armed.reset(approval_token)
        restore_tool_execution_records(record_token)
        restore_agent_runtime_history(runtime_history_token)
        restore_task_handoff_payload(handoff_token)
        if current_task is not None:
            await manager.clear(run.session_id, current_task)


async def _run_websocket_message(
    *,
    session_id: int,
    websocket: WebSocket,
    db: AsyncSession,
    current_user: User,
    session: ChatSession,
    user_message: str,
    client_send_id: Optional[str],
    allowed_tools: Optional[List[str]],
    blocked_tools: Optional[List[str]],
    approval_mode: bool = False,
) -> None:
    prompt_claimed = False
    send_id_claimed = False
    websocket_closed = False

    async def _send_json_if_open(payload: Dict[str, Any]) -> bool:
        nonlocal websocket_closed
        if websocket_closed:
            return False
        try:
            await websocket.send_json(payload)
            return True
        except Exception:
            websocket_closed = True
            log.info(
                "chat.websocket_send_skipped",
                session_id=session_id,
                user_id=current_user.id,
                websocket_id=hex(id(websocket)),
                client_send_id=client_send_id or "",
                payload_type=str(payload.get("type", "")),
            )
            return False

    async def _emit_state(
        state: str,
        *,
        tool_names: List[str] | None = None,
        tool_details: List[Dict[str, Any]] | None = None,
        retry_reason: str | None = None,
        attempt: int | None = None,
    ) -> None:
        await _send_json_if_open(
            _build_chat_state_event(
                state,
                tool_names=tool_names,
                tool_details=tool_details,
                retry_reason=retry_reason,
                attempt=attempt,
            )
        )

    try:
        message_started = time.perf_counter()
        stage_timings_ms: Dict[str, float] = {}
        websocket_id = hex(id(websocket))
        prompt_fingerprint = str(abs(hash(" ".join(str(user_message or "").split()))))[:12]
        if client_send_id:
            send_id_ok, send_id_active = await get_chat_run_manager().claim_client_send_id(
                session_id,
                client_send_id,
            )
            if not send_id_ok:
                log.info(
                    "chat.duplicate_client_send_id_rejected",
                    session_id=session_id,
                    user_id=current_user.id,
                    websocket_id=websocket_id,
                    client_send_id=client_send_id,
                    prompt_fingerprint=prompt_fingerprint,
                    active_run=send_id_active,
                )
                await _send_json_if_open(
                    {
                        "type": "error",
                        "content": (
                            "A matching chat request is already running."
                            if send_id_active
                            else "That message was already sent a moment ago."
                        ),
                    }
                )
                return
            send_id_claimed = True
        claimed, duplicate_active, claimed_fingerprint = await get_chat_run_manager().claim_prompt(
            session_id,
            user_message,
        )
        if not claimed:
            log.info(
                "chat.duplicate_prompt_rejected",
                session_id=session_id,
                user_id=current_user.id,
                websocket_id=websocket_id,
                client_send_id=client_send_id or "",
                prompt_fingerprint=claimed_fingerprint,
                active_run=duplicate_active,
            )
            await _send_json_if_open(
                {
                    "type": "error",
                    "content": (
                        "A matching chat request is already running."
                        if duplicate_active
                        else "That message was already sent a moment ago."
                    ),
                }
            )
            return
        prompt_claimed = True
        prompt_fingerprint = claimed_fingerprint or prompt_fingerprint
        log.info(
            "chat.websocket_message_received",
            session_id=session_id,
            user_id=current_user.id,
            websocket_id=websocket_id,
            client_send_id=client_send_id or "",
            prompt_fingerprint=prompt_fingerprint,
        )

        user_msg = ChatMessage(session_id=session_id, role="user", content=user_message)
        db.add(user_msg)
        await db.flush()
        log.info(
            "chat.websocket_message_persisted",
            session_id=session_id,
            user_id=current_user.id,
            websocket_id=websocket_id,
            client_send_id=client_send_id or "",
            prompt_fingerprint=prompt_fingerprint,
            chat_message_id=user_msg.id,
        )

        stage_started = time.perf_counter()
        history = await _load_history(session_id, db)
        _record_chat_stage_timing(stage_timings_ms, "history_load", stage_started)
        workspace_context = await _load_recent_workspace_context(
            session_id,
            user_id=current_user.id,
            db=db,
        )

        user_context = UserContext.from_user(current_user, persona_name=session.persona)
        _apply_tool_overrides(
            user_context,
            allowed_tools=allowed_tools,
            blocked_tools=blocked_tools,
        )
        stage_started = time.perf_counter()
        user_context = await hydrate_user_context(db, user_context, query=user_message)
        _record_chat_stage_timing(stage_timings_ms, "context_hydration", stage_started)
        user_context.session_id = session_id
        user_context.is_incognito = bool(session.is_incognito)
        stage_started = time.perf_counter()
        history, _memory_ids = await _apply_memory_context(
            history,
            db,
            current_user.id,
            user_message,
        )
        _record_chat_stage_timing(stage_timings_ms, "memory_context", stage_started)
        workspace_followup = _is_recent_workspace_followup_prompt(user_message, workspace_context)
        history = _apply_workspace_followup_grounding(
            history,
            user_prompt=user_message,
            context=workspace_context,
        )
        library_list_intent = (not workspace_followup) and is_library_lookup_intent(user_message)
        library_summary_intent = (not workspace_followup) and is_library_summary_intent(user_message)
        library_detail_intent = (not workspace_followup) and is_library_detail_or_excerpt_intent(user_message)
        library_intent = (
            library_list_intent
            or library_summary_intent
            or library_detail_intent
        )
        stage_started = time.perf_counter()
        history = await _apply_required_library_grounding(
            history,
            user_context,
            user_prompt=user_message,
            intent_type=(
                "summary"
                if library_summary_intent
                else (
                    "detail_or_excerpt"
                    if library_detail_intent
                    else ("list_documents" if library_list_intent else None)
                )
            ),
            selected_model=session.llm_model,
        )
        _record_chat_stage_timing(stage_timings_ms, "library_grounding", stage_started)
        full_response: List[str] = []
        provisional_draft_committed = False
        decision = classify_chat_complexity(
            user_message,
            threshold=settings.chat_complexity_threshold,
            routing_enabled=settings.chat_complexity_routing_enabled,
        )
        execution_mode, effective_complex = _resolve_chat_execution(
            auto_complex=(decision.is_complex or library_intent),
            preference=getattr(current_user, "chat_routing_preference", None),
        )
        _log_chat_routing_decision(
            decision=decision,
            preference=getattr(current_user, "chat_routing_preference", None),
            execution_mode=execution_mode,
            effective_complex=effective_complex,
            library_intent=library_intent,
            session_id=session_id,
            transport="websocket",
        )
        should_validate = should_validate_chat_response(
            user_prompt=user_message,
            effective_complex=effective_complex,
        ) or workspace_followup
        stage_started = time.perf_counter()
        execution_history = build_orchestrated_chat_history(
            history,
            enabled=(execution_mode == "chat_orchestrated" and settings.chat_orchestration_enabled),
            max_steps=settings.chat_orchestration_max_steps,
        )
        _record_chat_stage_timing(stage_timings_ms, "orchestration_build", stage_started)
        if decision.is_complex:
            metrics.inc_chat_complexity_complex_count()
            if effective_complex:
                metrics.inc_chat_complexity_routed_complex_count()
        else:
            metrics.inc_chat_complexity_simple_count()

        run_stage = "chat_complex" if effective_complex else "chat_simple"
        chat_run = await create_chat_run(
            db,
            session_id=session_id,
            user_id=current_user.id,
            user_message_id=int(user_msg.id),
            client_send_id=client_send_id,
            model=session.llm_model,
            mode=execution_mode,
            stage=run_stage,
        )
        await db.commit()
        await _send_json_if_open(
            {
                "type": "run_started",
                "run_id": chat_run.id,
                "session_id": session_id,
            }
        )
        pending_tool_calls: List[Dict[str, Any]] = []

        async def _record_run_event(event: AgentEvent) -> None:
            await apply_chat_run_event(db, chat_run, event)
            if event.type != AgentEventType.TEXT_DELTA:
                await db.commit()

        event_emitter = AgentEventEmitter(
            run_id=chat_run.id,
            session_id=session_id,
            callback=_record_run_event,
        )
        current_task = asyncio.current_task()
        if current_task is not None:
            await get_chat_run_manager().register(session_id, current_task, run_id=chat_run.id)
        approval_token = _approval_armed.set(approval_mode and not bool(session.is_incognito))
        record_token = reset_tool_execution_records()
        handoff_token = reset_task_handoff_payload()
        runtime_history_token = reset_agent_runtime_history()
        handoff_metadata: Dict[str, Any] = {}
        runtime_history_messages: List[Dict[str, Any]] = []
        usage_token = bind_llm_usage_context(
            user_id=current_user.id,
            session_id=session_id,
            source="chat_websocket",
        )
        _flush_runtime_messages, _flush_pending_runtime_history, _get_consumed_runtime_message_count = (
            _build_runtime_history_flush_helpers(
                session_id=session_id,
                db=db,
                get_runtime_history=get_agent_runtime_history,
            )
        )

        async def _flush_runtime_messages_with_state(new_messages: List[Dict[str, Any]]) -> None:
            await _flush_runtime_messages(new_messages)
            tool_names = _runtime_messages_tool_names(new_messages)
            if tool_names:
                await _emit_state("tool_completed", tool_names=tool_names)

        async def _emit_pre_tool_state(tool_calls: List[Dict[str, Any]]) -> None:
            pending_tool_calls.clear()
            pending_tool_calls.extend(tool_calls)
            tool_names = _tool_call_names_from_calls(tool_calls)
            tool_details = _build_live_tool_details(tool_calls)
            state = "image_rendering" if any(name == "generate_image" for name in tool_names) else "tool_active"
            await _emit_state(state, tool_names=tool_names, tool_details=tool_details)

        async def _emit_provisional_text(action: str, content: str) -> None:
            nonlocal provisional_draft_committed
            event_type = {
                "delta": "draft_token",
                "reset": "draft_reset",
                "commit": "draft_commit",
            }.get(action)
            if event_type is None:
                raise ValueError(f"Unsupported provisional text action: {action}")
            provisional_draft_committed = action == "commit"
            payload: Dict[str, Any] = {"type": event_type}
            if content:
                payload["content"] = content
            await _send_json_if_open(payload)

        await _emit_state("thinking")

        if should_validate:
            stage_started = time.perf_counter()
            try:
                complete = await _execute_chat_turn(
                    execution_history,
                    user_context,
                    user_prompt=user_message,
                    mode=execution_mode,
                    model_override=session.llm_model,
                    stage="chat_complex" if effective_complex else "chat_simple",
                    enable_validation=True,
                    runtime_message_callback=_flush_runtime_messages_with_state,
                    pre_tool_callback=_emit_pre_tool_state,
                    state_callback=_emit_state,
                    event_emitter=event_emitter,
                )
            except ApprovalRequired:
                raise
            except Exception as e:
                runtime_history_messages = await _flush_pending_runtime_history()
                handoff_metadata = get_task_handoff_payload() or {}
                if _is_local_chat_model(session.llm_model) and _runtime_history_has_completed_tool_turn(runtime_history_messages):
                    complete = _build_local_post_tool_synthesis_recovery_message(runtime_history_messages)
                    log.warning(
                        "chat.local_post_tool_synthesis_recovered",
                        session_id=session_id,
                        user_id=current_user.id,
                        websocket_id=websocket_id,
                        client_send_id=client_send_id or "",
                        model=session.llm_model,
                        mode=execution_mode,
                        stage="chat_complex" if effective_complex else "chat_simple",
                        tool_names=_runtime_history_tool_names(runtime_history_messages),
                        runtime_history_message_count=len(runtime_history_messages),
                        persisted_runtime_messages=_get_consumed_runtime_message_count(),
                        error=str(e),
                        failure_phase="post_tool_synthesis",
                    )
                else:
                    raise
            _record_chat_stage_timing(stage_timings_ms, "model_execution", stage_started)
            complete = _enforce_calendar_mutation_integrity(
                user_message,
                complete,
                get_tool_execution_records(),
            )
            complete = _ensure_generated_image_markdown_references(complete, get_tool_execution_records())
            for token_chunk in _chunk_text(complete):
                full_response.append(token_chunk)
                await _send_json_if_open({"type": "token", "content": token_chunk})
            complete = "".join(full_response)
        else:
            started = time.perf_counter()
            try:
                agent_stream = stream_agent(
                    execution_history,
                    user_context,
                    mode=execution_mode,
                    model_override=session.llm_model,
                    stage="chat_simple",
                    runtime_message_callback=_flush_runtime_messages_with_state,
                    pre_tool_callback=_emit_pre_tool_state,
                    provisional_text_callback=_emit_provisional_text,
                    event_emitter=event_emitter,
                )
                async with contextlib.aclosing(agent_stream):
                    async for token_chunk in agent_stream:
                        full_response.append(token_chunk)
                        if not provisional_draft_committed:
                            await _send_json_if_open({"type": "token", "content": token_chunk})
            except ApprovalRequired:
                raise
            except Exception as e:
                runtime_history_messages = await _flush_pending_runtime_history()
                handoff_metadata = get_task_handoff_payload() or {}
                if _is_local_chat_model(session.llm_model) and _runtime_history_has_completed_tool_turn(runtime_history_messages):
                    complete = _build_local_post_tool_synthesis_recovery_message(runtime_history_messages)
                    full_response = []
                    for token_chunk in _chunk_text(complete):
                        full_response.append(token_chunk)
                        await _send_json_if_open({"type": "token", "content": token_chunk})
                    log.warning(
                        "chat.local_post_tool_synthesis_recovered",
                        session_id=session_id,
                        user_id=current_user.id,
                        websocket_id=websocket_id,
                        client_send_id=client_send_id or "",
                        model=session.llm_model,
                        mode=execution_mode,
                        stage="chat_simple",
                        tool_names=_runtime_history_tool_names(runtime_history_messages),
                        runtime_history_message_count=len(runtime_history_messages),
                        persisted_runtime_messages=_get_consumed_runtime_message_count(),
                        error=str(e),
                        failure_phase="post_tool_synthesis",
                    )
                else:
                    raise
            metrics.record_chat_latency(
                mode="chat",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
            _record_chat_stage_timing(stage_timings_ms, "model_execution", started)
            if not full_response:
                complete = ""
            else:
                complete = _enforce_calendar_mutation_integrity(
                    user_message,
                    "".join(full_response),
                    get_tool_execution_records(),
                )
                complete = _ensure_generated_image_markdown_references(complete, get_tool_execution_records())
        runtime_history_messages = await _flush_pending_runtime_history()
        handoff_metadata = get_task_handoff_payload() or {}
        assistant_metadata = _build_assistant_message_metadata(
            handoff_metadata=handoff_metadata,
            executed_tools=get_tool_execution_records(),
            recalled_memory_ids=_memory_ids,
        )
        assistant_msg = ChatMessage(
            session_id=session_id,
            role="assistant",
            content=complete,
            tool_results=_encode_assistant_message_metadata(assistant_metadata) if assistant_metadata else None,
        )
        db.add(assistant_msg)
        await _mark_recalled_memories_materialized(db, _memory_ids)
        await db.flush()
        await mark_chat_run_terminal(
            db,
            chat_run,
            status="completed",
            phase="completed",
            assistant_message_id=int(assistant_msg.id),
        )
        await db.commit()
        _log_chat_latency_breakdown(
            session_id=session_id,
            mode=execution_mode,
            total_started=message_started,
            stage_timings_ms=stage_timings_ms,
            transport="websocket",
        )

        await _emit_state("completed")
        await _send_json_if_open(
            {
                "type": "done",
                "content": complete,
                "message_id": int(assistant_msg.id),
                "run_id": chat_run.id,
                "metadata": {
                    "active_skills": list(user_context.active_skill_slugs or []),
                    "skill_selection_mode": user_context.skill_selection_mode or "",
                    **(assistant_metadata or {}),
                    **handoff_metadata,
                },
            }
        )
        log.info(
            "chat.websocket_message_done",
            session_id=session_id,
            user_id=current_user.id,
            websocket_id=websocket_id,
            client_send_id=client_send_id or "",
            prompt_fingerprint=prompt_fingerprint,
            assistant_message_id=assistant_msg.id,
            run_id=chat_run.id,
        )
    except ApprovalRequired as exc:
        pending_message = await _persist_chat_approval_wait(
            db=db,
            run=chat_run,
            exc=exc,
            pending_tool_calls=pending_tool_calls,
        )
        await _emit_state(
            "waiting_approval",
            tool_names=[exc.tool_name],
        )
        await _send_json_if_open(
            {
                "type": "approval_required",
                "run_id": chat_run.id,
                "message_id": int(pending_message.id),
                "waiting_approval": serialize_chat_approval(chat_run),
            }
        )
        log.info(
            "chat.websocket_waiting_approval",
            session_id=session_id,
            run_id=chat_run.id,
            tool=exc.tool_name,
        )
    except asyncio.CancelledError:
        await db.rollback()
        if "chat_run" in locals():
            persisted_run = await db.get(ChatRun, chat_run.id)
            if persisted_run is not None:
                await mark_chat_run_terminal(
                    db,
                    persisted_run,
                    status="cancelled",
                    phase="cancelled",
                    error_classification="user_cancelled",
                )
                await db.commit()
        log.info(
            "chat.websocket_message_stopped",
            session_id=session_id,
            user_id=current_user.id,
            websocket_id=hex(id(websocket)),
            client_send_id=client_send_id or "",
        )
        raise
    except Exception as exc:
        await db.rollback()
        if "chat_run" in locals():
            persisted_run = await db.get(ChatRun, chat_run.id)
            if persisted_run is not None and persisted_run.status == "running":
                await mark_chat_run_terminal(
                    db,
                    persisted_run,
                    status="failed",
                    phase="failed",
                    error_classification=type(exc).__name__,
                )
                await db.commit()
        log.exception(
            "chat.websocket_message_error",
            session_id=session_id,
            user_id=current_user.id,
            websocket_id=hex(id(websocket)),
            client_send_id=client_send_id or "",
        )
        raise
    finally:
        if prompt_claimed:
            await get_chat_run_manager().mark_prompt_finished(session_id, user_message)
        if send_id_claimed and client_send_id:
            await get_chat_run_manager().mark_client_send_id_finished(session_id, client_send_id)
        if "record_token" in locals():
            restore_tool_execution_records(record_token)
        if "usage_token" in locals():
            reset_llm_usage_context(usage_token)
        if "handoff_token" in locals():
            restore_task_handoff_payload(handoff_token)
        if "runtime_history_token" in locals():
            restore_agent_runtime_history(runtime_history_token)
        if "approval_token" in locals() and approval_token is not None:
            _approval_armed.reset(approval_token)


# ── WebSocket /chat/sessions/{id}/ws (streaming) ─────────────────────────────

@router.websocket("/sessions/{session_id}/ws")
async def chat_websocket(
    session_id: int,
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    """
    WebSocket streaming chat.

    Auth path 1 — native client (Swift URLSessionWebSocketTask):
      HTTP upgrade header: Authorization: Bearer <token>
      First WS message:    {"content": "user message"}

    Auth path 2 — web/legacy client (backward compatible):
      First WS message:    {"content": "user message", "token": "Bearer <token>"}

    Server sends:  {"type": "draft_token", "content": "..."} (provisional)
                   {"type": "draft_reset"}                   (tool superseded draft)
                   {"type": "draft_commit"}                  (promote provisional text)
                   {"type": "token",   "content": "..."}   (committed chunk)
                   {"type": "done",    "content": "full response"}
                   {"type": "persona", "content": "...", "persona": "name"}
                   {"type": "error",   "content": "error message"}
    """
    await websocket.accept()
    metrics.ws_connect()
    chat_run_manager = get_chat_run_manager()

    try:
        from app.auth.jwt import decode_token
        from app.db.models import User as UserModel

        current_user: Optional[UserModel] = None

        # ── Auth path 1: Authorization header (Swift URLSessionWebSocketTask) ──
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            try:
                payload = decode_token(token)
                user_id = int(payload["sub"])
                result = await db.execute(select(UserModel).where(UserModel.id == user_id))
                candidate = result.scalar_one_or_none()
                if candidate and candidate.is_active:
                    current_user = candidate
            except Exception:
                await websocket.send_json({"type": "error", "content": "unauthorized"})
                await websocket.close()
                return

        # ── Read first message (content; token optional for legacy clients) ──
        websocket_id = hex(id(websocket))
        websocket_message_index = 0

        def _log_payload_received(payload: Dict[str, Any], send_id: Optional[str]) -> None:
            nonlocal websocket_message_index
            websocket_message_index += 1
            log.info(
                "chat.websocket_payload_received",
                session_id=session_id,
                websocket_id=websocket_id,
                websocket_message_index=websocket_message_index,
                client_send_id=send_id or "",
                message_type=str(payload.get("type", "")) if isinstance(payload, dict) else "",
            )

        def _decode_payload(message_data: Dict[str, Any]) -> ChatSocketPayload:
            content = message_data.get("content", "").strip() if isinstance(message_data, dict) else ""
            client_send_id = message_data.get("client_send_id") if isinstance(message_data, dict) else None
            allowed_tools = message_data.get("allowed_tools") if isinstance(message_data, dict) else None
            blocked_tools = message_data.get("blocked_tools") if isinstance(message_data, dict) else None
            approval_mode = bool(message_data.get("approval_mode", False)) if isinstance(message_data, dict) else False
            return ChatSocketPayload(
                raw=message_data,
                content=content,
                client_send_id=client_send_id,
                allowed_tools=allowed_tools,
                blocked_tools=blocked_tools,
                approval_mode=approval_mode,
            )

        async def _read_next_payload() -> Optional[ChatSocketPayload]:
            while True:
                try:
                    raw_text = await websocket.receive_text()
                except (WebSocketDisconnect, RuntimeError):
                    return None
                try:
                    message_data = json.loads(raw_text)
                except Exception:
                    await websocket.send_json({"type": "error", "content": "invalid payload"})
                    continue
                message_payload = _decode_payload(message_data)
                _log_payload_received(message_payload.raw, message_payload.client_send_id)
                return message_payload

        payload = await _read_next_payload()
        if payload is None:
            return

        # ── Auth path 2: token in first message body (backward compat) ──
        if current_user is None:
            token = payload.raw.get("token", "").removeprefix("Bearer ").strip()
            if not token:
                await websocket.send_json({"type": "error", "content": "token and content required"})
                await websocket.close()
                return
            token_payload = decode_token(token)
            user_id = int(token_payload["sub"])
            result = await db.execute(select(UserModel).where(UserModel.id == user_id))
            current_user = result.scalar_one_or_none()
            if not current_user or not current_user.is_active:
                await websocket.send_json({"type": "error", "content": "unauthorized"})
                await websocket.close()
                return

        # ── Verify session ownership (once per connection) ───────────────────
        session_result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id, ChatSession.user_id == user_id
            )
        )
        session = session_result.scalar_one_or_none()
        if not session:
            await websocket.send_json({"type": "error", "content": "session not found"})
            await websocket.close()
            return

        active_message_task: Optional[asyncio.Task] = None

        async def _extract_payload_from_receive_task(task: asyncio.Task) -> tuple[Optional[ChatSocketPayload], bool]:
            try:
                raw_text = task.result()
            except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                return None, True
            try:
                message_data = json.loads(raw_text)
            except Exception:
                await websocket.send_json({"type": "error", "content": "invalid payload"})
                return None, False
            message_payload = _decode_payload(message_data)
            _log_payload_received(message_payload.raw, message_payload.client_send_id)
            return message_payload, False

        async def _start_message_run(message_payload: ChatSocketPayload) -> None:
            nonlocal active_message_task, session
            if active_message_task is not None and not active_message_task.done():
                await websocket.send_json(
                    {
                        "type": "error",
                        "content": "A chat response is already running. Stop it before sending another message.",
                    }
                )
                return
            # The socket can outlive preference updates made through the HTTP API.
            # Refresh the narrow mutable field instead of forcing a reconnect.
            await db.refresh(current_user, attribute_names=["chat_routing_preference"])
            sr = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
            session = sr.scalar_one_or_none() or session
            active_message_task = asyncio.create_task(
                _run_websocket_message(
                    session_id=session_id,
                    websocket=websocket,
                    db=db,
                    current_user=current_user,
                    session=session,
                    user_message=message_payload.content,
                    client_send_id=message_payload.client_send_id,
                    allowed_tools=message_payload.allowed_tools,
                    blocked_tools=message_payload.blocked_tools,
                    approval_mode=message_payload.approval_mode,
                )
            )
            await chat_run_manager.register(session_id, active_message_task)

        async def _handle_control_message(message_payload: ChatSocketPayload) -> bool:
            message_type = str(message_payload.raw.get("type", "")).strip().lower()
            if message_type != "stop":
                return False
            stopped = await chat_run_manager.request_stop(session_id)
            await websocket.send_json(
                {
                    "type": "stop_requested" if stopped else "stopped",
                    "content": "Stopping chat response" if stopped else "No active chat response to stop.",
                    "stopped": stopped,
                }
            )
            return True

        # ── Message loop — keep connection alive for the session lifetime ────
        while True:
            if active_message_task is None:
                if payload is None:
                    payload = await _read_next_payload()
                    if payload is None:
                        break

                if await _handle_control_message(payload):
                    payload = None
                    continue

                if not payload.content:
                    await websocket.send_json({"type": "error", "content": "content required"})
                    payload = None
                    continue

                persona_name = _parse_persona_command(payload.content)
                if persona_name is not None:
                    resp = await _switch_persona(session_id, persona_name, session, db)
                    await websocket.send_json({
                        "type": "persona",
                        "content": resp["content"],
                        "persona": resp.get("persona_switched", persona_name),
                    })
                    await websocket.send_json({"type": "done", "content": resp["content"]})
                    sr = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
                    session = sr.scalar_one_or_none() or session
                    payload = None
                    continue

                await _start_message_run(payload)
                payload = None
                continue

            receive_task = asyncio.create_task(websocket.receive_text())
            done, _pending = await asyncio.wait(
                {active_message_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if active_message_task in done:
                try:
                    await active_message_task
                except asyncio.CancelledError:
                    await websocket.send_json(
                        {"type": "stopped", "content": "Stopped by user", "stopped": True}
                    )
                finally:
                    await chat_run_manager.clear(session_id, active_message_task)
                    active_message_task = None
                if receive_task in done:
                    payload, disconnected = await _extract_payload_from_receive_task(receive_task)
                    if payload is None:
                        if disconnected:
                            break
                        continue
                else:
                    receive_task.cancel()
                    try:
                        await receive_task
                    except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                        pass
                    payload = None
                continue

            payload, disconnected = await _extract_payload_from_receive_task(receive_task)
            if payload is None:
                if disconnected:
                    break
                continue
            if not await _handle_control_message(payload):
                await websocket.send_json(
                    {
                        "type": "error",
                        "content": "A chat response is already running. Stop it before sending another message.",
                    }
                )
            payload = None

    except WebSocketDisconnect:
        if 'active_message_task' in locals() and active_message_task is not None and not active_message_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await active_message_task
    except Exception as e:
        log.exception("Unhandled error in WebSocket handler", session_id=session_id)
        try:
            await websocket.send_json({"type": "error", "content": "Server error — check server logs"})
        except Exception:
            pass
    finally:
        if 'active_message_task' in locals() and active_message_task is not None:
            if active_message_task.done():
                await chat_run_manager.clear(session_id, active_message_task)
        metrics.ws_disconnect()


# ── Persona command helpers ───────────────────────────────────────────────────

def _parse_persona_command(content: str) -> Optional[str]:
    """
    If the message is a /persona command, return the requested persona name.
    Returns None for normal messages.
    """
    stripped = content.strip()
    if stripped.lower().startswith("/persona "):
        parts = stripped.split(None, 1)
        if len(parts) == 2:
            return parts[1].strip().lower().replace(" ", "_")
    return None


async def _switch_persona(
    session_id: int,
    persona_name: str,
    session: ChatSession,
    db: AsyncSession,
) -> Dict[str, Any]:
    """Switch the session persona and return a confirmation message."""
    from app.agent.persona_loader import get_persona, list_personas, persona_exists

    if not persona_exists(persona_name):
        available = ", ".join(list_personas().keys())
        return {
            "role": "assistant",
            "content": f"Unknown persona '{persona_name}'. Available personas: {available}",
            "session_id": session_id,
        }

    # Persist to DB
    session.persona = persona_name
    await db.commit()

    pc = get_persona(persona_name)
    description = pc.get("description", "")
    tone = pc.get("tone", "")
    blocked = pc.get("blocked_tools", [])

    parts = [f"Switched to persona: **{persona_name}**"]
    if description:
        parts.append(description)
    if tone:
        parts.append(f"Tone: {tone}")
    if blocked:
        parts.append(f"Unavailable tools in this persona: {', '.join(blocked)}")

    return {
        "role": "assistant",
        "content": "\n".join(parts),
        "session_id": session_id,
        "persona_switched": persona_name,
    }


# ── Shared helpers ────────────────────────────────────────────────────────────

def _chunk_text(content: str, chunk_size: int = 64):
    for i in range(0, len(content), chunk_size):
        yield content[i : i + chunk_size]


def _record_chat_stage_timing(stage_timings_ms: Dict[str, float], stage: str, started: float) -> None:
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    stage_timings_ms[stage] = round(elapsed_ms, 2)
    metrics.record_chat_stage_latency(stage=stage, elapsed_ms=elapsed_ms)


def _log_chat_latency_breakdown(
    *,
    session_id: int,
    mode: str,
    total_started: float,
    stage_timings_ms: Dict[str, float],
    transport: str,
) -> None:
    log.info(
        "chat.latency_breakdown",
        session_id=session_id,
        mode=mode,
        transport=transport,
        total_ms=round((time.perf_counter() - total_started) * 1000.0, 2),
        **{f"{stage}_ms": elapsed for stage, elapsed in sorted(stage_timings_ms.items())},
    )


def _resolve_chat_mode(is_complex: bool) -> str:
    if is_complex and settings.chat_orchestration_kill_switch:
        metrics.inc_chat_orchestration_kill_switch_suppressed_count()
        return "chat"
    return "chat_orchestrated" if is_complex else "chat"


def _runtime_messages_tool_names(runtime_messages: List[Dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for message in runtime_messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            tool_name = str(function.get("name") or "").strip()
            if tool_name and tool_name not in seen:
                seen.add(tool_name)
                names.append(tool_name)
    return names


def _tool_call_names_from_calls(tool_calls: List[Dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for call in tool_calls or []:
        function = (call or {}).get("function") or {}
        tool_name = str(function.get("name") or "").strip()
        if tool_name and tool_name not in seen:
            seen.add(tool_name)
            names.append(tool_name)
    return names


def _tool_call_arguments(call: Dict[str, Any]) -> Dict[str, Any]:
    function = (call or {}).get("function") or {}
    raw_args = function.get("arguments") or {}
    if isinstance(raw_args, dict):
        return dict(raw_args)
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _build_live_tool_details(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    details: List[Dict[str, Any]] = []
    for call in tool_calls or []:
        function = (call or {}).get("function") or {}
        tool_name = str(function.get("name") or "").strip()
        if not tool_name:
            continue
        arguments = _tool_call_arguments(call)
        if tool_name == "generate_image":
            sanitized_args = _sanitize_image_render_arguments(arguments)
        else:
            sanitized_args = _sanitize_live_tool_arguments(tool_name, arguments)
        if sanitized_args:
            details.append({"tool_name": tool_name, "arguments": sanitized_args})
    return details


_LIVE_TOOL_ARGUMENT_FIELDS: Dict[str, Dict[str, tuple[str, ...]]] = {
    "web_search": {"query": ("query", "q")},
    "fetch_page": {"url": ("url",)},
    "search_library": {"query": ("query",), "document": ("document_name", "filename")},
    "summarize_document": {
        "document": ("document_name", "filename"),
        "section": ("section", "section_query", "query"),
    },
    "search_my_feeds": {"query": ("query",), "category": ("category",)},
    "search_my_feeds_timeline": {
        "query": ("query",),
        "start": ("start_date",),
        "end": ("end_date",),
    },
    "search_feeds": {"query": ("query",), "category": ("category",)},
    "get_feed_items": {"source": ("source_id", "source"), "category": ("category",)},
    "read_file": {"path": ("path",)},
    "write_file": {"path": ("path",)},
    "append_file": {"path": ("path",)},
    "stat_file": {"path": ("path",)},
    "list_directory": {"path": ("path",)},
    "make_directory": {"path": ("path",)},
    "find_files": {"path": ("path", "root"), "pattern": ("pattern", "query")},
    "describe_image": {"path": ("path",), "question": ("question",)},
    "search_places": {"query": ("query",), "location": ("location",)},
    "get_daily_market_data": {"symbol": ("symbol",)},
    "get_intraday_market_data": {"symbol": ("symbol",), "interval": ("interval",)},
    "api_request": {"method": ("method",), "url": ("url",)},
    "get_task": {"task": ("task_id",)},
    "run_task_now": {"task": ("task_id",)},
    "update_task": {"task": ("task_id",), "title": ("title",)},
    "create_task": {"title": ("title",)},
    "propose_task_draft": {"title": ("title",)},
    "search_memory_graph": {"query": ("query",)},
}


def _sanitize_live_tool_arguments(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Expose only bounded, non-sensitive arguments useful for live operator feedback."""
    fields = _LIVE_TOOL_ARGUMENT_FIELDS.get(tool_name) or {}
    sanitized: Dict[str, Any] = {}
    for label, candidates in fields.items():
        value = next((arguments.get(key) for key in candidates if arguments.get(key) not in (None, "")), None)
        if value is None or isinstance(value, dict):
            continue
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value[:5])
        if isinstance(value, (int, float, bool)):
            sanitized[label] = value
            continue
        compact = " ".join(str(value).split())
        if label == "url":
            parsed = urlparse(compact)
            if parsed.scheme and parsed.netloc:
                compact = parsed._replace(query="", fragment="").geturl()
        if compact:
            sanitized[label] = compact[:300]
    return sanitized


def _sanitize_image_render_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
    aliases = {
        "prompt": ("prompt", "positive_prompt", "description"),
        "model": ("model", "model_name", "checkpoint", "ckpt_name"),
        "workflow": ("workflow", "workflow_name", "template"),
        "steps": ("steps", "num_steps"),
        "seed": ("seed",),
        "width": ("width",),
        "height": ("height",),
        "cfg_scale": ("cfg_scale", "guidance", "guidance_scale"),
    }
    sanitized: Dict[str, Any] = {}
    for canonical, candidates in aliases.items():
        value = next((arguments.get(key) for key in candidates if arguments.get(key) not in (None, "")), None)
        if value is None:
            continue
        if canonical in {"steps", "seed", "width", "height"}:
            try:
                sanitized[canonical] = int(value)
            except (TypeError, ValueError):
                sanitized[canonical] = str(value).strip()[:80]
        elif canonical == "cfg_scale":
            try:
                sanitized[canonical] = float(value)
            except (TypeError, ValueError):
                sanitized[canonical] = str(value).strip()[:80]
        else:
            sanitized[canonical] = str(value).strip()[:500]
    return sanitized


def _build_chat_state_event(
    state: str,
    *,
    tool_names: List[str] | None = None,
    tool_details: List[Dict[str, Any]] | None = None,
    retry_reason: str | None = None,
    attempt: int | None = None,
) -> Dict[str, Any]:
    normalized_state = str(state or "").strip().lower()
    if normalized_state not in _CHAT_LIVE_STATE_VALUES:
        raise ValueError(f"Unsupported chat live state: {state}")

    payload: Dict[str, Any] = {
        "type": "state",
        "state": normalized_state,
    }
    if isinstance(tool_names, list):
        cleaned_tool_names = [str(item).strip() for item in tool_names if str(item).strip()]
        if cleaned_tool_names:
            payload["tool_names"] = cleaned_tool_names
    if isinstance(tool_details, list):
        cleaned_details = [
            detail
            for detail in tool_details
            if isinstance(detail, dict) and str(detail.get("tool_name") or "").strip()
        ]
        if cleaned_details:
            payload["tool_details"] = cleaned_details
    if retry_reason:
        payload["retry_reason"] = str(retry_reason).strip()
    if attempt is not None:
        try:
            payload["attempt"] = int(attempt)
        except (TypeError, ValueError):
            pass
    return payload


def _normalize_chat_routing_preference(value: str | None) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"auto", "fast", "deep"}:
        return lowered
    return "auto"


def _log_chat_routing_decision(
    *,
    decision: ChatComplexityDecision,
    preference: str | None,
    execution_mode: str,
    effective_complex: bool,
    library_intent: bool,
    session_id: int,
    transport: str,
) -> None:
    log.info(
        "chat.routing_decision",
        session_id=session_id,
        transport=transport,
        preference=_normalize_chat_routing_preference(preference),
        classifier_score=getattr(decision, "score", None),
        classifier_reasons=list(getattr(decision, "reasons", []) or []),
        classifier_complex=decision.is_complex,
        library_intent=library_intent,
        execution_mode=execution_mode,
        effective_complex=effective_complex,
    )


def _resolve_chat_execution(
    *,
    auto_complex: bool,
    preference: str | None,
) -> tuple[str, bool]:
    normalized = _normalize_chat_routing_preference(preference)
    if normalized == "fast":
        return "chat", False
    if normalized == "deep":
        mode = _resolve_chat_mode(True)
        return mode, mode == "chat_orchestrated"
    mode = _resolve_chat_mode(auto_complex)
    return mode, auto_complex and mode == "chat_orchestrated"


async def _execute_chat_turn(
    history: List[Dict[str, Any]],
    user_context: UserContext,
    *,
    user_prompt: str,
    mode: str,
    model_override: str | None,
    stage: str,
    enable_validation: bool,
    runtime_message_callback=None,
    pre_tool_callback=None,
    state_callback: Callable[..., Awaitable[None]] | None = None,
    event_emitter: AgentEventEmitter | None = None,
) -> str:
    started = time.perf_counter()
    reply = await run_agent(
        history,
        user_context,
        mode=mode,
        model_override=model_override,
        stage=stage,
        runtime_message_callback=runtime_message_callback,
        pre_tool_callback=pre_tool_callback,
        event_emitter=event_emitter,
    )
    executed_tools = get_tool_execution_records()
    should_run_validation = settings.chat_validation_enabled and (enable_validation or bool(executed_tools))
    if not should_run_validation:
        metrics.record_chat_latency(
            mode=mode,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        return reply

    max_attempts = max(0, int(settings.chat_validation_retry_max_attempts))
    attempts = 0
    current = reply

    while True:
        if state_callback is not None:
            await state_callback("validating")
        if event_emitter is not None:
            await event_emitter.emit(AgentEventType.VALIDATION_STARTED, attempt=attempts + 1)
        validation = validate_chat_response(
            user_prompt,
            current,
            executed_tools=get_tool_execution_records(),
        )
        if validation.invalid_urls:
            metrics.inc_chat_validation_invalid_link_count(len(validation.invalid_urls))

        should_retry = (
            settings.chat_validation_retry_enabled
            and validation.should_retry
            and attempts < max_attempts
        )
        if not should_retry:
            metrics.record_chat_latency(
                mode=mode,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
            return validation.cleaned_content if validation.invalid_urls else current

        if validation.retry_reason == "empty_result":
            metrics.inc_chat_validation_empty_retry_count()
        metrics.inc_chat_validation_retry_count()
        attempts += 1
        if state_callback is not None:
            await state_callback("retrying", retry_reason=validation.retry_reason, attempt=attempts)
        if event_emitter is not None:
            await event_emitter.emit(
                AgentEventType.VALIDATION_RETRY,
                retry_reason=validation.retry_reason,
                attempt=attempts,
            )

        retry_instruction = build_chat_retry_instruction(validation.retry_reason)
        corrective = {"role": "system", "content": retry_instruction}
        retry_history = list(history)
        if retry_history and retry_history[-1].get("role") == "user":
            retry_history = retry_history[:-1] + [corrective, retry_history[-1]]
        else:
            retry_history.append(corrective)

        current = await run_agent(
            retry_history,
            user_context,
            mode=mode,
            model_override=model_override,
            stage=f"{stage}_retry",
            runtime_message_callback=runtime_message_callback,
            pre_tool_callback=pre_tool_callback,
            event_emitter=event_emitter,
        )


def _enforce_calendar_mutation_integrity(
    user_prompt: str,
    response: str,
    executed_tools: list[dict[str, Any]],
) -> str:
    validation = validate_chat_response(
        user_prompt,
        response,
        executed_tools=executed_tools,
    )
    if validation.mutation_unconfirmed:
        return (
            "I couldn't confirm that the calendar change actually succeeded. "
            "Please check your calendar and try again."
        )
    return _strip_calendar_event_ids(response, executed_tools)


def _strip_calendar_event_ids(
    response: str,
    executed_tools: list[dict[str, Any]],
) -> str:
    calendar_tools = {
        "list_events",
        "search_events",
        "create_event",
        "delete_event",
        "update_event",
        "move_event",
    }
    if not any((record or {}).get("tool") in calendar_tools for record in executed_tools):
        return response

    cleaned = response
    cleaned = re.sub(
        r"(^\s*[•*-]\s*)\[[^\]]+\]\s*",
        r"\1",
        cleaned,
        flags=re.MULTILINE,
    )
    cleaned = re.sub(r"\s*\(id:\s*[^)]+\)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(event id|id)\s*[:=]\s*[A-Za-z0-9_.:@-]+\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return cleaned.strip()


async def _apply_required_library_grounding(
    history: List[Dict[str, Any]],
    user_context: UserContext,
    *,
    user_prompt: str,
    intent_type: str | None,
    selected_model: str | None = None,
) -> List[Dict[str, Any]]:
    """
    For explicit library lookup intents, fetch grounded library evidence before
    the assistant turn so chat does not invent document names.
    """
    if not intent_type:
        return history

    blocked = {str(name).strip() for name in (user_context.blocked_tools or [])}
    from app.agent.tools import (
        _list_library_documents,
        _search_library,
        _summarize_document,
        _write_audit_log,
    )

    if intent_type == "list_documents":
        tool_name = "list_library_documents"
        args = {"limit": 50}
        if tool_name in blocked:
            result = "list_library_documents is unavailable for this persona."
        else:
            result = await _list_library_documents(args, user_context)
    elif intent_type == "summary":
        tool_name = "summarize_document"
        if tool_name in blocked:
            args = {"document_name": user_prompt}
            result = "summarize_document is unavailable for this persona."
        else:
            listing = await _list_library_documents({"limit": 50}, user_context)
            resolved_name = None
            ambiguous: list[str] = []
            try:
                payload = json.loads(listing)
                filenames = [
                    str(doc.get("filename", "")).strip()
                    for doc in payload.get("documents", [])
                    if str(doc.get("filename", "")).strip()
                ]
                resolved_name, ambiguous = resolve_document_name(user_prompt, filenames)
            except Exception:
                filenames = []
            if resolved_name:
                args = {"document_name": resolved_name}
                result = await _summarize_document(args, user_context)
            elif ambiguous:
                args = {"document_name": normalize_document_name_query(user_prompt)}
                choices = "\n".join(f"- {name}" for name in ambiguous)
                result = (
                    f"Multiple documents match '{user_prompt}':\n{choices}\n"
                    "Please use the exact filename."
                )
            else:
                args = {"document_name": normalize_document_name_query(user_prompt)}
                result = await _summarize_document(args, user_context)
    else:
        tool_name = "search_library"
        args = {"query": user_prompt, "top_k": 20}
        if tool_name in blocked:
            result = "search_library is unavailable for this persona."
        else:
            result = await _search_library(args, user_context)

    await _write_audit_log(
        user_id=user_context.user_id,
        session_id=user_context.session_id,
        tool_name=tool_name,
        arguments=args,
        result_summary=str(result)[:500],
    )
    result_label = f"{tool_name} result"
    result_body = result
    if intent_type == "summary" and _is_local_chat_model(selected_model):
        result_label = "document summary evidence digest"
        result_body = build_local_document_summary_digest(str(result))
        grounding_note = (
            "Required grounding for this turn: this is a library intent. "
            "Prioritize the newest user message over prior context. "
            "For this local model, the compact document-summary evidence below is the source of truth for the answer. "
            "Answer the user's summary request directly, stay close to the evidence, and if a detail is missing or unclear, say so briefly instead of inferring it. "
            "If the evidence is empty or insufficient, say so explicitly.\n\n"
            f"{result_label}:\n{result_body}"
        )
    else:
        grounding_note = (
            "Required grounding for this turn: this is a library intent. "
            "Prioritize the newest user message over prior context. "
            "Use only the tool output below as source of truth for document names/metadata. "
            "If output is empty, explicitly say no documents/excerpts were found.\n\n"
            f"{result_label}:\n{result_body}"
        )

    grounded = list(history)
    if grounded and grounded[-1].get("role") == "user":
        return grounded[:-1] + [{"role": "system", "content": grounding_note}, grounded[-1]]
    return grounded + [{"role": "system", "content": grounding_note}]


def _memory_retrieval_query(
    history: List[Dict[str, Any]],
    user_prompt: str,
    *,
    prior_user_turns: int = 2,
    max_chars: int = 900,
) -> str:
    """Retrieval text from the latest prompt plus recent user turns.

    A short follow-up ("what about tuesday?") carries almost no retrievable
    signal by itself; the preceding user turns supply the topic. The latest
    prompt goes last so lexical/vector matching still weights it most.
    """
    parts: list[str] = []
    prior: list[str] = []
    for message in reversed(history):
        if len(prior) >= prior_user_turns:
            break
        if str(message.get("role") or "") != "user":
            continue
        content = str(message.get("content") or "").strip()
        if content and content != str(user_prompt or "").strip():
            prior.append(content)
    parts.extend(reversed(prior))
    parts.append(str(user_prompt or "").strip())
    combined = "\n".join(part for part in parts if part)
    return combined[-max_chars:]


async def _apply_memory_context(
    history: List[Dict[str, Any]],
    db: AsyncSession,
    user_id: int,
    user_prompt: str,
) -> tuple[List[Dict[str, Any]], List[int]]:
    """
    Inject baseline memory context for the current turn without mutating memory access scores.
    """
    svc = get_memory_service()
    retrieval_query = _memory_retrieval_query(history, user_prompt)
    memories = await svc.retrieve_for_context(db, user_id, query=retrieval_query)
    if not memories:
        return history, []

    memory_note = (
        "Baseline memory context for this user. Use it as supporting context, "
        "but prioritize the latest user message and any grounded tool output.\n\n"
        f"{svc.format_for_prompt(memories)}"
    )
    grounded = list(history)
    if grounded and grounded[-1].get("role") == "user":
        grounded = grounded[:-1] + [{"role": "system", "content": memory_note}, grounded[-1]]
    else:
        grounded.append({"role": "system", "content": memory_note})
    return grounded, [int(m.id) for m in memories]


async def _mark_recalled_memories_materialized(
    db: AsyncSession,
    memory_ids: List[int] | None,
) -> None:
    """
    Chat retrieval stays passive until a turn actually completes.
    Once we are persisting a successful assistant message, treat recalled
    memories as materially used and record access in the same transaction.
    """
    if not memory_ids:
        return
    svc = get_memory_service()
    await svc.mark_accessed(memory_ids, mode="chat_materialized", db=db)


async def _get_session_or_404(
    session_id: int, user_id: int, db: AsyncSession
) -> ChatSession:
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id, ChatSession.user_id == user_id
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _get_chat_message_or_404(
    message_id: int,
    user_id: int,
    db: AsyncSession,
) -> ChatMessage:
    result = await db.execute(
        select(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(
            ChatMessage.id == message_id,
            ChatSession.user_id == user_id,
        )
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=404, detail="Chat message not found")
    return message


async def _reject_if_incognito_session(session_id: int, db: AsyncSession) -> None:
    """Persistent mutations (task drafts etc.) are blocked for incognito
    sessions at the endpoint layer too — the tool surface filter alone can't
    cover REST flows that act on prior messages."""
    session = await db.get(ChatSession, session_id)
    if session is not None and bool(session.is_incognito):
        raise HTTPException(
            status_code=409,
            detail="Persistent actions are disabled in incognito sessions.",
        )


def _summarize_executed_tool_names(records: List[Dict[str, Any]]) -> List[str]:
    seen: set[str] = set()
    names: list[str] = []
    for record in records or []:
        name = str(record.get("tool") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _normalize_assistant_metadata_payload(metadata: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    task_draft = metadata.get("task_draft")
    if isinstance(task_draft, dict) and task_draft:
        normalized["task_draft"] = task_draft

    created_task_id = metadata.get("created_task_id")
    if created_task_id is not None:
        try:
            normalized["created_task_id"] = int(created_task_id)
        except (TypeError, ValueError):
            pass

    tool_calls = metadata.get("tool_calls")
    if isinstance(tool_calls, list):
        cleaned = [str(item).strip() for item in tool_calls if str(item).strip()]
        if cleaned:
            normalized["tool_calls"] = cleaned

    recalled = metadata.get("recalled_memory_ids")
    if isinstance(recalled, list):
        cleaned_ids = []
        for item in recalled:
            try:
                cleaned_ids.append(int(item))
            except (TypeError, ValueError):
                continue
        if cleaned_ids:
            normalized["recalled_memory_ids"] = cleaned_ids
    evidence = metadata.get("evidence")
    if isinstance(evidence, dict):
        normalized_evidence: Dict[str, Any] = {}
        grounded = evidence.get("grounded")
        if isinstance(grounded, bool):
            normalized_evidence["grounded"] = grounded
        evidence_tool_names = evidence.get("tool_names")
        if isinstance(evidence_tool_names, list):
            cleaned_tool_names = [
                str(item).strip() for item in evidence_tool_names if str(item).strip()
            ]
            if cleaned_tool_names:
                normalized_evidence["tool_names"] = cleaned_tool_names
        source_kinds = evidence.get("source_kinds")
        if isinstance(source_kinds, list):
            cleaned_source_kinds = [str(item).strip() for item in source_kinds if str(item).strip()]
            if cleaned_source_kinds:
                normalized_evidence["source_kinds"] = cleaned_source_kinds
        source_counts = evidence.get("source_counts")
        if isinstance(source_counts, dict):
            cleaned_counts = {}
            for key, value in source_counts.items():
                name = str(key).strip()
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                if name and count > 0:
                    cleaned_counts[name] = count
            if cleaned_counts:
                normalized_evidence["source_counts"] = cleaned_counts
        tool_details = evidence.get("tool_details")
        if isinstance(tool_details, list):
            cleaned_details = []
            for item in tool_details[:8]:
                if not isinstance(item, dict):
                    continue
                tool_name = str(item.get("tool_name") or "").strip()
                detail_kind = str(item.get("detail_kind") or "").strip()
                label = str(item.get("label") or "").strip()
                value = str(item.get("value") or "").strip()
                if not (tool_name and detail_kind and label and value):
                    continue
                cleaned_item = {
                    "tool_name": tool_name,
                    "detail_kind": detail_kind,
                    "label": label,
                    "value": value[:500],
                }
                source_title = str(item.get("source_title") or "").strip()
                if source_title:
                    cleaned_item["source_title"] = source_title[:200]
                source_kind = str(item.get("source_kind") or "").strip()
                if source_kind:
                    cleaned_item["source_kind"] = source_kind[:32]
                cleaned_details.append(cleaned_item)
            if cleaned_details:
                normalized_evidence["tool_details"] = cleaned_details
        image_artifacts = evidence.get("image_artifacts")
        if isinstance(image_artifacts, list):
            cleaned_images = []
            for item in image_artifacts[:6]:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or item.get("image_path") or "").strip()
                if not path:
                    continue
                cleaned_item: Dict[str, Any] = {"path": path[:500]}
                for source_key, target_key, max_len in (
                    ("title", "title", 160),
                    ("prompt", "prompt", 500),
                    ("workflow", "workflow", 80),
                    ("source_tool", "source_tool", 80),
                ):
                    value = str(item.get(source_key) or "").strip()
                    if value:
                        cleaned_item[target_key] = value[:max_len]
                for key in ("seed", "width", "height"):
                    value = item.get(key)
                    if value is None:
                        continue
                    try:
                        cleaned_item[key] = int(value)
                    except (TypeError, ValueError):
                        continue
                cleaned_images.append(cleaned_item)
            if cleaned_images:
                normalized_evidence["image_artifacts"] = cleaned_images
        citations = evidence.get("citations")
        if isinstance(citations, list):
            cleaned_citations = []
            for item in citations[:12]:
                if not isinstance(item, dict):
                    continue
                cleaned_item = {}
                for key, max_len in (
                    ("url", 500),
                    ("title", 200),
                    ("label", 200),
                    ("source", 120),
                    ("document", 300),
                    ("path", 500),
                ):
                    value = str(item.get(key) or "").strip()
                    if value:
                        cleaned_item[key] = value[:max_len]
                if cleaned_item:
                    cleaned_citations.append(cleaned_item)
            if cleaned_citations:
                normalized_evidence["citations"] = cleaned_citations
        if normalized_evidence:
            normalized["evidence"] = normalized_evidence

    status = str(metadata.get("task_draft_status") or "").strip().lower()
    if status == "created":
        status = "accepted"
    if "task_draft" in normalized:
        if status not in {"draft", "accepted", "denied"}:
            status = "accepted" if "created_task_id" in normalized else "draft"
        normalized["task_draft_status"] = status
    return normalized


def _evidence_kind_for_tool_name(tool_name: str) -> str | None:
    normalized = str(tool_name or "").strip()
    if not normalized:
        return None
    if normalized in _LIBRARY_EVIDENCE_TOOL_NAMES:
        return "library"
    if normalized in _WORKSPACE_CONTEXT_TOOL_NAMES:
        return "workspace"
    if normalized in _RSS_EVIDENCE_TOOL_NAMES:
        return "rss"
    if normalized in _WEB_EVIDENCE_TOOL_NAMES:
        return "web"
    if normalized in _IMAGE_EVIDENCE_TOOL_NAMES:
        return "image"
    return None


def _build_assistant_source_counts(
    executed_tools: List[Dict[str, Any]],
) -> Dict[str, int]:
    source_counts: Dict[str, int] = {}
    for record in executed_tools or []:
        if not isinstance(record, dict):
            continue
        tool_name = str(record.get("tool") or "").strip()
        kind = _evidence_kind_for_tool_name(tool_name)
        if kind is None:
            continue
        source_counts[kind] = source_counts.get(kind, 0) + 1
    return source_counts
def _build_assistant_evidence_metadata(
    executed_tools: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    tool_names = _summarize_executed_tool_names(executed_tools)
    if not tool_names:
        return None

    source_counts = _build_assistant_source_counts(executed_tools)

    evidence: Dict[str, Any] = {
        "grounded": True,
        "tool_names": tool_names,
    }
    if source_counts:
        evidence["source_kinds"] = list(source_counts.keys())
        evidence["source_counts"] = source_counts
    tool_details = _build_assistant_tool_details(executed_tools)
    if tool_details:
        evidence["tool_details"] = tool_details
    image_artifacts = _build_assistant_image_artifacts(executed_tools)
    if image_artifacts:
        evidence["image_artifacts"] = image_artifacts
    citations = _build_assistant_citations(executed_tools)
    if citations:
        evidence["citations"] = citations
    return evidence


def _build_assistant_citations(executed_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    citations: List[Dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for record in executed_tools or []:
        if not isinstance(record, dict) or not isinstance(record.get("citations"), list):
            continue
        for item in record["citations"]:
            if not isinstance(item, dict):
                continue
            cleaned = {
                str(key): value
                for key, value in item.items()
                if str(key) in {"url", "title", "label", "source", "document", "path"}
                and value not in (None, "")
            }
            key = tuple(sorted((name, str(value)) for name, value in cleaned.items()))
            if not cleaned or key in seen:
                continue
            seen.add(key)
            citations.append(cleaned)
            if len(citations) >= 12:
                return citations
    return citations


def _source_kind_for_url(url: str) -> str:
    """Cheap URL-only heuristic for per-source iconography — no extra fetch."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if host.endswith("wikipedia.org"):
        return "wiki"
    return "web"


def _extract_fetch_page_title(result_summary: str) -> str:
    """fetch_page prefixes its result with `Title: ...` when one was found."""
    first_line = (result_summary or "").split("\n", 1)[0]
    prefix = "Title: "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return ""


def _build_assistant_tool_details(executed_tools: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    details: List[Dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for record in executed_tools or []:
        if not isinstance(record, dict):
            continue
        tool_name = str(record.get("tool") or "").strip()
        arguments = record.get("arguments") or {}
        if not tool_name or not isinstance(arguments, dict):
            continue

        candidates: list[tuple[str, str, str]] = []
        if tool_name in {"web_search", "search_library", "search_my_feeds", "search_feeds"}:
            query = str(arguments.get("query") or "").strip()
            if query:
                candidates.append(("query", "Query", query))
        if tool_name == "fetch_page":
            url = str(arguments.get("url") or "").strip()
            if url:
                candidates.append(("url", "Page", url))
        if tool_name == "summarize_document":
            document_name = str(arguments.get("document_name") or "").strip()
            if document_name:
                candidates.append(("document", "Document", document_name))
        if tool_name == "describe_image":
            image_path = str(arguments.get("path") or "").strip()
            if image_path:
                candidates.append(("image", "Image", image_path))
            question = str(arguments.get("question") or "").strip()
            if question:
                candidates.append(("question", "Question", question))

        for detail_kind, label, value in candidates:
            key = (tool_name, detail_kind, value)
            if key in seen:
                continue
            seen.add(key)
            detail: Dict[str, str] = {
                "tool_name": tool_name,
                "detail_kind": detail_kind,
                "label": label,
                "value": value,
            }
            if tool_name == "fetch_page" and detail_kind == "url":
                detail["source_kind"] = _source_kind_for_url(value)
                structured = record.get("structured_content")
                source_title = (
                    str(structured.get("title") or "").strip()
                    if isinstance(structured, dict)
                    else ""
                ) or _extract_fetch_page_title(str(record.get("result_summary") or ""))
                if source_title:
                    detail["source_title"] = source_title
            details.append(detail)
            if len(details) >= 8:
                return details

    return details


def _build_assistant_image_artifacts(executed_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()

    for record in executed_tools or []:
        if not isinstance(record, dict):
            continue
        tool_name = str(record.get("tool") or "").strip()
        if tool_name not in _IMAGE_EVIDENCE_TOOL_NAMES:
            continue
        normalized_artifacts = record.get("artifacts")
        payload = (
            next(
                (
                    dict(item)
                    for item in normalized_artifacts
                    if isinstance(item, dict)
                    and str(item.get("path") or item.get("image_path") or "").strip()
                ),
                None,
            )
            if isinstance(normalized_artifacts, list)
            else None
        )
        if payload is None:
            payload = _parse_image_tool_result(record.get("result_summary"))
        if not payload:
            continue
        image_path = str(payload.get("image_path") or payload.get("path") or "").strip()
        if not image_path or image_path in seen_paths:
            continue
        seen_paths.add(image_path)

        item: Dict[str, Any] = {
            "path": image_path,
            "source_tool": tool_name,
        }
        prompt = str(payload.get("prompt") or (record.get("arguments") or {}).get("prompt") or "").strip()
        if prompt:
            item["prompt"] = prompt
            item["title"] = _image_artifact_title(prompt)
        workflow = str(payload.get("workflow") or (record.get("arguments") or {}).get("workflow") or "").strip()
        if workflow:
            item["workflow"] = workflow
        for key in ("seed", "width", "height"):
            value = payload.get(key)
            if value is None and isinstance(record.get("arguments"), dict):
                value = record["arguments"].get(key)
            if value is None:
                continue
            try:
                item[key] = int(value)
            except (TypeError, ValueError):
                continue
        artifacts.append(item)
        if len(artifacts) >= 6:
            break
    return artifacts


def _parse_image_tool_result(raw: Any) -> Dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or "image_path" not in text:
        return None
    for parser in (json.loads, ast.literal_eval):
        try:
            value = parser(text)
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    match = re.search(r"(\{.*?image_path.*?\})", text, flags=re.DOTALL)
    if match:
        snippet = match.group(1)
        for parser in (json.loads, ast.literal_eval):
            try:
                value = parser(snippet)
            except Exception:
                continue
            if isinstance(value, dict):
                return value
    return None


def _image_artifact_title(prompt: str) -> str:
    compact = " ".join(str(prompt or "").split())
    if len(compact) <= 72:
        return compact
    return compact[:69].rstrip() + "..."


def _markdown_image_reference_keys(content: str) -> set[str]:
    keys: set[str] = set()
    for match in re.finditer(r"!\[[^\]]*\]\(\s*<?([^\s)>]+)>?(?:\s+[^)]*)?\)", str(content or "")):
        target = unquote(str(match.group(1) or "").strip())
        parsed = urlparse(target)
        query_path = (parse_qs(parsed.query).get("path") or [""])[0]
        path = unquote(query_path or parsed.path or target).lstrip("./")
        if not path:
            continue
        keys.add(path)
        keys.add(Path(path).name)
    return keys


def _ensure_generated_image_markdown_references(
    content: str,
    executed_tools: List[Dict[str, Any]],
) -> str:
    """Guarantee generated images remain addressable by ordered chat renderers."""
    generated_records = [
        record
        for record in executed_tools or []
        if isinstance(record, dict) and str(record.get("tool") or "").strip() == "generate_image"
    ]
    artifacts = _build_assistant_image_artifacts(generated_records)
    if not artifacts:
        return content

    referenced = _markdown_image_reference_keys(content)
    missing: List[Dict[str, Any]] = []
    for artifact in artifacts:
        path = str(artifact.get("path") or "").strip()
        normalized_path = unquote(path).lstrip("./")
        if not path or normalized_path in referenced or Path(normalized_path).name in referenced:
            continue
        missing.append(artifact)
    if not missing:
        return content

    references = []
    for artifact in missing:
        path = str(artifact.get("path") or "").strip()
        alt = str(artifact.get("title") or artifact.get("prompt") or "Generated image")
        alt = " ".join(alt.replace("[", "").replace("]", "").split())[:120] or "Generated image"
        references.append(f"![{alt}]({path})")

    base = str(content or "").rstrip()
    separator = "\n\n" if base else ""
    joined_references = "\n\n".join(references)
    return f"{base}{separator}{joined_references}"


def _build_assistant_message_metadata(
    *,
    handoff_metadata: Dict[str, Any],
    executed_tools: List[Dict[str, Any]],
    recalled_memory_ids: List[int] | None = None,
) -> Dict[str, Any] | None:
    metadata: Dict[str, Any] = {}
    task_draft = handoff_metadata.get("task_draft")
    if isinstance(task_draft, dict) and task_draft:
        metadata["task_draft"] = task_draft
        metadata["task_draft_status"] = "draft"

    tool_calls = _summarize_executed_tool_names(executed_tools)
    if tool_calls:
        metadata["tool_calls"] = tool_calls
        evidence = _build_assistant_evidence_metadata(executed_tools)
        if evidence:
            metadata["evidence"] = evidence

    if recalled_memory_ids:
        metadata["recalled_memory_ids"] = [int(i) for i in recalled_memory_ids]

    normalized = _normalize_assistant_metadata_payload(metadata)
    return normalized or None


def _encode_assistant_message_metadata(metadata: Dict[str, Any]) -> str:
    payload = {"kind": _ASSISTANT_MESSAGE_METADATA_KIND, **_normalize_assistant_metadata_payload(metadata)}
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _assistant_message_metadata(message: ChatMessage) -> Dict[str, Any] | None:
    if str(getattr(message, "role", "") or "") != "assistant":
        return None
    payload = _decode_chat_json(getattr(message, "tool_results", None))
    if not isinstance(payload, dict):
        return None
    if str(payload.get("kind") or "") != _ASSISTANT_MESSAGE_METADATA_KIND:
        return None
    metadata = dict(payload)
    metadata.pop("kind", None)
    normalized = _normalize_assistant_metadata_payload(metadata)
    return normalized or None


def _session_message_payload(message: ChatMessage) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": int(message.id),
        "role": str(message.role or ""),
        "content": str(message.content or ""),
        "created_at": message.created_at,
    }
    metadata = _assistant_message_metadata(message)
    if metadata:
        payload["metadata"] = metadata
    return payload


def _estimate_chat_message_tokens(message: Dict[str, Any]) -> int:
    return _shared_estimate_message_tokens(message)


def _decode_chat_json(raw: Any) -> Any:
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def _tool_call_name_from_payload(call: Any) -> str:
    if isinstance(call, dict):
        function = call.get("function") or {}
        return str(function.get("name") or "").strip()
    function = getattr(call, "function", None)
    return str(getattr(function, "name", "") or "").strip()


def _tool_call_arguments_from_payload(call: Any) -> Dict[str, Any]:
    if isinstance(call, dict):
        raw = ((call.get("function") or {}).get("arguments"))
    else:
        raw = getattr(getattr(call, "function", None), "arguments", None)
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        decoded = json.loads(str(raw))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _normalize_workspace_relative_path(user_id: int, raw_path: str) -> str | None:
    candidate = str(raw_path or "").strip()
    if not candidate:
        return None
    try:
        workspace_root, path = resolve_workspace_path_for_user(user_id, candidate)
    except Exception:
        return None
    try:
        return str(path.relative_to(workspace_root))
    except Exception:
        return None


def _workspace_basename_matches(prompt: str, path: str) -> bool:
    lowered = str(prompt or "").strip().lower()
    if not lowered:
        return False
    stem = Path(path).name.strip().lower()
    if not stem:
        return False
    return stem in lowered or Path(stem).stem.lower() in lowered


def _collect_recent_workspace_artifact_context(
    messages: List[ChatMessage],
    *,
    user_id: int,
    limit: int = 80,
) -> RecentWorkspaceArtifactContext:
    relevant = list(messages[-max(1, limit):])
    tool_calls_by_id: dict[str, dict[str, Any]] = {}
    recent_paths: list[str] = []
    recent_actions: list[str] = []
    recent_document_names: list[str] = []
    recent_read_path: str | None = None
    recent_write_path: str | None = None

    for message in relevant:
        tool_calls = _decode_chat_json(getattr(message, "tool_calls", None))
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "").strip()
                if call_id:
                    tool_calls_by_id[call_id] = call

    for message in reversed(relevant):
        if str(getattr(message, "role", "") or "") != "tool":
            continue
        tool_payload = _decode_chat_json(getattr(message, "tool_results", None))
        if not isinstance(tool_payload, dict):
            continue
        tool_call_id = str(tool_payload.get("tool_call_id") or "").strip()
        if not tool_call_id:
            continue
        call = tool_calls_by_id.get(tool_call_id)
        if not isinstance(call, dict):
            continue
        tool_name = _tool_call_name_from_payload(call)
        if tool_name not in _WORKSPACE_CONTEXT_TOOL_NAMES:
            continue
        arguments = _tool_call_arguments_from_payload(call)
        normalized_path = _normalize_workspace_relative_path(user_id, str(arguments.get("path") or ""))
        if tool_name in _WORKSPACE_FILE_TOOL_NAMES and normalized_path:
            if normalized_path not in recent_paths:
                recent_paths.append(normalized_path)
            if tool_name == "read_file" and recent_read_path is None:
                recent_read_path = normalized_path
            if tool_name in {"write_file", "append_file"} and recent_write_path is None:
                recent_write_path = normalized_path
        if tool_name in {"read_file", "write_file", "append_file"} and normalized_path:
            recent_actions.append(f"{tool_name}:{normalized_path}")
        elif tool_name == "find_files":
            query = str(arguments.get("query") or "").strip()
            if query:
                recent_actions.append(f"find_files:{query}")
        elif tool_name == "list_directory":
            target = normalized_path or str(arguments.get("path") or ".").strip()
            if target:
                recent_actions.append(f"list_directory:{target}")

    for path in recent_paths:
        name = Path(path).name
        stem = Path(path).stem
        for value in (name, stem):
            lowered = value.strip().lower()
            if lowered and lowered not in recent_document_names:
                recent_document_names.append(lowered)

    active_path = recent_write_path or recent_read_path or (recent_paths[0] if recent_paths else None)
    return RecentWorkspaceArtifactContext(
        active_path=active_path,
        recent_read_path=recent_read_path,
        recent_write_path=recent_write_path,
        recent_paths=recent_paths[:6],
        recent_actions=recent_actions[:8],
        recent_document_names=recent_document_names[:8],
    )


def _is_recent_workspace_followup_prompt(
    prompt: str,
    context: RecentWorkspaceArtifactContext,
) -> bool:
    if not context.has_context:
        return False
    lowered = str(prompt or "").strip().lower()
    if not lowered:
        return False
    if any(marker in lowered for marker in ("library", "uploaded", "search my docs", "search the library")):
        return False
    if any(_workspace_basename_matches(lowered, path) for path in context.recent_paths):
        return True
    if any(name in lowered for name in context.recent_document_names):
        return True
    has_file_target = any(term in lowered for term in _WORKSPACE_EXPLICIT_HINTS) or any(
        token in lowered for token in ("file", "doc", "document", "report", "notes", "key points")
    )
    has_action = any(token in lowered for token in _WORKSPACE_ACTION_HINTS)
    return has_file_target and has_action


def _workspace_followup_system_note(
    context: RecentWorkspaceArtifactContext,
) -> str | None:
    if not context.has_context:
        return None
    lines = [
        "Recent workspace file context for this chat session:",
    ]
    if context.active_path:
        lines.append(f"- Active workspace file: {context.active_path}")
    if context.recent_write_path and context.recent_write_path != context.active_path:
        lines.append(f"- Most recent edited file: {context.recent_write_path}")
    if context.recent_read_path and context.recent_read_path not in {context.active_path, context.recent_write_path}:
        lines.append(f"- Most recent read file: {context.recent_read_path}")
    if context.recent_paths:
        lines.append(f"- Recent workspace paths: {', '.join(context.recent_paths[:4])}")
    lines.append(
        "- If the user refers to the file or document you were just working on, prefer workspace filesystem tools before any library search."
    )
    return "\n".join(lines)


def _apply_workspace_followup_grounding(
    history: List[Dict[str, Any]],
    *,
    user_prompt: str,
    context: RecentWorkspaceArtifactContext,
) -> List[Dict[str, Any]]:
    if not _is_recent_workspace_followup_prompt(user_prompt, context):
        return history
    note = _workspace_followup_system_note(context)
    if not note:
        return history
    grounded = list(history)
    if grounded and grounded[-1].get("role") == "user":
        return grounded[:-1] + [{"role": "system", "content": note}, grounded[-1]]
    return grounded + [{"role": "system", "content": note}]


async def _load_recent_workspace_context(
    session_id: int,
    *,
    user_id: int,
    db: AsyncSession,
    limit: int = 80,
) -> RecentWorkspaceArtifactContext:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
    )
    messages = list(reversed(list(result.scalars().all())))
    return _collect_recent_workspace_artifact_context(messages, user_id=user_id, limit=limit)


def _history_message_from_chat_message(message: ChatMessage) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "role": str(message.role or ""),
        "content": str(message.content or ""),
    }
    tool_calls = _decode_chat_json(getattr(message, "tool_calls", None))
    if isinstance(tool_calls, list) and tool_calls:
        payload["tool_calls"] = tool_calls
    tool_payload = _decode_chat_json(getattr(message, "tool_results", None))
    if isinstance(tool_payload, dict):
        tool_call_id = str(tool_payload.get("tool_call_id") or "").strip()
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
    return payload


def _is_hidden_runtime_message(message: ChatMessage) -> bool:
    if _chat_compaction_metadata(message):
        return True
    if str(getattr(message, "role", "") or "") == "tool":
        return True
    tool_calls = _decode_chat_json(getattr(message, "tool_calls", None))
    return str(getattr(message, "role", "") or "") == "assistant" and isinstance(tool_calls, list) and bool(tool_calls)


async def _persist_runtime_history_messages(
    session_id: int,
    runtime_messages: List[Dict[str, Any]],
    db: AsyncSession,
) -> int:
    rows: list[ChatMessage] = []
    for message in runtime_messages:
        role = str(message.get("role") or "").strip()
        if role == "assistant" and message.get("tool_calls"):
            rows.append(
                ChatMessage(
                    session_id=session_id,
                    role="assistant",
                    content=str(message.get("content") or ""),
                    tool_calls=json.dumps(message.get("tool_calls") or [], ensure_ascii=True, sort_keys=True),
                )
            )
        elif role == "tool":
            tool_payload = {
                "tool_call_id": str(message.get("tool_call_id") or "").strip(),
            }
            rows.append(
                ChatMessage(
                    session_id=session_id,
                    role="tool",
                    content=str(message.get("content") or ""),
                    tool_results=json.dumps(tool_payload, ensure_ascii=True, sort_keys=True),
                )
            )
    if rows:
        db.add_all(rows)
    return len(rows)


def _build_runtime_history_flush_helpers(
    *,
    session_id: int,
    db: AsyncSession,
    get_runtime_history: Callable[[], List[Dict[str, Any]]],
) -> tuple[
    Callable[[List[Dict[str, Any]]], Awaitable[None]],
    Callable[[], Awaitable[List[Dict[str, Any]]]],
    Callable[[], int],
]:
    consumed_runtime_message_count = 0

    async def _flush_runtime_messages(new_messages: List[Dict[str, Any]]) -> None:
        nonlocal consumed_runtime_message_count
        rows_written = await _persist_runtime_history_messages(session_id, new_messages, db)
        consumed_runtime_message_count += len(new_messages)
        if rows_written:
            await db.commit()

    async def _flush_pending_runtime_history() -> List[Dict[str, Any]]:
        nonlocal consumed_runtime_message_count
        runtime_history_messages = get_runtime_history()
        pending = runtime_history_messages[consumed_runtime_message_count:]
        rows_written = await _persist_runtime_history_messages(session_id, pending, db)
        consumed_runtime_message_count += len(pending)
        if rows_written:
            await db.commit()
        return runtime_history_messages

    def _get_consumed_runtime_message_count() -> int:
        return consumed_runtime_message_count

    return _flush_runtime_messages, _flush_pending_runtime_history, _get_consumed_runtime_message_count


def _is_local_chat_model(model: str | None) -> bool:
    selected = str(model or "").strip()
    return selected.startswith(("ollama/", "ollama_chat/"))


def _runtime_history_has_completed_tool_turn(runtime_messages: List[Dict[str, Any]]) -> bool:
    saw_assistant_tool_call = any(
        str(message.get("role") or "") == "assistant" and bool(message.get("tool_calls"))
        for message in runtime_messages
    )
    saw_tool_result = any(str(message.get("role") or "") == "tool" for message in runtime_messages)
    return saw_assistant_tool_call and saw_tool_result


def _runtime_history_tool_names(runtime_messages: List[Dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for message in runtime_messages:
        for call in message.get("tool_calls") or []:
            name = _tool_call_name_from_payload(call)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _build_local_post_tool_synthesis_recovery_message(runtime_messages: List[Dict[str, Any]]) -> str:
    tool_names = _runtime_history_tool_names(runtime_messages)
    if "summarize_document" in tool_names:
        return (
            "I was able to complete the document-summary tool step, but the local model failed while turning that "
            "saved summary into a final answer. The tool evidence is still in this chat now. Ask me to retry from "
            "the saved summary or narrow the exact section you want."
        )
    return (
        "I was able to complete the tool step for that request, but the local model failed while turning the saved "
        "tool result into a final answer. The tool evidence is still in this chat now. Ask me to retry from the "
        "saved result or narrow the request."
    )


def _estimate_chat_history_tokens(history: List[Dict[str, Any]]) -> int:
    return sum(_estimate_chat_message_tokens(message) for message in history)


def _chat_recap_summaries(prefix: List[Dict[str, Any]]) -> list[str]:
    return recap_summaries(prefix, head_count=3, tail_count=9, max_lines=12)


def _build_chat_compaction_boundary(
    prefix: List[Dict[str, Any]],
    *,
    continuity: Dict[str, Any] | None = None,
    carried_recap: List[str] | None = None,
) -> Dict[str, Any]:
    payload = build_boundary_payload(
        mode="chat",
        recap=_chat_recap_summaries(prefix),
        carried_recap=carried_recap,
        continuity=continuity,
    )
    return _shared_boundary_message(payload)


def _chat_compaction_metadata(message: ChatMessage) -> Dict[str, Any] | None:
    if str(getattr(message, "role", "") or "") != "system":
        return None
    payload = _decode_chat_json(getattr(message, "tool_results", None))
    if not isinstance(payload, dict):
        return None
    if str(payload.get("kind") or "") != _CHAT_COMPACTION_MARKER_KIND:
        return None
    return payload


def _serialize_chat_compaction_event(message: ChatMessage) -> Dict[str, Any] | None:
    payload = _chat_compaction_metadata(message)
    if not isinstance(payload, dict):
        return None
    return {
        "id": int(message.id),
        "kind": _CHAT_COMPACTION_MARKER_KIND,
        "content": message.content,
        "created_at": message.created_at,
        "compacted_until_message_id": payload.get("compacted_until_message_id"),
        "compacted_message_count": payload.get("compacted_message_count"),
        "estimated_tokens_before": payload.get("estimated_tokens_before"),
        "estimated_tokens_after": payload.get("estimated_tokens_after"),
        "recent_messages_kept": payload.get("recent_messages_kept"),
        "continuity": payload.get("continuity") or {},
    }


def _build_chat_continuity_metadata(prefix_messages: List[ChatMessage], *, user_id: int) -> Dict[str, Any]:
    if not prefix_messages:
        return {}
    context = _collect_recent_workspace_artifact_context(
        prefix_messages,
        user_id=user_id,
        limit=len(prefix_messages),
    )
    active_document = ""
    pending_objective = ""
    for message in reversed(prefix_messages):
        role = str(getattr(message, "role", "") or "")
        content = " ".join(str(getattr(message, "content", "") or "").split()).strip()
        if not content:
            continue
        if not pending_objective and role == "user":
            pending_objective = content[:220]
        if not active_document and role in {"user", "assistant"}:
            match = re.search(r"\b([A-Za-z0-9_. -]+\.(?:pdf|md|txt|docx?|csv|json))\b", content, flags=re.IGNORECASE)
            if match:
                active_document = match.group(1).strip()
        if pending_objective and active_document:
            break
    return {
        "active_workspace_file": context.active_path or "",
        "active_document": active_document,
        "pending_objective": pending_objective,
        "recent_actions": context.recent_actions[:4],
    }


async def _persist_chat_compaction_marker(
    session_id: int,
    *,
    user_id: int,
    prefix_messages: List[ChatMessage],
    continuity_messages: List[ChatMessage] | None = None,
    recent_messages_kept: int,
    estimated_tokens_before: int,
    estimated_tokens_after: int,
    db: AsyncSession,
) -> ChatMessage | None:
    if not prefix_messages:
        return None

    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id, ChatMessage.role == "system")
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )
    system_rows = list(result.scalars().all())
    compaction_rows = [row for row in system_rows if _chat_compaction_metadata(row)]
    marker = compaction_rows[0] if compaction_rows else None
    stale_rows = compaction_rows[1:] if len(compaction_rows) > 1 else []

    carried_recap: list[str] = []
    if marker is not None:
        prior_payload = _chat_compaction_metadata(marker) or {}
        prior_carried = [
            str(item).strip()
            for item in (prior_payload.get("carried_recap") or [])
            if str(item).strip()
        ]
        prior_recap = _marker_recap_lines(prior_payload, str(marker.content or ""))
        carried_recap = _condense_carried_recap(prior_carried + prior_recap)

    prefix_payloads = [_history_message_from_chat_message(message) for message in prefix_messages]
    continuity = _build_chat_continuity_metadata(continuity_messages or prefix_messages, user_id=user_id)
    boundary_payload = build_boundary_payload(
        mode="chat",
        recap=_chat_recap_summaries(prefix_payloads),
        carried_recap=carried_recap,
        continuity=continuity,
    )
    boundary_content = render_boundary_text(boundary_payload)
    metadata = {
        **boundary_payload,
        "compacted_until_message_id": int(prefix_messages[-1].id),
        "compacted_message_count": len(prefix_messages),
        "estimated_tokens_before": estimated_tokens_before,
        "estimated_tokens_after": estimated_tokens_after,
        "recent_messages_kept": recent_messages_kept,
    }

    if marker is None:
        marker = ChatMessage(
            session_id=session_id,
            role="system",
            content=boundary_content,
            tool_results=json.dumps(metadata, ensure_ascii=True, sort_keys=True),
        )
        db.add(marker)
    else:
        marker.content = boundary_content
        marker.tool_results = json.dumps(metadata, ensure_ascii=True, sort_keys=True)
    for stale in stale_rows:
        await db.delete(stale)
    await db.flush()
    return marker


def _snap_chat_cut_to_tool_chain(messages: List[ChatMessage], cut_index: int) -> int:
    """Move a prefix/suffix cut backwards so a suffix never starts with tool
    results whose assistant tool-call message landed in the prefix."""
    while 0 < cut_index < len(messages) and str(getattr(messages[cut_index], "role", "") or "") == "tool":
        cut_index -= 1
    return cut_index


async def _load_history(session_id: int, db: AsyncSession) -> List[Dict[str, Any]]:
    """Load conversation history while preserving persisted tool metadata."""
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    messages = list(result.scalars().all())
    compaction_markers = [message for message in messages if _chat_compaction_metadata(message)]
    latest_marker = compaction_markers[-1] if compaction_markers else None
    compacted_until_id = 0
    if latest_marker is not None:
        marker_payload = _chat_compaction_metadata(latest_marker) or {}
        compacted_until_id = int(marker_payload.get("compacted_until_message_id") or 0)

    raw_messages = [
        message
        for message in messages
        if not _chat_compaction_metadata(message) and int(message.id or 0) > compacted_until_id
    ]
    raw_history = [_history_message_from_chat_message(m) for m in raw_messages]
    estimated_tokens_before = _estimate_chat_history_tokens(raw_history)
    if estimated_tokens_before <= int(settings.chat_history_soft_token_limit):
        if latest_marker is not None:
            return [{"role": "system", "content": latest_marker.content}] + raw_history
        return raw_history

    keep_recent = max(1, int(settings.chat_recent_messages_keep))
    cut = _snap_chat_cut_to_tool_chain(raw_messages, max(0, len(raw_messages) - keep_recent))
    prefix_messages = raw_messages[:cut]
    suffix_messages = raw_messages[cut:]
    if not prefix_messages:
        if latest_marker is not None:
            return [{"role": "system", "content": latest_marker.content}] + raw_history
        return raw_history

    projected_suffix = [_history_message_from_chat_message(m) for m in suffix_messages]
    estimated_tokens_after = _estimate_chat_history_tokens(projected_suffix)
    session_row = await db.get(ChatSession, session_id)
    prefix_until_id = int(prefix_messages[-1].id)
    continuity_messages = [
        message
        for message in messages
        if int(message.id or 0) > compacted_until_id and int(message.id or 0) <= prefix_until_id
    ]
    marker = await _persist_chat_compaction_marker(
        session_id,
        user_id=int(getattr(session_row, "user_id", 0) or 0),
        prefix_messages=prefix_messages,
        continuity_messages=continuity_messages,
        recent_messages_kept=keep_recent,
        estimated_tokens_before=estimated_tokens_before,
        estimated_tokens_after=estimated_tokens_after,
        db=db,
    )
    marker_content = marker.content if marker is not None else _build_chat_compaction_boundary(
        [_history_message_from_chat_message(m) for m in prefix_messages]
    )["content"]
    return [{"role": "system", "content": marker_content}] + projected_suffix


def _apply_tool_overrides(
    user_context: UserContext,
    *,
    allowed_tools: Optional[List[str]],
    blocked_tools: Optional[List[str]],
) -> None:
    """
    Testing override hook for chat sessions.
    Merges persona-level blocked tools with optional per-message allow/block lists.
    """
    from app.agent.tools import TOOL_SCHEMAS
    from app.mcp.registry import get_mcp_registry

    base_blocked = set(user_context.blocked_tools or [])
    all_tools = {tool["function"]["name"] for tool in TOOL_SCHEMAS}

    registry = get_mcp_registry()
    if registry._is_ready:
        all_tools.update(
            tool["function"]["name"]
            for tool in registry.get_tools_for_agent()
        )

    normalized_allowed = {
        str(name).strip() for name in (allowed_tools or [])
        if str(name).strip()
    }
    normalized_blocked = {
        str(name).strip() for name in (blocked_tools or [])
        if str(name).strip()
    }

    if normalized_allowed:
        valid_allowed = normalized_allowed.intersection(all_tools)
        if valid_allowed:
            base_blocked.update(all_tools - valid_allowed)
    base_blocked.update(normalized_blocked.intersection(all_tools))

    user_context.blocked_tools = sorted(base_blocked)
