import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.agent.chat_content import build_assistant_content_blocks, news_evidence_from_history
from app.agent.chat_validation import validate_chat_response
from app.agent.context import UserContext
from app.agent.core import run_agent
from app.api.chat import _build_assistant_message_metadata, _execute_chat_turn
from app.config import settings
from app.db.models import ChatMessage, LLMUsageEvent, User
from app.llm_usage import bind_llm_usage_context, record_llm_usage_event, reset_llm_usage_context
from tests.conftest import TestSessionLocal


RSS_RESULT = """Recent feed items (2):
[1] First canonical headline
    Source: NPR
    Summary: First feed summary.
    URL: https://npr.org/news/first
[2] Second canonical headline
    Source: BBC
    Summary: Second feed summary.
    URL: https://bbc.com/news/second
"""
NEWS = """### News
- **First headline**: First inline summary.
  [NPR](https://npr.org/news/first)
- **Second headline**: Second inline summary.
  [BBC](https://bbc.com/news/second)
"""


def _rss_history():
    return [
        {"role": "assistant", "tool_calls": [{
            "id": "rss_1", "function": {"name": "search_my_feeds", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "rss_1", "content": RSS_RESULT},
    ]


@pytest.mark.parametrize("separator", [":", ";", " -"])
def test_news_card_preserves_every_inline_headline_and_summary(separator):
    content = NEWS.replace("**:", f"**{separator}")
    blocks = build_assistant_content_blocks(content, [{"tool": "search_my_feeds"}])
    items = blocks[0]["sections"][0]["items"]
    assert [item["title"] for item in items] == ["First headline", "Second headline"]
    assert [item["summary"] for item in items] == ["First inline summary.", "Second inline summary."]
    assert [len(item["sources"]) for item in items] == [1, 1]
    assert blocks[0]["source_markdown"] == content.strip()


def test_followup_reuses_paired_rss_evidence_without_fabricating_current_activity():
    metadata = _build_assistant_message_metadata(
        handoff_metadata={}, executed_tools=[], content=NEWS, history=_rss_history(),
    )
    items = metadata["content_blocks"][0]["sections"][0]["items"]
    assert [item["title"] for item in items] == ["First canonical headline", "Second canonical headline"]
    assert "tool_calls" not in metadata
    assert "activity" not in metadata
    assert "evidence" not in metadata
    assert "artifacts" not in metadata


def test_mixed_headline_format_uses_complete_card_instead_of_partial_markdown():
    content = NEWS + "- Third headline without bold\n  [NPR](https://npr.org/news/third)\n"
    evidence = RSS_RESULT + "\n[3] Third canonical headline\n    URL: https://npr.org/news/third\n"
    block = build_assistant_content_blocks(content, [{"tool": "search_my_feeds", "result_summary": evidence}])[0]
    assert sum(len(section["items"]) for section in block["sections"]) == 3


@pytest.mark.parametrize("history", [
    [],
    [{"role": "user", "content": RSS_RESULT}],
    [{"role": "tool", "tool_call_id": "missing", "content": RSS_RESULT}],
    [{"role": "user", "tool_calls": [{"id": "rss_1", "function": {"name": "search_my_feeds"}}]},
     {"role": "tool", "tool_call_id": "rss_1", "content": RSS_RESULT}],
])
def test_followup_does_not_promote_untrusted_or_unpaired_news(history):
    assert news_evidence_from_history(history) == []
    assert not _build_assistant_message_metadata(
        handoff_metadata={}, executed_tools=[], content=NEWS, history=history,
    )


def test_prior_news_only_promotes_links_present_in_saved_rss_results():
    assert build_assistant_content_blocks(
        NEWS.replace("npr.org", "other.org").replace("bbc.com", "elsewhere.org"),
        prior_news_evidence=news_evidence_from_history(_rss_history()),
    ) == []
    assert len(news_evidence_from_history(_rss_history() * 20)) <= 4
    assert news_evidence_from_history(_rss_history() + [{"role": "user"}] * 100) == []


def test_prior_rss_evidence_is_character_bounded():
    history = []
    for index in range(8):
        pair = _rss_history()
        pair[0]["tool_calls"][0]["id"] = f"rss_{index}"
        pair[1].update(tool_call_id=f"rss_{index}", content="x" * 100_000)
        history.extend(pair)
    assert sum(len(record["result_summary"]) for record in news_evidence_from_history(history)) <= 96_000


@pytest.mark.asyncio
async def test_rest_followup_card_uses_persisted_rss_history(client, monkeypatch):
    await client.post("/auth/register", json={
        "username": "newsfollowup", "email": "newsfollowup@example.com", "password": "testpass123",
    })
    login = await client.post("/auth/login", json={"username": "newsfollowup", "password": "testpass123"})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created = await client.post("/chat/sessions", json={"title": "News"}, headers=headers)
    session_id = created.json()["id"]
    async with TestSessionLocal() as db:
        for message in _rss_history():
            db.add(ChatMessage(
                session_id=session_id, role=message["role"], content=message.get("content", ""),
                tool_calls=json.dumps(message["tool_calls"]) if "tool_calls" in message else None,
                tool_results=json.dumps({"tool_call_id": message["tool_call_id"]}) if "tool_call_id" in message else None,
            ))
        await db.commit()
    monkeypatch.setattr(settings, "chat_complexity_routing_enabled", False)
    with patch("app.api.chat.run_agent", new=AsyncMock(return_value=NEWS)):
        response = await client.post(
            f"/chat/sessions/{session_id}/messages", json={"content": "Only the news headlines"}, headers=headers,
        )
    assert response.status_code == 200
    metadata = response.json()["metadata"]
    assert metadata["content_blocks"][0]["type"] == "news_digest"
    assert "tool_calls" not in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["search_my_feeds", "create_artifact", "create_event"])
async def test_validation_repair_keeps_results_and_disables_tool_execution(tool, monkeypatch):
    monkeypatch.setattr(settings, "chat_validation_enabled", True)
    monkeypatch.setattr(settings, "chat_validation_retry_enabled", True)
    monkeypatch.setattr(settings, "chat_validation_retry_max_attempts", 1)
    record = {"tool": tool, "result_summary": RSS_RESULT, "is_error": False}
    with (
        patch("app.api.chat.run_agent", new=AsyncMock(side_effect=[
            "Here are today's headlines, with an explanation but missing citations.", NEWS,
        ])) as agent,
        patch("app.api.chat.get_tool_execution_records", return_value=[record]),
    ):
        reply = await _execute_chat_turn(
            [{"role": "user", "content": "Today's news"}],
            UserContext(user_id=1, username="tester", role="parent"),
            user_prompt="Today's news", mode="chat", model_override="gpt-4o",
            stage="chat_simple", enable_validation=True,
        )
    assert reply == NEWS
    assert agent.await_count == 2
    retry = agent.await_args_list[1]
    assert retry.kwargs["allow_tool_execution"] is False
    assert "https://npr.org/news/first" in str(retry.args[0])
    assert "Do not repeat any action" in str(retry.args[0])


@pytest.mark.asyncio
async def test_answer_only_repair_never_dispatches_even_if_model_returns_a_tool_call():
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="", tool_calls=[{"id": "repeat", "function": {"name": "create_artifact", "arguments": "{}"}}],
    ))])
    with (
        patch("app.agent.core.get_tools_for_user", return_value=[{
            "type": "function", "function": {"name": "create_artifact", "description": "Create", "parameters": {"type": "object", "properties": {}}},
        }]),
        patch("app.agent.core._acompletion_with_budget", new=AsyncMock(return_value=response)) as completion,
        patch("app.agent.core._execute_agent_tool_turn", new=AsyncMock()) as dispatch,
        patch("app.agent.core.record_llm_usage_event", new=AsyncMock()),
    ):
        reply = await run_agent(
            [{"role": "user", "content": "Repair the news answer"}],
            UserContext(user_id=1, username="tester", role="parent"),
            model_override="gpt-4o", allow_tool_execution=False,
        )
    dispatch.assert_not_awaited()
    assert not completion.await_args.kwargs.get("tools")
    assert "No additional tools were run" in reply


def _artifact_record(html):
    return {"tool": "create_artifact", "structured_content": {"artifact": {
        "type": "core.html", "schema_version": 1, "title": "News", "payload": {"content": html},
    }}}


def test_validation_counts_source_links_in_successful_sanitized_display_artifact():
    out = validate_chat_response(
        "Show news links", "I created a news display with the available source links.",
        [_artifact_record('<p><a href="https://npr.org/news/first">Read story</a></p>')],
    )
    assert not out.should_retry
    assert out.valid_urls == ["https://npr.org/news/first"]


@pytest.mark.parametrize("html", [
    '<script>https://npr.org/news/first</script>',
    '<style>a { content: "https://npr.org/news/first"; }</style>',
    '<a href="https://example.com/fake">Placeholder</a>',
    '<p>https://npr.org/news/first</p>',
])
def test_validation_does_not_count_nonvisible_or_placeholder_artifact_links(html):
    assert validate_chat_response(
        "Show news links", "I created a news display with the available source links.",
        [_artifact_record(html)],
    ).retry_reason == "missing_links"


def test_failed_artifact_does_not_supply_citations():
    record = _artifact_record('<a href="https://npr.org/news/first">Read</a>')
    record["is_error"] = True
    assert validate_chat_response("Show news", "Here is the latest news briefing for today.", [record]).should_retry


@pytest.mark.asyncio
@pytest.mark.parametrize("response_model,selected_model,expected", [
    ("ollama_chat/qwen3.6:35b", "ollama_chat/qwen3.6:35b", "ollama"),
    ("qwen3.6:35b", "ollama_chat/qwen3.6:35b", "ollama"),
    ("claude-sonnet", "anthropic/claude-sonnet", "anthropic"),
    ("gpt-4o", "gpt-4o", "openai"),
])
async def test_usage_provider_tracks_selected_model_not_global_backend(response_model, selected_model, expected, monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "openai")
    async with TestSessionLocal() as db:
        user = User(username="usage", email="usage@test.local", hashed_password="unused", role="parent")
        db.add(user)
        await db.commit()
        user_id = user.id
    response = {"model": response_model, "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
    token = bind_llm_usage_context(user_id=user_id, provider="openai")
    try:
        with (
            patch("app.llm_usage.AsyncSessionLocal", new=TestSessionLocal),
            patch("app.llm_usage.litellm.completion_cost", return_value=0.0),
        ):
            await record_llm_usage_event(response, model=selected_model)
    finally:
        reset_llm_usage_context(token)
    async with TestSessionLocal() as db:
        row = (await db.execute(select(LLMUsageEvent))).scalar_one()
    assert row.provider == expected
    assert row.model == response_model
