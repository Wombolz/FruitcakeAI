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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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
from app.agent.chat_routing import classify_chat_complexity
from app.agent.chat_validation import (
    build_chat_retry_instruction,
    should_validate_chat_response,
    validate_chat_response,
)
from app.agent.core import (
    get_task_handoff_payload,
    reset_task_handoff_payload,
    restore_task_handoff_payload,
    run_agent,
    stream_agent,
)
from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import ChatMessage, ChatSession, User
from app.db.session import get_db
from app.chat_runtime import get_chat_run_manager
from app.llm_registry import available_llm_models, is_configured_model
from app.llm_usage import bind_llm_usage_context, reset_llm_usage_context
from app.metrics import metrics
from app.memory.service import get_memory_service
from app.skills.service import hydrate_user_context
from app.agent.tools import (
    normalize_document_name_query,
    resolve_document_name,
    get_tool_execution_records,
    reset_tool_execution_records,
    restore_tool_execution_records,
)

log = structlog.get_logger(__name__)

router = APIRouter()
_CHAT_ESTIMATED_CHARS_PER_TOKEN = 4
_CHAT_COMPACTION_MARKER_KIND = "history_compaction"


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class SendMessageRequest(BaseModel):
    content: str
    client_send_id: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    blocked_tools: Optional[List[str]] = None


class StopChatResponse(BaseModel):
    stopped: bool
    session_id: int


class ChatSessionStatusResponse(BaseModel):
    session_id: int
    active: bool


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


class SessionOut(BaseModel):
    id: int
    title: Optional[str]
    persona: str
    llm_model: Optional[str]
    sort_order: Optional[int]

    class Config:
        from_attributes = True


@dataclass
class ChatSocketPayload:
    raw: Dict[str, Any]
    content: str
    client_send_id: Optional[str]
    allowed_tools: Optional[List[str]]
    blocked_tools: Optional[List[str]]


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
    session = ChatSession(
        user_id=current_user.id,
        title=body.title or "New conversation",
        persona=current_user.persona or "family_assistant",
        llm_model=settings.llm_model,
        sort_order=0,
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
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at,
            }
            for m in messages
            if not _chat_compaction_metadata(m)
        ],
    }


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
    stage_started = time.perf_counter()
    history, _memory_ids = await _apply_memory_context(
        history,
        db,
        current_user.id,
        body.content,
    )
    _record_chat_stage_timing(stage_timings_ms, "memory_context", stage_started)
    library_list_intent = is_library_lookup_intent(body.content)
    library_summary_intent = is_library_summary_intent(body.content)
    library_detail_intent = is_library_detail_or_excerpt_intent(body.content)
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
    should_validate = should_validate_chat_response(
        user_prompt=body.content,
        effective_complex=effective_complex,
    )
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
    handoff_metadata: Dict[str, Any] = {}
    chat_run_manager = get_chat_run_manager()
    current_task = asyncio.current_task()
    try:
        if current_task is not None:
            await chat_run_manager.register(session_id, current_task)
        record_token = reset_tool_execution_records()
        handoff_token = reset_task_handoff_payload()
        usage_token = bind_llm_usage_context(
            user_id=current_user.id,
            session_id=session_id,
            source="chat_rest",
        )
        stage_started = time.perf_counter()
        reply = await _execute_chat_turn(
            execution_history,
            user_context,
            user_prompt=body.content,
            mode=execution_mode,
            model_override=session.llm_model,
            stage="chat_complex" if effective_complex else "chat_simple",
            enable_validation=should_validate,
        )
        _record_chat_stage_timing(stage_timings_ms, "model_execution", stage_started)
        reply = _enforce_calendar_mutation_integrity(
            body.content,
            reply,
            get_tool_execution_records(),
        )
        handoff_metadata = get_task_handoff_payload() or {}
    except asyncio.CancelledError:
        log.info("Chat REST run stopped", session_id=session_id, user_id=current_user.id)
        return JSONResponse(status_code=409, content={"detail": "Chat stopped by user"})
    except Exception as e:
        log.exception("Agent error in REST handler", session_id=session_id)
        raise HTTPException(status_code=500, detail="Agent error — check server logs for details")
    finally:
        if prompt_claimed:
            await chat_run_manager.mark_prompt_finished(session_id, body.content)
        if current_task is not None:
            await chat_run_manager.clear(session_id, current_task)
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

    # Store assistant reply
    assistant_msg = ChatMessage(session_id=session_id, role="assistant", content=reply)
    db.add(assistant_msg)
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
        "session_id": session_id,
        "metadata": {
            "active_skills": list(user_context.active_skill_slugs or []),
            "skill_selection_mode": user_context.skill_selection_mode or "",
            **handoff_metadata,
        },
    }


@router.post("/sessions/{session_id}/stop", response_model=StopChatResponse)
async def stop_chat_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StopChatResponse:
    await _get_session_or_404(session_id, current_user.id, db)
    stopped = await get_chat_run_manager().request_stop(session_id)
    return StopChatResponse(stopped=stopped, session_id=session_id)


@router.get("/sessions/{session_id}/status", response_model=ChatSessionStatusResponse)
async def get_chat_session_status(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSessionStatusResponse:
    await _get_session_or_404(session_id, current_user.id, db)
    active = await get_chat_run_manager().is_active(session_id)
    return ChatSessionStatusResponse(session_id=session_id, active=active)


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
        stage_started = time.perf_counter()
        history, _memory_ids = await _apply_memory_context(
            history,
            db,
            current_user.id,
            user_message,
        )
        _record_chat_stage_timing(stage_timings_ms, "memory_context", stage_started)
        library_list_intent = is_library_lookup_intent(user_message)
        library_summary_intent = is_library_summary_intent(user_message)
        library_detail_intent = is_library_detail_or_excerpt_intent(user_message)
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
        )
        _record_chat_stage_timing(stage_timings_ms, "library_grounding", stage_started)
        full_response: List[str] = []
        decision = classify_chat_complexity(
            user_message,
            threshold=settings.chat_complexity_threshold,
            routing_enabled=settings.chat_complexity_routing_enabled,
        )
        execution_mode, effective_complex = _resolve_chat_execution(
            auto_complex=(decision.is_complex or library_intent),
            preference=getattr(current_user, "chat_routing_preference", None),
        )
        should_validate = should_validate_chat_response(
            user_prompt=user_message,
            effective_complex=effective_complex,
        )
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

        record_token = reset_tool_execution_records()
        handoff_token = reset_task_handoff_payload()
        handoff_metadata: Dict[str, Any] = {}
        usage_token = bind_llm_usage_context(
            user_id=current_user.id,
            session_id=session_id,
            source="chat_websocket",
        )
        if should_validate:
            stage_started = time.perf_counter()
            complete = await _execute_chat_turn(
                execution_history,
                user_context,
                user_prompt=user_message,
                mode=execution_mode,
                model_override=session.llm_model,
                stage="chat_complex" if effective_complex else "chat_simple",
                enable_validation=True,
            )
            _record_chat_stage_timing(stage_timings_ms, "model_execution", stage_started)
            complete = _enforce_calendar_mutation_integrity(
                user_message,
                complete,
                get_tool_execution_records(),
            )
            for token_chunk in _chunk_text(complete):
                full_response.append(token_chunk)
                await _send_json_if_open({"type": "token", "content": token_chunk})
            complete = "".join(full_response)
        else:
            started = time.perf_counter()
            async for token_chunk in stream_agent(
                execution_history,
                user_context,
                mode=execution_mode,
                model_override=session.llm_model,
                stage="chat_simple",
            ):
                full_response.append(token_chunk)
                await _send_json_if_open({"type": "token", "content": token_chunk})
            metrics.record_chat_latency(
                mode="chat",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
            _record_chat_stage_timing(stage_timings_ms, "model_execution", started)
            complete = _enforce_calendar_mutation_integrity(
                user_message,
                "".join(full_response),
                get_tool_execution_records(),
            )
        handoff_metadata = get_task_handoff_payload() or {}
        assistant_msg = ChatMessage(
            session_id=session_id, role="assistant", content=complete
        )
        db.add(assistant_msg)
        await db.commit()
        _log_chat_latency_breakdown(
            session_id=session_id,
            mode=execution_mode,
            total_started=message_started,
            stage_timings_ms=stage_timings_ms,
            transport="websocket",
        )

        await _send_json_if_open(
            {
                "type": "done",
                "content": complete,
                "metadata": {
                    "active_skills": list(user_context.active_skill_slugs or []),
                    "skill_selection_mode": user_context.skill_selection_mode or "",
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
        )
    except asyncio.CancelledError:
        await db.rollback()
        log.info(
            "chat.websocket_message_stopped",
            session_id=session_id,
            user_id=current_user.id,
            websocket_id=hex(id(websocket)),
            client_send_id=client_send_id or "",
        )
        raise
    except Exception:
        await db.rollback()
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

    Server sends:  {"type": "token",   "content": "..."}  (one per chunk)
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
            return ChatSocketPayload(
                raw=message_data,
                content=content,
                client_send_id=client_send_id,
                allowed_tools=allowed_tools,
                blocked_tools=blocked_tools,
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
                    except asyncio.CancelledError:
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


def _normalize_chat_routing_preference(value: str | None) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"auto", "fast", "deep"}:
        return lowered
    return "auto"


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
) -> str:
    started = time.perf_counter()
    reply = await run_agent(
        history,
        user_context,
        mode=mode,
        model_override=model_override,
        stage=stage,
    )
    if not (enable_validation and settings.chat_validation_enabled):
        metrics.record_chat_latency(
            mode=mode,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        return reply

    max_attempts = max(0, int(settings.chat_validation_retry_max_attempts))
    attempts = 0
    current = reply

    while True:
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
    grounding_note = (
        "Required grounding for this turn: this is a library intent. "
        "Prioritize the newest user message over prior context. "
        "Use only the tool output below as source of truth for document names/metadata. "
        "If output is empty, explicitly say no documents/excerpts were found.\n\n"
        f"{tool_name} result:\n{result}"
    )

    grounded = list(history)
    if grounded and grounded[-1].get("role") == "user":
        return grounded[:-1] + [{"role": "system", "content": grounding_note}, grounded[-1]]
    return grounded + [{"role": "system", "content": grounding_note}]


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
    memories = await svc.retrieve_for_context(db, user_id, query=user_prompt)
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


def _estimate_chat_message_tokens(message: Dict[str, Any]) -> int:
    total = max(1, len(str(message.get("content") or "")) // _CHAT_ESTIMATED_CHARS_PER_TOKEN) if message.get("content") else 0
    tool_calls = message.get("tool_calls") or []
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            total += max(1, len(str(function.get("name") or "")) // _CHAT_ESTIMATED_CHARS_PER_TOKEN) if function.get("name") else 0
            total += max(1, len(str(function.get("arguments") or "")) // _CHAT_ESTIMATED_CHARS_PER_TOKEN) if function.get("arguments") else 0
    return total


def _decode_chat_json(raw: Any) -> Any:
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def _estimate_chat_history_tokens(history: List[Dict[str, Any]]) -> int:
    return sum(_estimate_chat_message_tokens(message) for message in history)


def _compact_chat_message_summary(message: Dict[str, Any]) -> str:
    role = str(message.get("role") or "").strip() or "unknown"
    content = " ".join(str(message.get("content") or "").split()).strip()
    if len(content) > 160:
        content = content[:159].rstrip() + "…"
    if role == "assistant" and message.get("tool_calls"):
        tool_names = []
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = str(function.get("name") or "").strip()
            if name:
                tool_names.append(name)
        if tool_names:
            return f"Assistant requested tools: {', '.join(tool_names[:5])}"
    return f"{role.capitalize()}: {content}" if content else f"{role.capitalize()}:"


def _build_chat_compaction_boundary(prefix: List[Dict[str, Any]]) -> Dict[str, Any]:
    lines = [
        "Earlier chat history was compacted to keep the live context focused.",
        "Use this as a compact recap unless newer turns contradict it:",
    ]
    summaries: list[str] = []
    for message in prefix:
        summary = _compact_chat_message_summary(message)
        if summary:
            summaries.append(f"- {summary}")
        if len(summaries) >= 12:
            break
    if not summaries:
        summaries.append("- Earlier user and assistant turns were compacted.")
    lines.extend(summaries)
    return {"role": "system", "content": "\n".join(lines)}


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
    }


async def _persist_chat_compaction_marker(
    session_id: int,
    *,
    prefix_messages: List[ChatMessage],
    recent_messages_kept: int,
    estimated_tokens_before: int,
    estimated_tokens_after: int,
    db: AsyncSession,
) -> ChatMessage | None:
    if not prefix_messages:
        return None

    boundary = _build_chat_compaction_boundary(
        [{"role": str(message.role), "content": str(message.content)} for message in prefix_messages]
    )
    compacted_until_message_id = int(prefix_messages[-1].id)
    metadata = {
        "kind": _CHAT_COMPACTION_MARKER_KIND,
        "compacted_until_message_id": compacted_until_message_id,
        "compacted_message_count": len(prefix_messages),
        "estimated_tokens_before": estimated_tokens_before,
        "estimated_tokens_after": estimated_tokens_after,
        "recent_messages_kept": recent_messages_kept,
    }

    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id, ChatMessage.role == "system")
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )
    system_rows = list(result.scalars().all())
    compaction_rows = [row for row in system_rows if _chat_compaction_metadata(row)]
    marker = compaction_rows[0] if compaction_rows else None
    stale_rows = compaction_rows[1:] if len(compaction_rows) > 1 else []

    if marker is None:
        marker = ChatMessage(
            session_id=session_id,
            role="system",
            content=boundary["content"],
            tool_results=json.dumps(metadata, ensure_ascii=True, sort_keys=True),
        )
        db.add(marker)
    else:
        marker.content = boundary["content"]
        marker.tool_results = json.dumps(metadata, ensure_ascii=True, sort_keys=True)
    for stale in stale_rows:
        await db.delete(stale)
    await db.flush()
    return marker


def _project_chat_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not history:
        return []

    estimated_tokens = _estimate_chat_history_tokens(history)
    if estimated_tokens <= int(settings.chat_history_soft_token_limit):
        return history

    keep_recent = max(1, int(settings.chat_recent_messages_keep))
    prefix = history[:-keep_recent]
    suffix = history[-keep_recent:]
    if not prefix:
        return history
    return [_build_chat_compaction_boundary(prefix)] + suffix


async def _load_history(session_id: int, db: AsyncSession) -> List[Dict[str, Any]]:
    """Load conversation history as a list of {role, content} dicts."""
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
    raw_history = [{"role": m.role, "content": m.content} for m in raw_messages]
    estimated_tokens_before = _estimate_chat_history_tokens(raw_history)
    if estimated_tokens_before <= int(settings.chat_history_soft_token_limit):
        if latest_marker is not None:
            return [{"role": "system", "content": latest_marker.content}] + raw_history
        return raw_history

    keep_recent = max(1, int(settings.chat_recent_messages_keep))
    prefix_messages = raw_messages[:-keep_recent]
    suffix_messages = raw_messages[-keep_recent:]
    if not prefix_messages:
        if latest_marker is not None:
            return [{"role": "system", "content": latest_marker.content}] + raw_history
        return raw_history

    projected_suffix = [{"role": m.role, "content": m.content} for m in suffix_messages]
    estimated_tokens_after = _estimate_chat_history_tokens(projected_suffix)
    marker = await _persist_chat_compaction_marker(
        session_id,
        prefix_messages=prefix_messages,
        recent_messages_kept=keep_recent,
        estimated_tokens_before=estimated_tokens_before,
        estimated_tokens_after=estimated_tokens_after,
        db=db,
    )
    marker_content = marker.content if marker is not None else _build_chat_compaction_boundary(
        [{"role": m.role, "content": m.content} for m in prefix_messages]
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
