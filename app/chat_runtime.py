from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runtime import AgentEvent, AgentEventType
from app.db.models import ChatRun


_DURABLE_EVENT_PHASES: dict[AgentEventType, str] = {
    AgentEventType.RUN_STARTED: "model_starting",
    AgentEventType.PHASE_CHANGED: "running",
    AgentEventType.MODEL_TURN_STARTED: "model_active",
    AgentEventType.REASONING_STARTED: "reasoning",
    AgentEventType.TOOL_REQUESTED: "tool_requested",
    AgentEventType.TOOL_STARTED: "tool_active",
    AgentEventType.TOOL_COMPLETED: "tool_completed",
    AgentEventType.TOOL_FAILED: "tool_failed",
    AgentEventType.APPROVAL_REQUIRED: "waiting_approval",
    AgentEventType.SYNTHESIS_STARTED: "synthesizing",
    AgentEventType.VALIDATION_STARTED: "validating",
    AgentEventType.VALIDATION_RETRY: "retrying",
    AgentEventType.RUN_COMPLETED: "finalizing",
    AgentEventType.RUN_FAILED: "failed",
    AgentEventType.RUN_CANCELLED: "cancelled",
}


@dataclass
class RecentPromptState:
    normalized_prompt: str
    fingerprint: str
    timestamp_monotonic: float
    active: bool


@dataclass
class RecentSendIdState:
    client_send_id: str
    timestamp_monotonic: float
    active: bool


class ChatRunManager:
    def __init__(self, *, duplicate_window_seconds: float = 300.0) -> None:
        self._active_runs: dict[int, asyncio.Task] = {}
        self._active_run_ids: dict[int, str] = {}
        self._recent_prompts: dict[int, RecentPromptState] = {}
        self._recent_send_ids: dict[int, RecentSendIdState] = {}
        self._lock = asyncio.Lock()
        self._duplicate_window_seconds = duplicate_window_seconds

    @staticmethod
    def normalize_prompt(prompt: str) -> str:
        return " ".join(str(prompt or "").split()).strip()

    @staticmethod
    def fingerprint_prompt(prompt: str) -> str:
        normalized = ChatRunManager.normalize_prompt(prompt)
        if not normalized:
            return ""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]

    async def register(self, session_id: int, task: asyncio.Task, *, run_id: str | None = None) -> None:
        async with self._lock:
            self._active_runs[session_id] = task
            if run_id:
                self._active_run_ids[session_id] = run_id
            else:
                self._active_run_ids.pop(session_id, None)

    async def clear(self, session_id: int, task: Optional[asyncio.Task] = None) -> None:
        async with self._lock:
            current = self._active_runs.get(session_id)
            if current is None:
                return
            if task is not None and current is not task:
                return
            self._active_runs.pop(session_id, None)
            self._active_run_ids.pop(session_id, None)

    async def request_stop(self, session_id: int) -> bool:
        async with self._lock:
            task = self._active_runs.get(session_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def is_active(self, session_id: int) -> bool:
        async with self._lock:
            task = self._active_runs.get(session_id)
        return task is not None and not task.done()

    async def active_run_id(self, session_id: int) -> str | None:
        async with self._lock:
            task = self._active_runs.get(session_id)
            run_id = self._active_run_ids.get(session_id)
        if task is None or task.done():
            return None
        return str(run_id or "").strip() or None

    async def claim_prompt(self, session_id: int, prompt: str) -> tuple[bool, bool, str]:
        normalized = self.normalize_prompt(prompt)
        fingerprint = self.fingerprint_prompt(prompt)
        if not normalized:
            return True, False, fingerprint

        now = time.monotonic()
        async with self._lock:
            current = self._recent_prompts.get(session_id)
            if (
                current is not None
                and current.normalized_prompt == normalized
                and (now - current.timestamp_monotonic) <= self._duplicate_window_seconds
            ):
                return False, current.active, current.fingerprint

            self._recent_prompts[session_id] = RecentPromptState(
                normalized_prompt=normalized,
                fingerprint=fingerprint,
                timestamp_monotonic=now,
                active=True,
            )
            return True, False, fingerprint

    async def claim_client_send_id(self, session_id: int, client_send_id: str) -> tuple[bool, bool]:
        normalized = str(client_send_id or "").strip()
        if not normalized:
            return True, False

        now = time.monotonic()
        async with self._lock:
            current = self._recent_send_ids.get(session_id)
            if (
                current is not None
                and current.client_send_id == normalized
                and (now - current.timestamp_monotonic) <= self._duplicate_window_seconds
            ):
                return False, current.active

            self._recent_send_ids[session_id] = RecentSendIdState(
                client_send_id=normalized,
                timestamp_monotonic=now,
                active=True,
            )
            return True, False

    async def mark_prompt_finished(self, session_id: int, prompt: str) -> None:
        normalized = self.normalize_prompt(prompt)
        if not normalized:
            return

        now = time.monotonic()
        async with self._lock:
            current = self._recent_prompts.get(session_id)
            if current is None or current.normalized_prompt != normalized:
                return
            current.active = False
            current.timestamp_monotonic = now

    async def mark_client_send_id_finished(self, session_id: int, client_send_id: str) -> None:
        normalized = str(client_send_id or "").strip()
        if not normalized:
            return

        now = time.monotonic()
        async with self._lock:
            current = self._recent_send_ids.get(session_id)
            if current is None or current.client_send_id != normalized:
                return
            current.active = False
            current.timestamp_monotonic = now


_chat_run_manager: ChatRunManager | None = None


def get_chat_run_manager() -> ChatRunManager:
    global _chat_run_manager
    if _chat_run_manager is None:
        _chat_run_manager = ChatRunManager()
    return _chat_run_manager


def new_chat_run_id() -> str:
    return f"chat_run_{uuid4().hex}"


async def create_chat_run(
    db: AsyncSession,
    *,
    session_id: int,
    user_id: int,
    user_message_id: int,
    client_send_id: str | None,
    model: str | None,
    mode: str,
    stage: str,
) -> ChatRun:
    run = ChatRun(
        id=new_chat_run_id(),
        session_id=session_id,
        user_id=user_id,
        user_message_id=user_message_id,
        client_send_id=str(client_send_id or "").strip() or None,
        status="running",
        phase="starting",
        model=str(model or "").strip() or None,
        mode=mode,
        stage=stage,
    )
    db.add(run)
    await db.flush()
    return run


async def latest_chat_run(db: AsyncSession, session_id: int) -> ChatRun | None:
    result = await db.execute(
        select(ChatRun)
        .where(ChatRun.session_id == session_id)
        .order_by(desc(ChatRun.started_at), desc(ChatRun.id))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def owned_chat_run(
    db: AsyncSession,
    *,
    run_id: str,
    user_id: int,
) -> ChatRun | None:
    result = await db.execute(
        select(ChatRun).where(ChatRun.id == run_id, ChatRun.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def apply_chat_run_event(db: AsyncSession, run: ChatRun, event: AgentEvent) -> None:
    """Project bounded runtime lifecycle events onto the durable run row."""
    run.last_event_sequence = max(int(run.last_event_sequence or 0), int(event.sequence))
    phase = _DURABLE_EVENT_PHASES.get(event.type)
    if event.type == AgentEventType.PHASE_CHANGED:
        phase = str(event.payload.get("phase") or "running").strip() or "running"
    if phase:
        run.phase = phase
    run.updated_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_chat_run_terminal(
    db: AsyncSession,
    run: ChatRun,
    *,
    status: str,
    phase: str,
    assistant_message_id: int | None = None,
    error_classification: str | None = None,
) -> None:
    run.status = status
    run.phase = phase
    run.assistant_message_id = assistant_message_id
    run.error_classification = error_classification
    now = datetime.now(timezone.utc)
    run.updated_at = now
    run.finished_at = now
    await db.flush()


def serialize_chat_approval(run: ChatRun) -> dict[str, Any] | None:
    payload = run.approval_payload
    if run.status != "waiting_approval" or not isinstance(payload, dict):
        return None
    return {
        "kind": str(run.approval_kind or "tool"),
        "blocked_tool": str(payload.get("tool_name") or ""),
        "reason": str(payload.get("reason") or ""),
        "arguments": dict(payload.get("arguments") or {}),
    }


def serialize_chat_run(run: ChatRun, *, active: bool) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "active": bool(active),
        "status": run.status,
        "phase": run.phase,
        "mode": run.mode,
        "stage": run.stage,
        "model": run.model,
        "last_event_sequence": int(run.last_event_sequence or 0),
        "waiting_approval": serialize_chat_approval(run),
        "error_classification": run.error_classification,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
        "user_message_id": run.user_message_id,
        "assistant_message_id": run.assistant_message_id,
    }
