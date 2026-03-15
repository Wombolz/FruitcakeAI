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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import structlog

from app.agent.context import UserContext
from app.agent.chat_orchestration import build_orchestrated_chat_history
from app.agent.chat_routing import classify_chat_complexity
from app.agent.chat_validation import (
    build_chat_retry_instruction,
    validate_chat_response,
)
from app.agent.core import run_agent, stream_agent
from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import ChatMessage, ChatSession, User
from app.db.session import get_db
from app.metrics import metrics

log = structlog.get_logger(__name__)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class SendMessageRequest(BaseModel):
    content: str
    allowed_tools: Optional[List[str]] = None
    blocked_tools: Optional[List[str]] = None


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class UpdateSessionPersonaRequest(BaseModel):
    persona: str = Field(min_length=1, max_length=100)


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

    class Config:
        from_attributes = True


# ── GET /chat/personas ────────────────────────────────────────────────────────

@router.get("/personas")
async def list_personas() -> Dict[str, Any]:
    """Return all available personas and their descriptions."""
    from app.agent.persona_loader import list_personas as _list
    personas = _list()
    return {
        name: {
            "description": cfg.get("description", ""),
            "tone": cfg.get("tone", ""),
            "blocked_tools": cfg.get("blocked_tools", []),
            "content_filter": cfg.get("content_filter", ""),
        }
        for name, cfg in personas.items()
    }


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
    )
    db.add(session)
    await db.flush()
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
        .order_by(ChatSession.updated_at.desc())
    )
    return result.scalars().all()


# ── GET /chat/sessions/{id} ───────────────────────────────────────────────────

@router.get("/sessions/{session_id}")
async def get_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    session = await _get_session_or_404(session_id, current_user.id, db)

    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    messages = result.scalars().all()

    return {
        "id": session.id,
        "title": session.title,
        "persona": session.persona,
        "llm_model": session.llm_model,
        "messages": [{"id": m.id, "role": m.role, "content": m.content} for m in messages],
    }


# ── PATCH /chat/sessions/{id} ────────────────────────────────────────────────

@router.patch("/sessions/{session_id}", response_model=SessionOut)
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

@router.patch("/sessions/{session_id}/persona", response_model=SessionOut)
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


# ── DELETE /chat/sessions/{id} ───────────────────────────────────────────────

@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
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
    session = await _get_session_or_404(session_id, current_user.id, db)

    # Handle /persona command before touching history or running the agent
    persona_name = _parse_persona_command(body.content)
    if persona_name is not None:
        return await _switch_persona(session_id, persona_name, session, db)

    # Load conversation history
    history = await _load_history(session_id, db)

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
    user_context.session_id = session_id

    decision = classify_chat_complexity(
        body.content,
        threshold=settings.chat_complexity_threshold,
        routing_enabled=settings.chat_complexity_routing_enabled,
    )
    execution_mode = "chat_orchestrated" if decision.is_complex else "chat"
    execution_history = build_orchestrated_chat_history(
        history,
        enabled=decision.is_complex and settings.chat_orchestration_enabled,
        max_steps=settings.chat_orchestration_max_steps,
    )
    if decision.is_complex:
        metrics.inc_chat_complexity_complex_count()
        metrics.inc_chat_complexity_routed_complex_count()
    else:
        metrics.inc_chat_complexity_simple_count()

    try:
        reply = await _execute_chat_turn(
            execution_history,
            user_context,
            user_prompt=body.content,
            mode=execution_mode,
            stage="chat_complex" if decision.is_complex else "chat_simple",
            enable_validation=decision.is_complex,
        )
    except Exception as e:
        log.exception("Agent error in REST handler", session_id=session_id)
        raise HTTPException(status_code=500, detail="Agent error — check server logs for details")

    # Store assistant reply
    assistant_msg = ChatMessage(session_id=session_id, role="assistant", content=reply)
    db.add(assistant_msg)
    await db.commit()

    return {"role": "assistant", "content": reply, "session_id": session_id}


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
        raw = await websocket.receive_text()
        data = json.loads(raw)
        user_message = data.get("content", "").strip()
        allowed_tools = data.get("allowed_tools") if isinstance(data, dict) else None
        blocked_tools = data.get("blocked_tools") if isinstance(data, dict) else None

        # ── Auth path 2: token in first message body (backward compat) ──
        if current_user is None:
            token = data.get("token", "").removeprefix("Bearer ").strip()
            if not token:
                await websocket.send_json({"type": "error", "content": "token and content required"})
                await websocket.close()
                return
            payload = decode_token(token)
            user_id = int(payload["sub"])
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

        # ── Message loop — keep connection alive for the session lifetime ────
        # user_message is already set from the first read above.
        # WebSocketDisconnect raised by receive_text() exits the loop cleanly.
        while True:
            if not user_message:
                await websocket.send_json({"type": "error", "content": "content required"})
            else:
                # Handle /persona command
                persona_name = _parse_persona_command(user_message)
                if persona_name is not None:
                    resp = await _switch_persona(session_id, persona_name, session, db)
                    await websocket.send_json({
                        "type": "persona",
                        "content": resp["content"],
                        "persona": resp.get("persona_switched", persona_name),
                    })
                    await websocket.send_json({"type": "done", "content": resp["content"]})
                    # Reload session so subsequent messages see the new persona
                    sr = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
                    session = sr.scalar_one_or_none() or session
                else:
                    # Store user message
                    user_msg = ChatMessage(session_id=session_id, role="user", content=user_message)
                    db.add(user_msg)
                    await db.flush()

                    # Load history and append new user message
                    history = await _load_history(session_id, db)

                    # Build context from session's current persona
                    user_context = UserContext.from_user(current_user, persona_name=session.persona)
                    _apply_tool_overrides(
                        user_context,
                        allowed_tools=allowed_tools,
                        blocked_tools=blocked_tools,
                    )
                    user_context.session_id = session_id
                    full_response = []
                    decision = classify_chat_complexity(
                        user_message,
                        threshold=settings.chat_complexity_threshold,
                        routing_enabled=settings.chat_complexity_routing_enabled,
                    )
                    execution_mode = "chat_orchestrated" if decision.is_complex else "chat"
                    execution_history = build_orchestrated_chat_history(
                        history,
                        enabled=decision.is_complex and settings.chat_orchestration_enabled,
                        max_steps=settings.chat_orchestration_max_steps,
                    )
                    if decision.is_complex:
                        metrics.inc_chat_complexity_complex_count()
                        metrics.inc_chat_complexity_routed_complex_count()
                    else:
                        metrics.inc_chat_complexity_simple_count()
                    if decision.is_complex:
                        complete = await _execute_chat_turn(
                            execution_history,
                            user_context,
                            user_prompt=user_message,
                            mode=execution_mode,
                            stage="chat_complex",
                            enable_validation=True,
                        )
                        for token_chunk in _chunk_text(complete):
                            full_response.append(token_chunk)
                            await websocket.send_json({"type": "token", "content": token_chunk})
                        complete = "".join(full_response)
                    else:
                        async for token_chunk in stream_agent(
                            execution_history,
                            user_context,
                            mode=execution_mode,
                            stage="chat_simple",
                        ):
                            full_response.append(token_chunk)
                            await websocket.send_json({"type": "token", "content": token_chunk})
                        complete = "".join(full_response)

                    # Store assistant reply
                    assistant_msg = ChatMessage(
                        session_id=session_id, role="assistant", content=complete
                    )
                    db.add(assistant_msg)
                    await db.commit()

                    await websocket.send_json({"type": "done", "content": complete})

            # Wait for the next message from the client
            raw = await websocket.receive_text()
            data = json.loads(raw)
            user_message = data.get("content", "").strip()
            allowed_tools = data.get("allowed_tools") if isinstance(data, dict) else None
            blocked_tools = data.get("blocked_tools") if isinstance(data, dict) else None

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("Unhandled error in WebSocket handler", session_id=session_id)
        try:
            await websocket.send_json({"type": "error", "content": "Server error — check server logs"})
        except Exception:
            pass
    finally:
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


async def _execute_chat_turn(
    history: List[Dict[str, Any]],
    user_context: UserContext,
    *,
    user_prompt: str,
    mode: str,
    stage: str,
    enable_validation: bool,
) -> str:
    reply = await run_agent(
        history,
        user_context,
        mode=mode,
        stage=stage,
    )
    if not (enable_validation and settings.chat_validation_enabled):
        return reply

    max_attempts = max(0, int(settings.chat_validation_retry_max_attempts))
    attempts = 0
    current = reply

    while True:
        validation = validate_chat_response(user_prompt, current)
        if validation.invalid_urls:
            metrics.inc_chat_validation_invalid_link_count(len(validation.invalid_urls))

        should_retry = (
            settings.chat_validation_retry_enabled
            and validation.should_retry
            and attempts < max_attempts
        )
        if not should_retry:
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
            stage=f"{stage}_retry",
        )


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


async def _load_history(session_id: int, db: AsyncSession) -> List[Dict[str, Any]]:
    """Load conversation history as a list of {role, content} dicts."""
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    messages = result.scalars().all()
    return [{"role": m.role, "content": m.content} for m in messages]


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
