from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.context import UserContext
from app.agent.core import run_agent, stream_agent


class _FakeMessage:
    def __init__(self, *, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none: bool = True):
        payload = {"content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        return payload


def _fake_response(*, content: str = "", tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=_FakeMessage(content=content, tool_calls=tool_calls),
                finish_reason="tool_calls" if tool_calls else "stop",
            )
        ]
    )


async def _fake_stream(*parts: str):
    for part in parts:
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=part),
                    finish_reason=None,
                )
            ]
        )


@pytest.mark.asyncio
async def test_stream_agent_uses_true_stream_for_simple_final_turn():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")

    async def _acompletion(**kwargs):
        if kwargs.get("stream"):
            return _fake_stream("Hello", " world")
        return _fake_response(content="Hello world")

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[]),
        patch("app.agent.core.litellm.acompletion", side_effect=_acompletion) as mock_completion,
    ):
        chunks = [chunk async for chunk in stream_agent([{"role": "user", "content": "hi"}], user_context)]

    assert chunks == ["Hello", " world"]
    assert mock_completion.await_count == 2
    first = mock_completion.await_args_list[0].kwargs
    second = mock_completion.await_args_list[1].kwargs
    assert first["stream"] is False
    assert second["stream"] is True
    assert second.get("tools") is None


@pytest.mark.asyncio
async def test_stream_agent_skips_second_stream_pass_for_local_ollama_chat_model():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[]),
        patch("app.agent.core.litellm.acompletion", new=AsyncMock(return_value=_fake_response(content="Hello world"))) as mock_completion,
    ):
        chunks = [
            chunk
            async for chunk in stream_agent(
                [{"role": "user", "content": "hi"}],
                user_context,
                model_override="ollama_chat/qwen3.6:35b",
            )
        ]

    assert chunks == ["Hello world"]
    assert mock_completion.await_count == 1
    assert mock_completion.await_args.kwargs["stream"] is False


@pytest.mark.asyncio
async def test_stream_agent_falls_back_to_text_only_when_local_tool_json_parse_fails():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    parse_error = RuntimeError(
        'litellm.APIConnectionError: Ollama_chatException - {"error":"failed to parse JSON: invalid character \'{\' looking for beginning of object key string"}'
    )

    async def _acompletion(**kwargs):
        if kwargs.get("tools"):
            raise parse_error
        return _fake_response(content="Using the existing context only, here is the answer.")

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "read_file"}}]),
        patch("app.agent.core.litellm.acompletion", side_effect=_acompletion) as mock_completion,
    ):
        chunks = [
            chunk
            async for chunk in stream_agent(
                [{"role": "user", "content": "What is the weather in Atlanta today?"}],
                user_context,
                model_override="ollama_chat/qwen3.6:35b",
                stage="chat_complex",
            )
        ]

    assert "".join(chunks) == "Using the existing context only, here is the answer."
    assert mock_completion.await_count == 2
    assert mock_completion.await_args_list[0].kwargs["tools"] is not None
    assert mock_completion.await_args_list[1].kwargs.get("tools") is None


@pytest.mark.asyncio
async def test_run_agent_falls_back_to_text_only_when_local_tool_json_parse_fails():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    parse_error = RuntimeError(
        'litellm.APIConnectionError: Ollama_chatException - {"error":"failed to parse JSON: invalid character \'l\' after object key"}'
    )

    async def _acompletion(**kwargs):
        if kwargs.get("tools"):
            raise parse_error
        return _fake_response(content="I can answer from the existing workspace context without calling tools.")

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "read_file"}}]),
        patch("app.agent.core.litellm.acompletion", side_effect=_acompletion) as mock_completion,
    ):
        result = await run_agent(
            [{"role": "user", "content": "What is the weather in Atlanta today?"}],
            user_context,
            mode="chat_orchestrated",
            model_override="ollama_chat/qwen3.6:35b",
            stage="chat_complex",
        )

    assert result == "I can answer from the existing workspace context without calling tools."
    assert mock_completion.await_count == 2
    assert mock_completion.await_args_list[0].kwargs["tools"] is not None
    assert mock_completion.await_args_list[1].kwargs.get("tools") is None


@pytest.mark.asyncio
async def test_run_agent_preemptively_disables_tools_for_qwen_workspace_followup_guardrail():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "read_file"}}]),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(return_value=_fake_response(content="I cannot confirm the workspace file contents from the current context.")),
        ) as mock_completion,
    ):
        result = await run_agent(
            [{"role": "user", "content": "Tell me about my latest repo map report in the workspace."}],
            user_context,
            mode="chat_orchestrated",
            model_override="ollama_chat/qwen3.6:35b",
            stage="chat_complex",
        )

    assert result == "I cannot confirm the workspace file contents from the current context."
    assert mock_completion.await_count == 1
    assert mock_completion.await_args.kwargs.get("tools") is None


@pytest.mark.asyncio
async def test_run_agent_restricts_qwen_manual_fact_lookup_to_search_tools():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")

    with (
        patch(
            "app.agent.core.get_tools_for_user",
            return_value=[
                {"function": {"name": "search_library"}},
                {"function": {"name": "summarize_document"}},
                {"function": {"name": "list_library_documents"}},
            ],
        ),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(return_value=_fake_response(content="The manual says the default IP address is 192.168.0.10.")),
        ) as mock_completion,
    ):
        result = await run_agent(
            [{"role": "user", "content": "What does the AW-UE160P manual say the default IP address is for the camera?"}],
            user_context,
            mode="chat_orchestrated",
            model_override="ollama_chat/qwen3.6:35b",
            stage="chat_complex",
        )

    assert result == "The manual says the default IP address is 192.168.0.10."
    allowed_tools = mock_completion.await_args.kwargs.get("tools") or []
    tool_names = [tool.get("function", {}).get("name") for tool in allowed_tools]
    assert "search_library" in tool_names
    assert "list_library_documents" in tool_names
    assert "summarize_document" not in tool_names


@pytest.mark.asyncio
async def test_run_agent_does_not_disable_tools_for_cloud_workspace_followup():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "read_file"}}]),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(return_value=_fake_response(content="Cloud model response")),
        ) as mock_completion,
    ):
        result = await run_agent(
            [{"role": "user", "content": "Tell me about my latest repo map report in the workspace."}],
            user_context,
            mode="chat_orchestrated",
            model_override="gpt-5-mini",
            stage="chat_complex",
        )

    assert result == "Cloud model response"
    assert mock_completion.await_count == 1
    assert mock_completion.await_args.kwargs.get("tools") is not None


@pytest.mark.asyncio
async def test_run_agent_logs_structured_local_tool_parse_diagnostics_after_successful_tool_turn():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    parse_error = RuntimeError(
        'litellm.APIConnectionError: Ollama_chatException - {"error":"failed to parse JSON: invalid character \'{\' looking for beginning of object key string"}'
    )
    tool_calls = [
        SimpleNamespace(
            id="call_1",
            type="function",
            function=SimpleNamespace(name="get_weather", arguments='{"location":"Atlanta, GA"}'),
        )
    ]

    async def _acompletion(**kwargs):
        if _acompletion.calls == 0:
            _acompletion.calls += 1
            return _fake_response(tool_calls=tool_calls)
        if kwargs.get("tools"):
            raise parse_error
        return _fake_response(content="Using prior weather evidence only.")

    _acompletion.calls = 0

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "get_weather"}}]),
        patch(
            "app.agent.core.dispatch_tool_calls",
            new=AsyncMock(
                return_value=[{"role": "tool", "tool_call_id": "call_1", "content": "Atlanta weather is 82F and sunny."}]
            ),
        ),
        patch("app.agent.core.litellm.acompletion", side_effect=_acompletion) as mock_completion,
        patch("app.agent.core.log.warning") as mock_warning,
    ):
        result = await run_agent(
            [{"role": "user", "content": "What is the weather in Atlanta today?"}],
            user_context,
            mode="chat_orchestrated",
            model_override="ollama_chat/qwen3.6:35b",
            stage="chat_complex",
        )

    assert result == "Using prior weather evidence only."
    assert mock_completion.await_count == 3
    fallback_call = None
    for call in mock_warning.call_args_list:
        if call.args and call.args[0] == "LLM local_tool_json_parse_fallback":
            fallback_call = call
            break
    assert fallback_call is not None
    assert fallback_call.kwargs["tool_failure_phase"] == "after_successful_tool_turn"
    assert fallback_call.kwargs["prompt_class"] == "general"
    assert fallback_call.kwargs["offered_tools"] == ["get_weather"]
    assert fallback_call.kwargs["history_preview"]
    assert "failed to parse JSON" in fallback_call.kwargs["error_preview"]


@pytest.mark.asyncio
async def test_stream_agent_keeps_tool_turns_internal_before_streaming_final():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    tool_calls = [
        SimpleNamespace(
            id="call_1",
            type="function",
            function=SimpleNamespace(name="search_library", arguments="{}"),
        )
    ]

    async def _acompletion(**kwargs):
        if kwargs.get("stream"):
            return _fake_stream("Done", ".")
        if _acompletion.calls == 0:
            _acompletion.calls += 1
            return _fake_response(tool_calls=tool_calls)
        return _fake_response(content="Done.")

    _acompletion.calls = 0

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "search_library"}}]),
        patch("app.agent.core.dispatch_tool_calls", new=AsyncMock(return_value=[{"role": "tool", "content": "ok"}])) as mock_dispatch,
        patch("app.agent.core.litellm.acompletion", side_effect=_acompletion) as mock_completion,
    ):
        chunks = [chunk async for chunk in stream_agent([{"role": "user", "content": "hi"}], user_context)]

    assert chunks == ["Done", "."]
    assert mock_dispatch.await_count == 1
    assert mock_completion.await_count == 3
    assert mock_completion.await_args_list[-1].kwargs["stream"] is True


@pytest.mark.asyncio
async def test_stream_agent_stops_after_failed_delete_event_tool_result():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    tool_calls = [
        SimpleNamespace(
            id="call_1",
            type="function",
            function=SimpleNamespace(name="delete_event", arguments='{"event_id":"evt_123","confirm":true}'),
        )
    ]

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "delete_event"}}]),
        patch(
            "app.agent.core.dispatch_tool_calls",
            new=AsyncMock(
                return_value=[
                    {
                        "role": "tool",
                        "content": "Failed to verify deletion for event 'evt_123'. The calendar provider did not confirm that the event was removed.",
                    }
                ]
            ),
        ) as mock_dispatch,
        patch("app.agent.core.litellm.acompletion", new=AsyncMock(return_value=_fake_response(tool_calls=tool_calls))) as mock_completion,
    ):
        chunks = [chunk async for chunk in stream_agent([{"role": "user", "content": "delete it"}], user_context)]

    assert chunks == [
        "Failed to verify deletion for event 'evt_123'. The calendar provider did not confirm that the event was removed."
    ]
    assert mock_dispatch.await_count == 1
    assert mock_completion.await_count == 1
