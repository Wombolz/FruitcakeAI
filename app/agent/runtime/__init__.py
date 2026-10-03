"""Typed runtime contracts for agent execution and observation."""

from app.agent.runtime.events import (
    AgentEvent,
    AgentEventEmitter,
    AgentEventType,
    emit_tool_completed_events,
    emit_tool_requested_events,
    wrap_provisional_text_callback,
)
from app.agent.runtime.models import (
    AgentRunState,
    ChatRunContext,
    ModelTurn,
    RunOutcome,
    ToolCallRequest,
    ToolCallResult,
    ToolOutputText,
    build_tool_call_result,
    normalize_tool_call_results,
)
from app.agent.runtime.provider import ProviderCapabilities, resolve_provider_capabilities

__all__ = [
    "AgentEvent",
    "AgentEventEmitter",
    "AgentEventType",
    "AgentRunState",
    "ChatRunContext",
    "ModelTurn",
    "ProviderCapabilities",
    "RunOutcome",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolOutputText",
    "build_tool_call_result",
    "emit_tool_completed_events",
    "emit_tool_requested_events",
    "normalize_tool_call_results",
    "resolve_provider_capabilities",
    "wrap_provisional_text_callback",
]
