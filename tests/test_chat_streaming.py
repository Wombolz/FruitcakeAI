from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.context import UserContext
from app.agent.core import stream_agent


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
    assert "tools" not in second


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
