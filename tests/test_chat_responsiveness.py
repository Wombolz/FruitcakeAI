from __future__ import annotations

from unittest.mock import patch

from app.agent.context import UserContext
from app.api.chat import (
    _apply_tool_overrides,
    _is_simple_chat_tool_relevant,
    _record_chat_stage_timing,
    _should_apply_simple_chat_memory,
    _trim_simple_chat_history,
)
from app.config import settings
from app.metrics import _Metrics


def test_trim_simple_chat_history_keeps_recent_messages_only():
    history = [{"role": "user", "content": f"m{i}"} for i in range(12)]
    with patch.object(settings, "chat_simple_history_max_messages", 6):
        trimmed = _trim_simple_chat_history(history)
    assert len(trimmed) == 6
    assert trimmed[0]["content"] == "m6"
    assert trimmed[-1]["content"] == "m11"


def test_should_apply_simple_chat_memory_for_referential_prompt():
    history = [{"role": "user", "content": "hi"}]
    assert _should_apply_simple_chat_memory("Remember my usual coffee order", history) is True


def test_should_skip_simple_chat_memory_for_short_generic_prompt():
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert _should_apply_simple_chat_memory("Tell me a joke", history) is False


def test_is_simple_chat_tool_relevant_for_calendar_prompt():
    assert _is_simple_chat_tool_relevant("What do I have on my calendar this week?", library_intent=False) is True
    assert _is_simple_chat_tool_relevant("Tell me a joke", library_intent=False) is False


def test_apply_tool_overrides_supports_block_all_wildcard():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    _apply_tool_overrides(user_context, allowed_tools=None, blocked_tools=["*"])
    assert "create_memory" in user_context.blocked_tools
    assert "search_library" in user_context.blocked_tools


def test_record_chat_stage_timing_records_elapsed_ms():
    stage_timings = {}
    with patch("app.api.chat.metrics", new=_Metrics()):
        with patch("app.api.chat.time.perf_counter", return_value=10.025):
            _record_chat_stage_timing(stage_timings, "history_load", 10.0)
        assert stage_timings["history_load"] == 25.0
