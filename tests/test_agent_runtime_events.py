from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.context import UserContext
from app.agent.core import run_agent, stream_agent
from app.agent.runtime import (
    AgentEventEmitter,
    AgentEventType,
    ToolOutputText,
    build_tool_call_result,
    emit_tool_completed_events,
)
from app.autonomy.approval import ApprovalRequired
from app.config import settings


class _FakeMessage:
    def __init__(self, *, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none: bool = True):
        payload = {"content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in self.tool_calls
            ]
        return payload


def _response(*, content: str = "", tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=_FakeMessage(content=content, tool_calls=tool_calls),
                finish_reason="tool_calls" if tool_calls else "stop",
            )
        ]
    )


def _stream_response(content: str):
    async def _stream():
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=content))],
            usage=None,
        )

    return _stream()


def _context() -> UserContext:
    return UserContext(
        user_id=1,
        username="tester",
        role="parent",
        session_id=42,
    )


def test_structured_tool_result_preserves_plain_model_message():
    output = ToolOutputText(
        "Image generated.",
        structured_content={
            "image_path": "generated_images/map.png",
            "prompt": "A system map",
            "sources": [{"url": "https://example.com/source", "title": "Source"}],
        },
    )

    result = build_tool_call_result(
        tool_call_id="call_image",
        name="generate_image",
        content=output,
    )

    assert result.to_message() == {
        "role": "tool",
        "tool_call_id": "call_image",
        "content": "Image generated.",
    }
    assert result.artifacts == [
        {
            "kind": "image",
            "path": "generated_images/map.png",
            "prompt": "A system map",
            "source_tool": "generate_image",
        }
    ]
    assert result.citations == [
        {"url": "https://example.com/source", "title": "Source"}
    ]


@pytest.mark.asyncio
async def test_tool_completion_event_reports_structured_metadata_without_content():
    events = []

    async def _collect(event):
        events.append(event)

    emitter = AgentEventEmitter(callback=_collect, session_id=42)
    result = build_tool_call_result(
        tool_call_id="call_image",
        name="generate_image",
        content='{"image_path":"generated_images/map.png"}',
    )
    await emit_tool_completed_events(
        emitter,
        [{"id": "call_image", "function": {"name": "generate_image", "arguments": "{}"}}],
        [result],
        turn=1,
    )

    assert len(events) == 1
    assert events[0].type == AgentEventType.TOOL_COMPLETED
    assert events[0].payload["has_structured_content"] is True
    assert events[0].payload["artifact_count"] == 1
    assert events[0].payload["citation_count"] == 0
    assert "content" not in events[0].payload


@pytest.mark.asyncio
async def test_event_emitter_orders_bounds_and_redacts_payloads():
    events = []

    async def _collect(event):
        events.append(event)

    emitter = AgentEventEmitter(
        run_id="run_test",
        session_id=42,
        callback=_collect,
    )
    await emitter.start_once(
        model="test",
        api_key="do-not-log",
        prompt_tokens=120,
    )
    await emitter.emit(AgentEventType.PHASE_CHANGED, detail="x" * 1_500)
    await emitter.terminal_once(AgentEventType.RUN_COMPLETED, content_chars=12)
    await emitter.terminal_once(AgentEventType.RUN_FAILED, error_type="ignored")

    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.type for event in events] == [
        AgentEventType.RUN_STARTED,
        AgentEventType.PHASE_CHANGED,
        AgentEventType.RUN_COMPLETED,
    ]
    assert events[0].payload["api_key"] == "[redacted]"
    assert events[0].payload["prompt_tokens"] == 120
    assert len(events[1].payload["detail"]) < 1_100
    assert events[0].to_dict()["timestamp"].endswith("+00:00")


@pytest.mark.asyncio
async def test_run_agent_emits_lifecycle_without_changing_plain_text_result():
    events = []

    async def _collect(event):
        events.append(event)

    emitter = AgentEventEmitter(callback=_collect, session_id=42)
    with (
        patch("app.agent.core.get_tools_for_user", return_value=[]),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(return_value=_response(content="Hello from Fruitcake.")),
        ),
        patch("app.agent.core.record_llm_usage_event", new=AsyncMock()),
    ):
        result = await run_agent(
            [{"role": "user", "content": "hello"}],
            _context(),
            event_emitter=emitter,
        )

    assert result == "Hello from Fruitcake."
    assert [event.type for event in events] == [
        AgentEventType.RUN_STARTED,
        AgentEventType.MODEL_TURN_STARTED,
        AgentEventType.CONTEXT_BUDGET,
        AgentEventType.RUN_COMPLETED,
    ]
    assert events[2].payload["context_window_tokens"] >= 8_192
    assert events[2].payload["estimated_input_tokens"] >= 0
    assert "content" not in events[2].payload
    assert events[-1].payload == {"content_chars": len(result)}


@pytest.mark.asyncio
async def test_run_agent_emits_tool_sequence_and_preserves_legacy_callbacks():
    events = []
    callback_order = []
    call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="read_file", arguments='{"path":"notes/a.md"}'),
    )
    responses = [
        _response(tool_calls=[call]),
        _response(content="The file contains a short note."),
    ]

    async def _collect(event):
        events.append(event)

    async def _pre_tool(tool_calls):
        callback_order.append(("pre", tool_calls[0]["function"]["name"]))

    async def _runtime_messages(messages):
        callback_order.append(("runtime", messages[-1]["tool_call_id"]))

    emitter = AgentEventEmitter(callback=_collect, session_id=42)
    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "read_file"}}]),
        patch("app.agent.core.litellm.acompletion", new=AsyncMock(side_effect=responses)),
        patch(
            "app.agent.core.dispatch_tool_calls",
            new=AsyncMock(
                return_value=[
                    {"role": "tool", "tool_call_id": "call_1", "content": "hello"}
                ]
            ),
        ),
        patch("app.agent.core.record_llm_usage_event", new=AsyncMock()),
    ):
        result = await run_agent(
            [{"role": "user", "content": "read the note"}],
            _context(),
            runtime_message_callback=_runtime_messages,
            pre_tool_callback=_pre_tool,
            event_emitter=emitter,
        )

    event_types = [event.type for event in events]
    assert result == "The file contains a short note."
    assert callback_order == [("pre", "read_file"), ("runtime", "call_1")]
    assert event_types == [
        AgentEventType.RUN_STARTED,
        AgentEventType.MODEL_TURN_STARTED,
        AgentEventType.CONTEXT_BUDGET,
        AgentEventType.TOOL_REQUESTED,
        AgentEventType.TOOL_STARTED,
        AgentEventType.TOOL_COMPLETED,
        AgentEventType.SYNTHESIS_STARTED,
        AgentEventType.MODEL_TURN_STARTED,
        AgentEventType.CONTEXT_BUDGET,
        AgentEventType.RUN_COMPLETED,
    ]
    requested = events[3]
    assert requested.payload["tool_name"] == "read_file"
    assert requested.payload["argument_keys"] == ["path"]


@pytest.mark.asyncio
async def test_run_agent_emits_approval_required_without_misclassifying_failure():
    events = []
    call = SimpleNamespace(
        id="call_write",
        type="function",
        function=SimpleNamespace(name="write_file", arguments='{"path":"notes/a.md","content":"x"}'),
    )

    async def _collect(event):
        events.append(event)

    emitter = AgentEventEmitter(callback=_collect, session_id=42)
    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "write_file"}}]),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(return_value=_response(tool_calls=[call])),
        ),
        patch(
            "app.agent.core.dispatch_tool_calls",
            new=AsyncMock(side_effect=ApprovalRequired("write_file")),
        ),
        patch("app.agent.core.record_llm_usage_event", new=AsyncMock()),
    ):
        with pytest.raises(ApprovalRequired):
            await run_agent(
                [{"role": "user", "content": "write the note"}],
                _context(),
                event_emitter=emitter,
            )

    event_types = [event.type for event in events]
    assert event_types[-1] == AgentEventType.APPROVAL_REQUIRED
    assert AgentEventType.RUN_FAILED not in event_types
    assert events[-1].payload["tool_name"] == "write_file"


@pytest.mark.asyncio
async def test_stream_agent_emits_visible_deltas_and_terminal_event():
    events = []

    async def _collect(event):
        events.append(event)

    emitter = AgentEventEmitter(callback=_collect, session_id=42)
    with (
        patch("app.agent.core.get_tools_for_user", return_value=[]),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(
                side_effect=[
                    _response(content="Streamed answer"),
                    _stream_response("Streamed answer"),
                ]
            ),
        ),
        patch("app.agent.core.record_llm_usage_event", new=AsyncMock()),
    ):
        result = "".join(
            [
                chunk
                async for chunk in stream_agent(
                    [{"role": "user", "content": "hello"}],
                    _context(),
                    event_emitter=emitter,
                )
            ]
        )

    assert result == "Streamed answer"
    visible_deltas = [
        event.payload["content"]
        for event in events
        if event.type == AgentEventType.TEXT_DELTA and not event.payload["provisional"]
    ]
    assert "".join(visible_deltas) == result
    assert events[-1].type == AgentEventType.RUN_COMPLETED
    assert events[-1].payload["content_chars"] == len(result)


@pytest.mark.asyncio
async def test_stream_agent_emits_cancelled_when_consumer_closes_stream():
    events = []

    async def _collect(event):
        events.append(event)

    emitter = AgentEventEmitter(callback=_collect, session_id=42)
    with (
        patch("app.agent.core.get_tools_for_user", return_value=[]),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(
                side_effect=[
                    _response(content="A response long enough to chunk."),
                    _stream_response("A response long enough to chunk."),
                ]
            ),
        ),
        patch("app.agent.core.record_llm_usage_event", new=AsyncMock()),
    ):
        stream = stream_agent(
            [{"role": "user", "content": "hello"}],
            _context(),
            event_emitter=emitter,
        )
        assert await anext(stream)
        await stream.aclose()

    assert events[-1].type == AgentEventType.RUN_CANCELLED
    assert AgentEventType.RUN_COMPLETED not in [event.type for event in events]


@pytest.mark.asyncio
async def test_run_agent_emits_failure_without_swallowing_exception():
    events = []

    async def _collect(event):
        events.append(event)

    emitter = AgentEventEmitter(callback=_collect, session_id=42)
    with (
        patch("app.agent.core.get_tools_for_user", return_value=[]),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
        ),
    ):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await run_agent(
                [{"role": "user", "content": "hello"}],
                _context(),
                event_emitter=emitter,
            )

    assert events[-1].type == AgentEventType.RUN_FAILED
    assert events[-1].payload["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_streaming_and_non_streaming_share_repeated_tool_convergence(monkeypatch):
    monkeypatch.setattr(settings, "agent_repeated_tool_signature_threshold", 1)
    tool_call = SimpleNamespace(
        id="call_repeat",
        type="function",
        function=SimpleNamespace(name="find_files", arguments="{}"),
    )
    tool_result = [
        {"role": "tool", "tool_call_id": "call_repeat", "content": "same result"}
    ]

    async def _exercise(*, streaming: bool) -> tuple[str, int, int]:
        completion = AsyncMock(return_value=_response(tool_calls=[tool_call]))
        dispatch = AsyncMock(return_value=tool_result)
        with (
            patch(
                "app.agent.core.get_tools_for_user",
                return_value=[{"type": "function", "function": {"name": "find_files"}}],
            ),
            patch("app.agent.core.litellm.acompletion", new=completion),
            patch("app.agent.core.dispatch_tool_calls", new=dispatch),
            patch("app.agent.core.record_llm_usage_event", new=AsyncMock()),
            patch("app.agent.core._native_agent_streaming_enabled", return_value=False),
        ):
            if streaming:
                result = "".join(
                    [
                        chunk
                        async for chunk in stream_agent(
                            [{"role": "user", "content": "Keep searching."}],
                            _context(),
                            mode="task",
                        )
                    ]
                )
            else:
                result = await run_agent(
                    [{"role": "user", "content": "Keep searching."}],
                    _context(),
                    mode="task",
                )
        return result, completion.await_count, dispatch.await_count

    plain_result, plain_model_turns, plain_tool_turns = await _exercise(streaming=False)
    stream_result, stream_model_turns, stream_tool_turns = await _exercise(streaming=True)

    assert stream_result == plain_result
    assert "repeating the same tool cycle" in plain_result.lower()
    assert (plain_model_turns, plain_tool_turns) == (2, 2)
    assert (stream_model_turns, stream_tool_turns) == (2, 2)
