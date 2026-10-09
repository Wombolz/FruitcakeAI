"""Small typed value objects shared by chat runtime implementations.

These types describe runtime state without taking ownership of loop policy,
persistence, provider calls, or tool dispatch.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence


_MAX_STRUCTURED_RESULT_CHARS = 64_000
_MAX_RESULT_ARTIFACTS = 8
_MAX_RESULT_CITATIONS = 12


class ToolOutputText(str):
    """String-compatible tool output carrying optional MCP structured content."""

    structured_content: dict[str, Any] | None

    def __new__(
        cls,
        value: Any,
        *,
        structured_content: Mapping[str, Any] | None = None,
    ) -> "ToolOutputText":
        instance = super().__new__(cls, str(value or ""))
        instance.structured_content = (
            dict(structured_content) if structured_content is not None else None
        )
        return instance


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
    citations: list[dict[str, Any]] = field(default_factory=list)
    approval_state: str | None = None

    def to_message(self) -> dict[str, Any]:
        """Project rich runtime state onto the provider-compatible tool message."""
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }

    def to_execution_record(self, *, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "tool": self.name,
            "arguments": dict(arguments or {}),
            "result_summary": self.content,
            "is_error": self.is_error,
        }
        if self.structured_content is not None:
            record["structured_content"] = self.structured_content
        if self.artifacts:
            record["artifacts"] = list(self.artifacts)
        if self.citations:
            record["citations"] = list(self.citations)
        if self.approval_state:
            record["approval_state"] = self.approval_state
        return record


def _parse_structured_content(content: str) -> dict[str, Any] | None:
    text = str(content or "").strip()
    if not text or len(text) > _MAX_STRUCTURED_RESULT_CHARS or not text.startswith("{"):
        return None
    for parser in (json.loads, ast.literal_eval):
        try:
            value = parser(text)
        except (TypeError, ValueError, SyntaxError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _normalize_citations(structured: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not structured:
        return []
    raw = structured.get("citations") or structured.get("sources") or []
    if not isinstance(raw, list):
        return []
    citations: list[dict[str, Any]] = []
    for item in raw[:_MAX_RESULT_CITATIONS]:
        if isinstance(item, str) and item.strip():
            citations.append({"url": item.strip()})
        elif isinstance(item, Mapping):
            citation = {
                str(key): value
                for key, value in item.items()
                if str(key) in {"url", "title", "label", "source", "document", "path"}
                and value not in (None, "")
            }
            if citation:
                citations.append(citation)
    return citations


def _normalize_artifacts(
    structured: Mapping[str, Any] | None,
    *,
    tool_name: str,
) -> list[dict[str, Any]]:
    if not structured:
        return []
    single_artifact = structured.get("artifact")
    raw_artifacts = structured.get("artifacts")
    candidates = list(raw_artifacts) if isinstance(raw_artifacts, list) else []
    if isinstance(single_artifact, Mapping):
        candidates.insert(0, single_artifact)
    image_path = str(structured.get("image_path") or "").strip()
    if image_path:
        candidates.insert(
            0,
            {
                "kind": "image",
                "path": image_path,
                **{
                    key: structured[key]
                    for key in ("prompt", "workflow", "seed", "width", "height")
                    if structured.get(key) is not None
                },
            },
        )
    artifacts: list[dict[str, Any]] = []
    for item in candidates[:_MAX_RESULT_ARTIFACTS]:
        if not isinstance(item, Mapping):
            continue
        artifact = dict(item)
        if {
            "type",
            "schema_version",
            "title",
        }.issubset(artifact) and any(
            artifact.get(key) is not None for key in ("payload", "resources", "fallback")
        ):
            artifacts.append(artifact)
            continue
        artifact.setdefault("source_tool", tool_name)
        if artifact.get("path") or artifact.get("url"):
            artifacts.append(artifact)
    return artifacts


def build_tool_call_result(
    *,
    tool_call_id: str,
    name: str,
    content: Any,
    is_error: bool | None = None,
    structured_content: Mapping[str, Any] | None = None,
    approval_state: str | None = None,
) -> ToolCallResult:
    text = str(content or "")
    attached_structured = getattr(content, "structured_content", None)
    structured = (
        dict(structured_content)
        if structured_content is not None
        else dict(attached_structured)
        if isinstance(attached_structured, Mapping)
        else _parse_structured_content(text)
    )
    lowered = text.casefold().lstrip()
    inferred_error = (
        lowered.startswith(("error", "unknown tool", "unknown mcp tool"))
        or (lowered.startswith(("tool ", "mcp server")) and "failed" in lowered[:120])
        or (lowered.startswith("mcp server") and "not available" in lowered[:160])
    )
    return ToolCallResult(
        tool_call_id=str(tool_call_id or ""),
        name=str(name or "").strip() or "unknown",
        content=text,
        is_error=inferred_error if is_error is None else bool(is_error),
        structured_content=structured,
        artifacts=_normalize_artifacts(structured, tool_name=str(name or "").strip()),
        citations=_normalize_citations(structured),
        approval_state=approval_state,
    )


def normalize_tool_call_results(
    tool_calls: Sequence[Any],
    tool_messages: Sequence[Mapping[str, Any] | ToolCallResult],
) -> list[ToolCallResult]:
    names_by_id: dict[str, str] = {}
    for call in tool_calls:
        if isinstance(call, Mapping):
            call_id = str(call.get("id") or "")
            name = str(((call.get("function") or {}).get("name") or ""))
        else:
            call_id = str(getattr(call, "id", "") or "")
            name = str(getattr(getattr(call, "function", None), "name", "") or "")
        names_by_id[call_id] = name

    normalized: list[ToolCallResult] = []
    for message in tool_messages:
        if isinstance(message, ToolCallResult):
            normalized.append(message)
            continue
        call_id = str(message.get("tool_call_id") or "")
        normalized.append(
            build_tool_call_result(
                tool_call_id=call_id,
                name=names_by_id.get(call_id, "unknown"),
                content=message.get("content"),
                is_error=message.get("is_error") if "is_error" in message else None,
                structured_content=(
                    message.get("structured_content")
                    if isinstance(message.get("structured_content"), Mapping)
                    else None
                ),
                approval_state=str(message.get("approval_state") or "") or None,
            )
        )
    return normalized


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
