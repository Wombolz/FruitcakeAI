from __future__ import annotations

import json

import pytest

from app.agent.model_stream import (
    ModelTurnAccumulator,
    close_provider_stream,
    iter_model_stream_events,
)
from app.agent.litellm_ollama_patch import apply_litellm_ollama_stream_patch


def _ollama_parser():
    from litellm.llms.ollama.chat.transformation import OllamaChatCompletionResponseIterator

    apply_litellm_ollama_stream_patch()
    return OllamaChatCompletionResponseIterator(streaming_response=iter([]), sync_stream=True)


def _ollama_chunk(message, *, done=False):
    return {
        "model": "test",
        "message": {"role": "assistant", "content": "", **message},
        "done": done,
        "prompt_eval_count": 12 if done else 0,
        "eval_count": 4 if done else 0,
    }


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


@pytest.mark.asyncio
@pytest.mark.parametrize("messages, reasoning, content", [
    ([{"content": "<think>private analysis</think>Visible answer"}], "private analysis", "Visible answer"),
    ([{"content": part} for part in ["<think>private ", "analysis", "</think>Visible answer"]],
     "private analysis", "Visible answer"),
    ([{"content": part} for part in "<think>private analysis</think>Visible answer"],
     "private analysis", "Visible answer"),
    ([{"thinking": "private analysis", "content": "Visible answer"}], "private analysis", "Visible answer"),
    ([{"thinking": "private "}, {"thinking": "analysis"}, {"content": "Visible answer"}],
     "private analysis", "Visible answer"),
    ([{"content": "<think>unfinished private analysis"}], "unfinished private analysis", ""),
    ([{"content": "Visible answer<"}], "", "Visible answer<"),
])
async def test_real_ollama_parser_preserves_reasoning_boundaries(messages, reasoning, content):
    parser = _ollama_parser()
    chunks = [
        parser.chunk_parser(_ollama_chunk(message, done=index == len(messages) - 1))
        for index, message in enumerate(messages)
    ]
    result = await _accumulate(*chunks)

    assert result.reasoning_content == reasoning
    assert result.content == content
    assert result.usage == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}


@pytest.mark.asyncio
async def test_real_ollama_parser_keeps_complete_calls_separate_across_chunks():
    parser = _ollama_parser()
    first = _ollama_chunk({"tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "notes/a.md"}}},
        {"function": {"name": "list_tasks", "arguments": {}}},
    ]})
    second = _ollama_chunk({"tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "notes/b.md"}}},
        {"function": {"name": "list_tasks", "arguments": {}}},
    ]}, done=True)
    snapshot = json.dumps([first, second])
    result = await _accumulate(parser.chunk_parser(first), parser.chunk_parser(second))

    assert json.dumps([first, second]) == snapshot
    assert [call["function"]["name"] for call in result.tool_calls] == [
        "read_file", "list_tasks", "read_file", "list_tasks",
    ]
    assert [json.loads(call["function"]["arguments"]) for call in result.tool_calls] == [
        {"path": "notes/a.md"}, {}, {"path": "notes/b.md"}, {},
    ]
    assert len({call["id"] for call in result.tool_calls}) == 4


@pytest.mark.asyncio
async def test_real_ollama_parser_keeps_reasoning_state_per_stream():
    first, second = _ollama_parser(), _ollama_parser()
    private = first.chunk_parser(_ollama_chunk({"content": "<think>private"}))
    public = second.chunk_parser(_ollama_chunk({"content": "Visible answer"}, done=True))
    closed = first.chunk_parser(_ollama_chunk({"content": "</think>First answer"}, done=True))

    assert (await _accumulate(public)).content == "Visible answer"
    result = await _accumulate(private, closed)
    assert result.reasoning_content == "private"
    assert result.content == "First answer"


def test_ollama_stream_patch_is_idempotent():
    _ollama_parser()
    assert apply_litellm_ollama_stream_patch() is False
