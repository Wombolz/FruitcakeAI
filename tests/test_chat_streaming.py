from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.context import UserContext
from app.agent.core import build_local_document_summary_digest, run_agent, stream_agent
from app.agent.tools import _soften_unsupported_summary_totals
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
async def test_run_agent_preemptively_disables_tools_for_configured_text_only_local_model(monkeypatch):
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    monkeypatch.setattr(settings, "local_tool_text_only_models", "ollama_chat/qwen36-hauhau:q4km")

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "read_file"}}]),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(return_value=_fake_response(content="Text-only local response from existing context.")),
        ) as mock_completion,
    ):
        result = await run_agent(
            [{"role": "user", "content": "Tell me about my latest repo map report in the workspace."}],
            user_context,
            mode="chat_orchestrated",
            model_override="ollama_chat/qwen36-hauhau:q4km",
            stage="chat_complex",
        )

    assert result == "Text-only local response from existing context."
    assert mock_completion.await_count == 1
    assert mock_completion.await_args.kwargs.get("tools") is None


@pytest.mark.asyncio
async def test_run_agent_falls_back_to_text_only_when_local_model_does_not_support_tools():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    unsupported_error = RuntimeError(
        'litellm.APIConnectionError: Ollama_chatException - {"error":"registry.ollama.ai/library/qwen36-hauhau:q4km does not support tools"}'
    )

    async def _acompletion(**kwargs):
        if kwargs.get("tools"):
            raise unsupported_error
        return _fake_response(content="This model can answer from the current context without tools.")

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{"function": {"name": "read_file"}}]),
        patch("app.agent.core.litellm.acompletion", side_effect=_acompletion) as mock_completion,
    ):
        result = await run_agent(
            [{"role": "user", "content": "Tell me about my latest repo map report in the workspace."}],
            user_context,
            mode="chat_orchestrated",
            model_override="ollama_chat/qwen36-hauhau:q4km",
            stage="chat_complex",
        )

    assert result == "This model can answer from the current context without tools."
    assert mock_completion.await_count == 2
    assert mock_completion.await_args_list[0].kwargs["tools"] is not None
    assert mock_completion.await_args_list[1].kwargs.get("tools") is None


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
    assert fallback_call.kwargs["offered_tool_count"] == 1
    assert fallback_call.kwargs["offered_has_browser_tools"] is False
    assert fallback_call.kwargs["history_preview"]
    assert "failed to parse JSON" in fallback_call.kwargs["error_preview"]


@pytest.mark.asyncio
async def test_run_agent_investigation_filter_can_drop_browser_tools_for_qwen(monkeypatch):
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    monkeypatch.setattr(settings, "local_tool_investigation_enabled", True)
    monkeypatch.setattr(settings, "local_tool_investigation_drop_browser_tools", True)
    monkeypatch.setattr(settings, "local_tool_investigation_max_tools", 0)

    with (
        patch(
            "app.agent.core.get_tools_for_user",
            return_value=[
                {"function": {"name": "search_library"}},
                {"function": {"name": "browser_navigate"}},
                {"function": {"name": "browser_click"}},
            ],
        ),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(return_value=_fake_response(content="Search-only response"))),
        patch("app.agent.core.log.warning") as mock_warning,
    ):
        result = await run_agent(
            [{"role": "user", "content": "Find the note in my library."}],
            user_context,
            mode="chat_orchestrated",
            model_override="ollama_chat/qwen3.6:35b",
            stage="chat_complex",
        )

    assert result == "Search-only response"
    investigation_call = None
    for call in mock_warning.call_args_list:
        if call.args and call.args[0] == "LLM local_tool_investigation_filter_applied":
            investigation_call = call
            break
    assert investigation_call is not None
    assert investigation_call.kwargs["investigation_reasons"] == ["drop_browser_tools"]
    assert investigation_call.kwargs["raw_tool_surface"]["offered_tool_count"] == 3
    assert investigation_call.kwargs["raw_tool_surface"]["offered_browser_tool_count"] == 2
    assert investigation_call.kwargs["effective_tool_surface"]["offered_tools"] == ["search_library"]


@pytest.mark.asyncio
async def test_run_agent_investigation_filter_can_cap_local_qwen_tool_count(monkeypatch):
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    monkeypatch.setattr(settings, "local_tool_investigation_enabled", True)
    monkeypatch.setattr(settings, "local_tool_investigation_drop_browser_tools", False)
    monkeypatch.setattr(settings, "local_tool_investigation_max_tools", 2)

    with (
        patch(
            "app.agent.core.get_tools_for_user",
            return_value=[
                {"function": {"name": "search_library"}},
                {"function": {"name": "list_library_documents"}},
                {"function": {"name": "summarize_document"}},
            ],
        ),
        patch(
            "app.agent.core.litellm.acompletion",
            new=AsyncMock(return_value=_fake_response(content="Capped tool response")),
        ) as mock_completion,
        patch("app.agent.core.log.warning") as mock_warning,
    ):
        result = await run_agent(
            [{"role": "user", "content": "Tell me about my documents."}],
            user_context,
            mode="chat_orchestrated",
            model_override="ollama_chat/qwen3.6:35b",
            stage="chat_complex",
        )

    assert result == "Capped tool response"
    tool_names = [tool.get("function", {}).get("name") for tool in (mock_completion.await_args.kwargs.get("tools") or [])]
    assert tool_names == ["search_library", "list_library_documents"]
    investigation_call = None
    for call in mock_warning.call_args_list:
        if call.args and call.args[0] == "LLM local_tool_investigation_filter_applied":
            investigation_call = call
            break
    assert investigation_call is not None
    assert investigation_call.kwargs["investigation_reasons"] == ["cap_tools:2"]


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


@pytest.mark.asyncio
async def test_run_agent_compacts_large_document_summary_and_disables_tools_for_local_followup():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    large_summary = (
        "**Summary of 'Agents of Chaos.pdf' (67 total sections):**\n"
        "_Note: This document has 67 sections. The summary covers 64 evenly-spaced samples from throughout._\n\n"
        + "\n".join(f"- Finding {index}: {'detail ' * 25}" for index in range(1, 12))
    )
    tool_calls = [
        SimpleNamespace(
            id="call_sum_1",
            type="function",
            function=SimpleNamespace(name="summarize_document", arguments='{"document_name":"Agents of Chaos.pdf"}'),
        )
    ]

    async def _acompletion(**kwargs):
        if _acompletion.calls == 0:
            _acompletion.calls += 1
            return _fake_response(tool_calls=tool_calls)
        _acompletion.second_kwargs = kwargs
        return _fake_response(content="Here is the saved summary in plain text.")

    _acompletion.calls = 0
    _acompletion.second_kwargs = None

    with (
        patch(
            "app.agent.core.get_tools_for_user",
            return_value=[
                {"function": {"name": "summarize_document"}},
                {"function": {"name": "search_library"}},
            ],
        ),
        patch(
            "app.agent.core.dispatch_tool_calls",
            new=AsyncMock(
                return_value=[{"role": "tool", "tool_call_id": "call_sum_1", "content": large_summary}]
            ),
        ),
        patch("app.agent.core.litellm.acompletion", side_effect=_acompletion),
    ):
        result = await run_agent(
            [{"role": "user", "content": "Summarize the agents of chaos document in my library."}],
            user_context,
            mode="chat_orchestrated",
            model_override="ollama_chat/qwen3.6:35b",
            stage="chat_complex",
        )

    assert result == "Here is the saved summary in plain text."
    assert _acompletion.second_kwargs is not None
    assert _acompletion.second_kwargs.get("tools") is None
    message_texts = [message.get("content", "") for message in _acompletion.second_kwargs["messages"]]
    assert any("Document summary evidence digest." in text for text in message_texts)
    assert any("- Major sections:" in text for text in message_texts)
    assert any("- Key findings:" in text for text in message_texts)
    assert any("- Coverage note:" in text for text in message_texts)
    assert any("source of truth" in text for text in message_texts)
    assert any("avoid unsupported specifics" in text for text in message_texts)
    assert any("if a detail is missing or unclear" in text for text in message_texts)


def test_build_local_document_summary_digest_is_structured_and_filters_recommendations():
    summary = (
        "**Summary of 'Agents of Chaos.pdf' (67 total sections):**\n"
        "_Note: This document has 67 sections. The summary covers 64 evenly-spaced samples from throughout._\n\n"
        "### Major sections\n"
        "- Election interference timeline\n"
        "- Social media operations\n"
        "### Key findings\n"
        "- Russian actors coordinated influence operations across multiple platforms.\n"
        "- The report traces escalation across the 2016 cycle.\n"
        "- Recommendation: the team should build a follow-up workflow.\n"
        "### Caveats\n"
        "- Some conclusions depend on sampled sections rather than every page.\n"
    )

    digest = build_local_document_summary_digest(summary)

    assert "Document summary evidence digest." in digest
    assert "- Document: Agents of Chaos.pdf" in digest
    assert "- Total sections: 67" in digest
    assert "- Coverage note: Note: This document has 67 sections." in digest
    assert "- Major sections:" in digest
    assert "Election interference timeline" in digest
    assert "- Key findings:" in digest
    assert "Russian actors coordinated influence operations" in digest
    assert "- Caveats:" in digest
    assert "sampled sections" in digest
    assert "Recommendation:" not in digest


def test_soften_unsupported_summary_totals_preserves_supported_specifics():
    source_text = (
        "We report an exploratory red-teaming study of autonomous language-model-powered agents "
        "deployed in a live laboratory environment with persistent memory, email accounts, Discord access, "
        "file systems, and shell execution. Over a two-week period, twenty AI researchers interacted with them. "
        "We use Claude Opus and Kimi K2.5 as backbone models. We deploy each one to an isolated virtual machine on Fly.io. "
        "The next section presents ten representative case studies drawn from this two-week period."
    )
    summary = (
        "### Major sections\n"
        "- Setup & Infrastructure: OpenClaw agents deployed on Fly.io VMs backed by Claude Opus and Kimi K2.5.\n"
        "### Key findings\n"
        "- Two-week exploratory red-teaming by 20 AI researchers.\n"
        "- The study identified 11 case studies and additional tests.\n"
    )

    adjusted = _soften_unsupported_summary_totals(summary, source_text)

    assert "20 AI researchers" in adjusted
    assert "Fly.io VMs" in adjusted
    assert "Claude Opus and Kimi K2.5" in adjusted
    assert "11 case studies" not in adjusted
    assert "representative case studies" in adjusted or "multiple case studies" in adjusted
