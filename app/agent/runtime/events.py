"""Canonical, bounded runtime events for agent-loop observers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

from app.agent.runtime.models import ToolCallResult


class AgentEventType(str, Enum):
    RUN_STARTED = "run_started"
    PHASE_CHANGED = "phase_changed"
    MODEL_TURN_STARTED = "model_turn_started"
    REASONING_STARTED = "reasoning_started"
    TEXT_DELTA = "text_delta"
    PROVISIONAL_TEXT_RESET = "provisional_text_reset"
    TOOL_REQUESTED = "tool_requested"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    APPROVAL_REQUIRED = "approval_required"
    SYNTHESIS_STARTED = "synthesis_started"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_RETRY = "validation_retry"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


AgentEventCallback = Callable[["AgentEvent"], Awaitable[None]]
ProvisionalTextCallback = Callable[[str, str], Awaitable[None]]

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
)
_SENSITIVE_TOKEN_KEYS = {
    "access_token",
    "bearer_token",
    "refresh_token",
    "token",
}
_MAX_STRING_CHARS = 1_000
_MAX_COLLECTION_ITEMS = 30
_MAX_DEPTH = 5


def _sanitize_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    lowered_key = key.casefold()
    if lowered_key in _SENSITIVE_TOKEN_KEYS or any(part in lowered_key for part in _SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if depth >= _MAX_DEPTH:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return f"{value[:_MAX_STRING_CHARS]}...[truncated {len(value) - _MAX_STRING_CHARS} chars]"
    if isinstance(value, Mapping):
        items = list(value.items())
        sanitized = {
            str(item_key): _sanitize_value(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in items[:_MAX_COLLECTION_ITEMS]
        }
        if len(items) > _MAX_COLLECTION_ITEMS:
            sanitized["_truncated_items"] = len(items) - _MAX_COLLECTION_ITEMS
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = list(value)
        sanitized_items = [
            _sanitize_value(item, depth=depth + 1)
            for item in items[:_MAX_COLLECTION_ITEMS]
        ]
        if len(items) > _MAX_COLLECTION_ITEMS:
            sanitized_items.append(f"[truncated {len(items) - _MAX_COLLECTION_ITEMS} items]")
        return sanitized_items
    return str(value)[:_MAX_STRING_CHARS]


@dataclass(frozen=True)
class AgentEvent:
    event_id: str
    run_id: str
    sequence: int
    timestamp: datetime
    type: AgentEventType
    session_id: int | None = None
    task_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp.isoformat(),
            "type": self.type.value,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "payload": self.payload,
        }


class AgentEventEmitter:
    """Emit an ordered event stream for one logical agent run."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        session_id: int | None = None,
        task_id: int | None = None,
        callback: AgentEventCallback | None = None,
    ) -> None:
        self.run_id = run_id or f"run_{uuid4().hex}"
        self.session_id = session_id
        self.task_id = task_id
        self.callback = callback
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._started = False
        self._terminal = False

    @property
    def is_observed(self) -> bool:
        return self.callback is not None

    async def emit(self, event_type: AgentEventType, **payload: Any) -> AgentEvent:
        async with self._lock:
            self._sequence += 1
            event = AgentEvent(
                event_id=f"{self.run_id}:{self._sequence}",
                run_id=self.run_id,
                sequence=self._sequence,
                timestamp=datetime.now(timezone.utc),
                type=event_type,
                session_id=self.session_id,
                task_id=self.task_id,
                payload=_sanitize_value(payload),
            )
            if self.callback is not None:
                await self.callback(event)
            return event

    async def start_once(self, **payload: Any) -> AgentEvent | None:
        if self._started:
            return None
        self._started = True
        return await self.emit(AgentEventType.RUN_STARTED, **payload)

    async def terminal_once(self, event_type: AgentEventType, **payload: Any) -> AgentEvent | None:
        if self._terminal:
            return None
        self._terminal = True
        return await self.emit(event_type, **payload)


def content_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _tool_call_parts(call: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    function = call.get("function") or {}
    name = str(function.get("name") or call.get("name") or "unknown")
    call_id = str(call.get("id") or "")
    raw_arguments = function.get("arguments", {})
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            arguments = {}
    elif isinstance(raw_arguments, Mapping):
        arguments = dict(raw_arguments)
    else:
        arguments = {}
    return call_id, name, arguments


async def emit_tool_requested_events(
    emitter: AgentEventEmitter | None,
    tool_calls: Sequence[Mapping[str, Any]],
    *,
    turn: int,
) -> None:
    if emitter is None:
        return
    for call in tool_calls:
        call_id, name, arguments = _tool_call_parts(call)
        payload = {
            "turn": turn,
            "tool_call_id": call_id,
            "tool_name": name,
            "argument_keys": sorted(str(key) for key in arguments),
        }
        await emitter.emit(AgentEventType.TOOL_REQUESTED, **payload)
        await emitter.emit(AgentEventType.TOOL_STARTED, **payload)


async def emit_tool_completed_events(
    emitter: AgentEventEmitter | None,
    tool_calls: Sequence[Mapping[str, Any]],
    tool_results: Sequence[Mapping[str, Any] | ToolCallResult],
    *,
    turn: int,
) -> None:
    if emitter is None:
        return
    names_by_id = {
        call_id: name
        for call_id, name, _ in (_tool_call_parts(call) for call in tool_calls)
    }
    for result in tool_results:
        if isinstance(result, ToolCallResult):
            call_id = result.tool_call_id
            content = result.content
            is_error = result.is_error
            tool_name = result.name
            artifact_count = len(result.artifacts)
            citation_count = len(result.citations)
            has_structured_content = result.structured_content is not None
        else:
            call_id = str(result.get("tool_call_id") or "")
            content = str(result.get("content") or "")
            lowered = content.casefold().lstrip()
            is_error = bool(result.get("is_error")) or lowered.startswith(("error", "tool ")) and "failed" in lowered[:120]
            tool_name = names_by_id.get(call_id, "unknown")
            artifact_count = len(result.get("artifacts") or [])
            citation_count = len(result.get("citations") or [])
            has_structured_content = isinstance(result.get("structured_content"), Mapping)
        event_type = AgentEventType.TOOL_FAILED if is_error else AgentEventType.TOOL_COMPLETED
        await emitter.emit(
            event_type,
            turn=turn,
            tool_call_id=call_id,
            tool_name=tool_name,
            content_chars=len(content),
            result_fingerprint=content_fingerprint(content),
            has_structured_content=has_structured_content,
            artifact_count=artifact_count,
            citation_count=citation_count,
        )


def wrap_provisional_text_callback(
    emitter: AgentEventEmitter | None,
    callback: ProvisionalTextCallback | None,
) -> ProvisionalTextCallback | None:
    """Mirror legacy provisional-text callbacks into the canonical event stream."""
    if emitter is None or not emitter.is_observed:
        return callback

    async def _wrapped(action: str, content: str) -> None:
        if action == "delta" and content:
            await emitter.emit(
                AgentEventType.TEXT_DELTA,
                content=content,
                provisional=True,
            )
        elif action == "reset":
            await emitter.emit(AgentEventType.PROVISIONAL_TEXT_RESET)
        elif action == "commit":
            await emitter.emit(AgentEventType.PHASE_CHANGED, phase="provisional_text_committed")
        if callback is not None:
            await callback(action, content)

    return _wrapped
