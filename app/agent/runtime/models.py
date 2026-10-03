"""Small typed value objects shared by chat runtime implementations.

These types describe runtime state without taking ownership of loop policy,
persistence, provider calls, or tool dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ChatRunContext:
    run_id: str
    mode: str
    model: str
    stage: str | None = None
    session_id: int | None = None
    task_id: int | None = None
    user_id: int | None = None
    persona: str | None = None
    is_incognito: bool = False


@dataclass(frozen=True)
class ToolCallRequest:
    tool_call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    structured_content: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    approval_state: str | None = None


@dataclass(frozen=True)
class ModelTurn:
    turn: int
    model: str
    tools_enabled: bool
    history_length: int
    content: str = ""
    reasoning_metadata: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunOutcome:
    status: Literal[
        "completed",
        "failed",
        "cancelled",
        "waiting_approval",
        "max_turns",
        "convergence_stopped",
    ]
    content_chars: int = 0
    error_type: str | None = None


@dataclass
class AgentRunState:
    context: ChatRunContext
    turn: int = 0
    phase: str = "starting"
    history: list[dict[str, Any]] = field(default_factory=list)
    available_tools: list[dict[str, Any]] = field(default_factory=list)
    executed_tools: list[ToolCallResult] = field(default_factory=list)
    convergence_counters: dict[str, int] = field(default_factory=dict)
    tool_calls_completed: int = 0
    outcome: RunOutcome | None = None
