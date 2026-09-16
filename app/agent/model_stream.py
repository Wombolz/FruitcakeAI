"""Provider-neutral model stream normalization and turn accumulation."""

from __future__ import annotations

import inspect
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal


StreamEventKind = Literal[
    "reasoning_delta",
    "text_delta",
    "tool_call_delta",
    "usage",
    "turn_finished",
]


class ModelStreamError(RuntimeError):
    """Raised when a provider stream cannot form a valid assistant turn."""


@dataclass(frozen=True)
class ModelStreamEvent:
    kind: StreamEventKind
    text: str | None = None
    tool_call_index: int | None = None
    tool_call_position: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_payload: str | dict[str, Any] | None = None
    tool_call_complete: bool = False
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class ModelTurnResult:
    content: str
    reasoning_content: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int] | None
    finish_reason: str | None
    event_count: int

    def assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if self.reasoning_content:
            message["reasoning_content"] = self.reasoning_content
        return message


@dataclass
class _ToolCallState:
    ordinal: int
    provider_id: str = ""
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)
    argument_object: dict[str, Any] | None = None
    complete: bool = False


class ModelTurnAccumulator:
    """Reconstruct a complete assistant message from normalized stream events."""

    def __init__(self, *, request_id: str | None = None) -> None:
        self.request_id = request_id or uuid.uuid4().hex[:12]
        self.text_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self._tool_calls: dict[tuple[str, int | str], _ToolCallState] = {}
        self._usage: dict[str, int] | None = None
        self.finish_reason: str | None = None
        self.event_count = 0

    def add(self, event: ModelStreamEvent) -> None:
        self.event_count += 1
        if event.kind == "text_delta" and event.text:
            self.text_parts.append(event.text)
            return
        if event.kind == "reasoning_delta" and event.text:
            self.reasoning_parts.append(event.text)
            return
        if event.kind == "usage" and event.usage:
            if sum(event.usage.values()) > 0:
                self._usage = dict(event.usage)
            return
        if event.kind == "turn_finished":
            self.finish_reason = event.finish_reason or self.finish_reason
            return
        if event.kind != "tool_call_delta":
            return

        key: tuple[str, int | str]
        if event.tool_call_index is not None:
            key = ("index", event.tool_call_index)
        else:
            # Native Ollama calls are unindexed. Position remains stable across
            # their complete call list and also lets a provider id arrive late.
            key = ("position", event.tool_call_position or 0)
        state = self._tool_calls.get(key)
        if state is None:
            state = _ToolCallState(ordinal=len(self._tool_calls))
            self._tool_calls[key] = state
        if event.tool_call_id:
            state.provider_id = event.tool_call_id
        if event.tool_name:
            state.name_parts.append(event.tool_name)
        if isinstance(event.arguments_payload, dict):
            state.argument_object = event.arguments_payload
        elif isinstance(event.arguments_payload, str):
            state.argument_parts.append(event.arguments_payload)
        state.complete = state.complete or event.tool_call_complete

    def finish(self) -> ModelTurnResult:
        calls = [self._canonical_tool_call(state) for state in self._tool_calls.values()]
        return ModelTurnResult(
            content="".join(self.text_parts),
            reasoning_content="".join(self.reasoning_parts),
            tool_calls=calls,
            usage=dict(self._usage) if self._usage else None,
            finish_reason=self.finish_reason,
            event_count=self.event_count,
        )

    def _canonical_tool_call(self, state: _ToolCallState) -> dict[str, Any]:
        name = "".join(state.name_parts).strip()
        if not name:
            raise ModelStreamError("Streamed tool call did not include a function name")

        if state.argument_object is not None:
            arguments = state.argument_object
        else:
            raw_arguments = "".join(state.argument_parts).strip() or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except (TypeError, ValueError) as exc:
                raise ModelStreamError(f"Malformed streamed arguments for tool {name}") from exc
            if not isinstance(arguments, dict):
                raise ModelStreamError(f"Streamed arguments for tool {name} must be an object")

        call_id = state.provider_id or f"call_stream_{self.request_id}_{state.ordinal}"
        return {
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            },
        }


class ModelStreamNormalizer:
    """Translate LiteLLM chunks while separating explicit or tagged reasoning."""

    _OPEN_TAG = "<think>"
    _CLOSE_TAG = "</think>"

    def __init__(self) -> None:
        self._inside_reasoning = False
        self._content_buffer = ""
        self._finished = False

    def feed(self, chunk: Any) -> list[ModelStreamEvent]:
        events: list[ModelStreamEvent] = []
        choices = _value(chunk, "choices", []) or []
        finish_reason: str | None = None

        for choice in choices:
            delta = _value(choice, "delta", None)
            finish_reason = str(_value(choice, "finish_reason", "") or "") or finish_reason
            if delta is None:
                continue

            reasoning = _first_text(
                _value(delta, "reasoning_content", None),
                _value(delta, "reasoning", None),
                _value(delta, "thinking", None),
            )
            if reasoning:
                events.append(ModelStreamEvent(kind="reasoning_delta", text=reasoning))

            content = _value(delta, "content", None)
            if isinstance(content, str) and content:
                events.extend(self._split_tagged_content(content, terminal=False))

            tool_calls = _value(delta, "tool_calls", None) or []
            for position, call in enumerate(tool_calls):
                function = _value(call, "function", {}) or {}
                arguments = _value(function, "arguments", None)
                index = _optional_int(_value(call, "index", None))
                events.append(
                    ModelStreamEvent(
                        kind="tool_call_delta",
                        tool_call_index=index,
                        tool_call_position=position,
                        tool_call_id=str(_value(call, "id", "") or "") or None,
                        tool_name=str(_value(function, "name", "") or "") or None,
                        arguments_payload=arguments,
                        tool_call_complete=isinstance(arguments, dict),
                        finish_reason=finish_reason,
                    )
                )

        usage = _normalized_usage(_value(chunk, "usage", None))
        if usage and sum(usage.values()) > 0:
            events.append(ModelStreamEvent(kind="usage", usage=usage))

        if finish_reason:
            events.extend(self._split_tagged_content("", terminal=True))
            events.append(ModelStreamEvent(kind="turn_finished", finish_reason=finish_reason))
            self._finished = True
        return events

    def finish(self) -> list[ModelStreamEvent]:
        events = self._split_tagged_content("", terminal=True)
        if not self._finished:
            events.append(ModelStreamEvent(kind="turn_finished"))
            self._finished = True
        return events

    def _split_tagged_content(self, content: str, *, terminal: bool) -> list[ModelStreamEvent]:
        self._content_buffer += content
        events: list[ModelStreamEvent] = []
        while self._content_buffer:
            marker = self._CLOSE_TAG if self._inside_reasoning else self._OPEN_TAG
            marker_index = self._content_buffer.find(marker)
            if marker_index >= 0:
                prefix = self._content_buffer[:marker_index]
                if prefix:
                    events.append(
                        ModelStreamEvent(
                            kind="reasoning_delta" if self._inside_reasoning else "text_delta",
                            text=prefix,
                        )
                    )
                self._content_buffer = self._content_buffer[marker_index + len(marker) :]
                self._inside_reasoning = not self._inside_reasoning
                continue

            if terminal:
                events.append(
                    ModelStreamEvent(
                        kind="reasoning_delta" if self._inside_reasoning else "text_delta",
                        text=self._content_buffer,
                    )
                )
                self._content_buffer = ""
                break

            keep = _possible_marker_suffix_length(self._content_buffer, marker)
            emit_count = len(self._content_buffer) - keep
            if emit_count <= 0:
                break
            prefix = self._content_buffer[:emit_count]
            self._content_buffer = self._content_buffer[emit_count:]
            events.append(
                ModelStreamEvent(
                    kind="reasoning_delta" if self._inside_reasoning else "text_delta",
                    text=prefix,
                )
            )
        return events


async def iter_model_stream_events(provider_stream: Any) -> AsyncIterator[ModelStreamEvent]:
    normalizer = ModelStreamNormalizer()
    async for chunk in provider_stream:
        for event in normalizer.feed(chunk):
            yield event
    for event in normalizer.finish():
        yield event


async def close_provider_stream(provider_stream: Any) -> None:
    close = getattr(provider_stream, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalized_usage(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    values = {
        "prompt_tokens": _value(usage, "prompt_tokens", 0),
        "completion_tokens": _value(usage, "completion_tokens", 0),
        "total_tokens": _value(usage, "total_tokens", 0),
    }
    normalized: dict[str, int] = {}
    for key, value in values.items():
        try:
            normalized[key] = max(0, int(value or 0))
        except (TypeError, ValueError):
            normalized[key] = 0
    if normalized["total_tokens"] <= 0:
        normalized["total_tokens"] = normalized["prompt_tokens"] + normalized["completion_tokens"]
    return normalized


def _possible_marker_suffix_length(content: str, marker: str) -> int:
    maximum = min(len(content), len(marker) - 1)
    for length in range(maximum, 0, -1):
        if marker.startswith(content[-length:]):
            return length
    return 0
