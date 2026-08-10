from __future__ import annotations

import json

import pytest

from app.agent.model_stream import (
    ModelTurnAccumulator,
    close_provider_stream,
    iter_model_stream_events,
)


async def _chunks(*items):
    for item in items:
        yield item


async def _accumulate(*chunks):
    accumulator = ModelTurnAccumulator(request_id="test")
    async for event in iter_model_stream_events(_chunks(*chunks)):
        accumulator.add(event)
    return accumulator.finish()


@pytest.mark.asyncio
async def test_ollama_complete_unindexed_tool_calls_are_canonicalized():
    result = await _accumulate(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"function": {"name": "read_file", "arguments": {"path": "notes/a.md"}}},
                            {"function": {"name": "list_tasks", "arguments": {}}},
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 14, "total_tokens": 134},
        }
    )

    assert [call["function"]["name"] for call in result.tool_calls] == ["read_file", "list_tasks"]
    assert result.tool_calls[0]["id"] == "call_stream_test_0"
    assert result.tool_calls[1]["id"] == "call_stream_test_1"
    assert all(call["type"] == "function" for call in result.tool_calls)
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"path": "notes/a.md"}
    assert json.loads(result.tool_calls[1]["function"]["arguments"]) == {}
    assert result.usage == {"prompt_tokens": 120, "completion_tokens": 14, "total_tokens": 134}


@pytest.mark.asyncio
async def test_openai_fragmented_tool_call_and_terminal_usage_are_accumulated():
    result = await _accumulate(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_provider_1",
                                "function": {"name": "search_", "arguments": "{\"query\":\"fruit"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "library", "arguments": "cake\"}"}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [],
            "usage": {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58},
        },
    )

    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        {
            "id": "call_provider_1",
            "type": "function",
            "function": {"name": "search_library", "arguments": '{"query":"fruitcake"}'},
        }
    ]
    assert result.usage == {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58}


@pytest.mark.asyncio
async def test_unindexed_provider_id_can_arrive_after_first_fragment():
    result = await _accumulate(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"function": {"name": "read_file", "arguments": '{"path":"notes/'}}
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"id": "call_late", "function": {"arguments": 'a.md"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )

    assert result.tool_calls[0]["id"] == "call_late"
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"path": "notes/a.md"}


@pytest.mark.asyncio
async def test_zero_usage_chunks_do_not_create_usage_result():
    result = await _accumulate(
        {
            "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
        {
            "choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
    )

    assert result.content == "Hello world"
    assert result.usage is None


@pytest.mark.asyncio
async def test_split_think_tags_never_enter_visible_content():
    result = await _accumulate(
        {"choices": [{"delta": {"content": "<thi"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "nk>private analysis</th"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "ink>Visible answer"}, "finish_reason": "stop"}]},
    )

    assert result.reasoning_content == "private analysis"
    assert result.content == "Visible answer"


@pytest.mark.asyncio
async def test_close_provider_stream_awaits_aclose():
    class _ProviderStream:
        closed = False

        async def aclose(self):
            self.closed = True

    stream = _ProviderStream()
    await close_provider_stream(stream)
    assert stream.closed is True
