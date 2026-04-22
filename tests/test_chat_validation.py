from unittest.mock import AsyncMock, patch

import pytest

from app.agent.chat_validation import (
    build_chat_retry_instruction,
    should_validate_chat_response,
    validate_chat_response,
)
from app.agent.context import UserContext
from app.config import settings
from app.metrics import metrics


def test_validate_chat_response_requires_links_for_research_prompt():
    out = validate_chat_response(
        "Research top headlines with sources",
        "Here is a summary without any links.",
    )
    assert out.is_research_style is True
    assert out.should_retry is True
    assert out.retry_reason == "missing_links"


def test_validate_chat_response_flags_invalid_placeholder_links():
    out = validate_chat_response(
        "Find latest news and cite links",
        "Story one: [Read more](https://example.com/article)",
    )
    assert out.should_retry is True
    assert out.retry_reason == "invalid_links"
    assert out.invalid_urls
    assert "https://example.com/article" not in out.cleaned_content


def test_validate_chat_response_accepts_valid_research_links():
    out = validate_chat_response(
        "Research latest headlines and cite sources",
        "1. AP story: [AP](https://apnews.com/article/something)",
    )
    assert out.should_retry is False
    assert out.valid_urls == ["https://apnews.com/article/something"]


def test_validate_chat_response_flags_tool_call_leakage():
    out = validate_chat_response(
        "Check my rss sources for the latest news on Iran and cite sources",
        "I'll pull a timeline now. (to=functions.search_my_feeds_timeline armat) The tool name must be provided correctly.",
    )
    assert out.has_tool_call_leakage is True
    assert out.should_retry is True
    assert out.retry_reason == "tool_call_leakage"


def test_validate_chat_response_flags_fetch_narration_and_compacted_tool_leakage():
    out = validate_chat_response(
        "Give me the run down on the Ship Seizure from the 1st article",
        (
            "I'll fetch the full page now.\n"
            "Compacted tool result.\n"
            "Tool: fetch_page\n"
            "Tool call id: call_123\n"
        ),
    )
    assert out.has_tool_call_leakage is True
    assert out.should_retry is True
    assert out.retry_reason == "tool_call_leakage"


def test_should_validate_chat_response_enables_research_on_simple_path():
    assert should_validate_chat_response(
        user_prompt="Research the latest headlines on Iran and cite sources",
        effective_complex=False,
    ) is True
    assert should_validate_chat_response(
        user_prompt="Give me the run down on the Ship Seizure from the 1st article",
        effective_complex=False,
    ) is True
    assert should_validate_chat_response(
        user_prompt="Tell me more about the first story",
        effective_complex=False,
    ) is True
    assert should_validate_chat_response(
        user_prompt="Tell me a joke",
        effective_complex=False,
    ) is False


def test_build_retry_instruction_has_reason_specific_text():
    assert "internal tool-calling" in build_chat_retry_instruction("tool_call_leakage").lower()
    assert "too brief/empty" in build_chat_retry_instruction("empty_result").lower()
    assert "grounded sources" in build_chat_retry_instruction("missing_links").lower()
    assert "invalid/placeholder links" in build_chat_retry_instruction("invalid_links").lower()
    assert "calendar" in build_chat_retry_instruction("calendar_mutation_unconfirmed").lower()
    assert "task or watcher" in build_chat_retry_instruction("task_mutation_unconfirmed").lower()


def test_validate_chat_response_flags_unconfirmed_calendar_mutation_claim():
    out = validate_chat_response(
        "Create an event for next Wednesday at 12pm for lunch with Rod",
        "I've created an event titled 'Lunch with Rod' on your calendar for next Wednesday at 12 PM.",
        executed_tools=[],
    )
    assert out.mutation_unconfirmed is True
    assert out.should_retry is True
    assert out.retry_reason == "calendar_mutation_unconfirmed"


def test_validate_chat_response_accepts_confirmed_calendar_delete_claim():
    out = validate_chat_response(
        "Delete the lunch with Rod event from my calendar",
        "I've deleted the Lunch with Rod event from your calendar.",
        executed_tools=[
            {
                "tool": "delete_event",
                "result_summary": "Event deleted: 'Lunch with Rod' (evt_123)",
            }
        ],
    )
    assert out.mutation_unconfirmed is False
    assert out.should_retry is False


def test_validate_chat_response_flags_unconfirmed_task_mutation_claim():
    out = validate_chat_response(
        "Create a task to watch Iran every 2 hours",
        "I created the task and set it to run every 2 hours.",
        executed_tools=[],
    )
    assert out.task_mutation_unconfirmed is True
    assert out.should_retry is True
    assert out.retry_reason == "task_create_unconfirmed"


def test_validate_chat_response_flags_claimed_update_when_only_create_task_ran():
    out = validate_chat_response(
        "Change the World Cup watcher to only run between 7am and 9pm",
        "I updated the World Cup topic watcher to only run between 7:00 AM and 9:00 PM.",
        executed_tools=[
            {
                "tool": "create_task",
                "result_summary": '{"created": true, "task_id": 55, "title": "World Cup Topic Watcher"}',
            }
        ],
    )
    assert out.task_mutation_unconfirmed is True
    assert out.should_retry is True
    assert out.retry_reason == "task_update_unconfirmed"


@pytest.mark.asyncio
async def test_send_message_retries_once_for_missing_links_on_complex_prompt(client):
    await client.post(
        "/auth/register",
        json={
            "username": "chatvalidateuser",
            "email": "chatvalidateuser@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "chatvalidateuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Validation Retry"}, headers=headers)
    session_id = create.json()["id"]

    with (
        patch.object(settings, "chat_complexity_routing_enabled", True),
        patch.object(settings, "chat_complexity_threshold", 1),
        patch.object(settings, "chat_validation_enabled", True),
        patch.object(settings, "chat_validation_retry_enabled", True),
        patch.object(settings, "chat_validation_retry_max_attempts", 1),
        patch(
            "app.api.chat.run_agent",
            new_callable=AsyncMock,
            side_effect=[
                "Top stories today include several major events.",
                "Top stories:\n1. AP: [Read](https://apnews.com/)\n2. BBC: [Read](https://www.bbc.com/news)",
            ],
        ) as mock_run,
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Research the latest headlines and cite sources"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert "https://apnews.com/" in resp.json()["content"]
    assert mock_run.await_count == 2
    assert mock_run.await_args_list[0].kwargs["mode"] == "chat_orchestrated"


@pytest.mark.asyncio
async def test_send_message_retries_once_for_tool_leakage_on_simple_research_prompt(client):
    await client.post(
        "/auth/register",
        json={
            "username": "chattoolleak",
            "email": "chattoolleak@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "chattoolleak", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Validation Research Simple"}, headers=headers)
    session_id = create.json()["id"]

    with (
        patch.object(settings, "chat_complexity_routing_enabled", True),
        patch.object(settings, "chat_complexity_threshold", 99),
        patch.object(settings, "chat_validation_enabled", True),
        patch.object(settings, "chat_validation_retry_enabled", True),
        patch.object(settings, "chat_validation_retry_max_attempts", 1),
        patch(
            "app.api.chat.run_agent",
            new_callable=AsyncMock,
            side_effect=[
                "I'll pull more coverage. (to=functions.search_my_feeds_timeline armat) The tool name must be provided correctly.",
                "Weekend summary:\n1. BBC: [Read](https://www.bbc.com/news/articles/cn9qzl12537o)",
            ],
        ) as mock_run,
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "check my rss sources for the latest news on Iran and cite sources"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert "bbc.com/news/articles" in resp.json()["content"]
    assert mock_run.await_count == 2
    assert mock_run.await_args_list[0].kwargs["mode"] == "chat"


@pytest.mark.asyncio
async def test_send_message_retries_once_for_followup_article_detail_leakage(client):
    await client.post(
        "/auth/register",
        json={
            "username": "chatdetailleak",
            "email": "chatdetailleak@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "chatdetailleak", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Follow-up Detail Leak"}, headers=headers)
    session_id = create.json()["id"]

    with (
        patch.object(settings, "chat_complexity_routing_enabled", True),
        patch.object(settings, "chat_complexity_threshold", 99),
        patch.object(settings, "chat_validation_enabled", True),
        patch.object(settings, "chat_validation_retry_enabled", True),
        patch.object(settings, "chat_validation_retry_max_attempts", 1),
        patch(
            "app.api.chat.run_agent",
            new_callable=AsyncMock,
            side_effect=[
                (
                    "I'll fetch the full page now.\n"
                    "Compacted tool result.\n"
                    "Tool: fetch_page\n"
                    "Tool call id: call_123\n"
                    "Summary: Iran said the ships were seized."
                ),
                (
                    "The rundown: Iran's Revolutionary Guards said they seized two cargo ships near the Strait of Hormuz "
                    "after the ceasefire extension, and U.K. maritime monitors reported attacks on two vessels."
                ),
            ],
        ) as mock_run,
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Give me the run down on the Ship Seizure from the 1st article"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert "The rundown:" in resp.json()["content"]
    assert "Compacted tool result." not in resp.json()["content"]
    assert mock_run.await_count == 2
    assert mock_run.await_args_list[0].kwargs["mode"] == "chat"


@pytest.mark.asyncio
async def test_send_message_strips_invalid_links_when_retry_disabled(client):
    await client.post(
        "/auth/register",
        json={
            "username": "chatinvalidstrip",
            "email": "chatinvalidstrip@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "chatinvalidstrip", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Validation Strip"}, headers=headers)
    session_id = create.json()["id"]

    with (
        patch.object(settings, "chat_complexity_routing_enabled", True),
        patch.object(settings, "chat_complexity_threshold", 1),
        patch.object(settings, "chat_validation_enabled", True),
        patch.object(settings, "chat_validation_retry_enabled", False),
        patch(
            "app.api.chat.run_agent",
            new_callable=AsyncMock,
            return_value="Story: [Read](https://example.com/fake).",
        ),
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Research recent stories and include links"},
            headers=headers,
        )

    assert resp.status_code == 200
    content = resp.json()["content"]
    assert "example.com/fake" not in content


@pytest.mark.asyncio
async def test_kill_switch_forces_complex_chat_back_to_simple_mode(client):
    token = await _login_token(client, "chatkillswitch", "chatkillswitch@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    create = await client.post("/chat/sessions", json={"title": "Kill Switch"}, headers=headers)
    session_id = create.json()["id"]

    baseline = metrics.snapshot().get("chat_orchestration_kill_switch_suppressed_count", 0)

    with (
        patch.object(settings, "chat_complexity_routing_enabled", True),
        patch.object(settings, "chat_complexity_threshold", 1),
        patch.object(settings, "chat_orchestration_kill_switch", True),
        patch.object(settings, "chat_validation_enabled", False),
        patch("app.api.chat.run_agent", new_callable=AsyncMock, return_value="ok") as mock_run,
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Research latest headlines and compare sources"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert mock_run.await_count == 1
    assert mock_run.await_args.kwargs["mode"] == "chat"
    now = metrics.snapshot().get("chat_orchestration_kill_switch_suppressed_count", 0)
    assert now >= baseline + 1


@pytest.mark.asyncio
async def test_library_lookup_intent_forces_grounded_search(client):
    token = await _login_token(client, "chatlibraryintent", "chatlibraryintent@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    create = await client.post("/chat/sessions", json={"title": "Library Grounding"}, headers=headers)
    session_id = create.json()["id"]

    with (
        patch.object(settings, "chat_complexity_routing_enabled", False),
        patch.object(settings, "chat_orchestration_kill_switch", False),
        patch(
            "app.agent.tools._list_library_documents",
            new_callable=AsyncMock,
            return_value='{"count":1,"documents":[{"id":1,"filename":"Family Calendar.pdf"}]}',
        ),
        patch("app.agent.tools._write_audit_log", new_callable=AsyncMock),
        patch(
            "app.api.chat.run_agent",
            new_callable=AsyncMock,
            return_value="Grounded response with concrete document evidence and details.",
        ) as mock_run,
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Can you list the documents in my library?"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert mock_run.await_count >= 1
    kwargs = mock_run.await_args_list[0].kwargs
    assert kwargs["mode"] == "chat_orchestrated"
    injected_history = mock_run.await_args_list[0].args[0]
    assert any(
        m.get("role") == "system" and "Required grounding for this turn" in m.get("content", "")
        for m in injected_history
    )


@pytest.mark.asyncio
async def test_library_excerpt_intent_uses_search_library_grounding(client):
    token = await _login_token(client, "chatlibraryexcerpt", "chatlibraryexcerpt@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    create = await client.post("/chat/sessions", json={"title": "Library Excerpt Grounding"}, headers=headers)
    session_id = create.json()["id"]

    with (
        patch.object(settings, "chat_complexity_routing_enabled", False),
        patch.object(settings, "chat_orchestration_kill_switch", False),
        patch("app.agent.tools._search_library", new_callable=AsyncMock, return_value="Search results for: excerpt"),
        patch("app.agent.tools._write_audit_log", new_callable=AsyncMock),
        patch(
            "app.api.chat.run_agent",
            new_callable=AsyncMock,
            return_value="Grounded excerpt response with source.",
        ) as mock_run,
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "show me excerpt details from that document in my library"},
            headers=headers,
        )

    assert resp.status_code == 200
    injected_history = mock_run.await_args_list[0].args[0]
    assert any(
        m.get("role") == "system" and "search_library result" in m.get("content", "")
        for m in injected_history
    )


@pytest.mark.asyncio
async def test_library_summary_intent_uses_summarize_document_grounding(client):
    token = await _login_token(client, "chatlibrarysummary", "chatlibrarysummary@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    create = await client.post("/chat/sessions", json={"title": "Library Summary Grounding"}, headers=headers)
    session_id = create.json()["id"]

    with (
        patch.object(settings, "chat_complexity_routing_enabled", False),
        patch.object(settings, "chat_orchestration_kill_switch", False),
        patch(
            "app.agent.tools._list_library_documents",
            new_callable=AsyncMock,
            return_value='{"count":1,"documents":[{"id":17,"filename":"SGAO_Studio_Manual_v1.0-2.pdf"}]}',
        ),
        patch(
            "app.agent.tools._summarize_document",
            new_callable=AsyncMock,
            return_value="Document summary for SGAO_Studio_Manual_v1.0-2.pdf",
        ) as mock_summary,
        patch("app.agent.tools._write_audit_log", new_callable=AsyncMock) as mock_audit,
        patch(
            "app.api.chat.run_agent",
            new_callable=AsyncMock,
            return_value="Grounded document summary response.",
        ) as mock_run,
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "summarize the SGAO_Studio_Manual_v1.0-2.pdf from my library"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert mock_summary.await_args.kwargs == {}
    assert mock_summary.await_args.args[0]["document_name"] == "SGAO_Studio_Manual_v1.0-2.pdf"
    assert mock_audit.await_args.kwargs["arguments"]["document_name"] == "SGAO_Studio_Manual_v1.0-2.pdf"
    injected_history = mock_run.await_args_list[0].args[0]
    assert any(
        m.get("role") == "system" and "summarize_document result" in m.get("content", "")
        for m in injected_history
    )


@pytest.mark.asyncio
async def test_library_summary_intent_returns_ambiguity_prompt_when_multiple_docs_match(client):
    token = await _login_token(client, "chatlibraryambig", "chatlibraryambig@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    create = await client.post("/chat/sessions", json={"title": "Library Summary Ambiguity"}, headers=headers)
    session_id = create.json()["id"]

    with (
        patch.object(settings, "chat_complexity_routing_enabled", False),
        patch.object(settings, "chat_orchestration_kill_switch", False),
        patch(
            "app.agent.tools._list_library_documents",
            new_callable=AsyncMock,
            return_value='{"count":2,"documents":[{"id":17,"filename":"WorkerNode.md"},{"id":18,"filename":"WorkerNode-Notes.md"}]}',
        ),
        patch("app.agent.tools._summarize_document", new_callable=AsyncMock) as mock_summary,
        patch("app.agent.tools._write_audit_log", new_callable=AsyncMock) as mock_audit,
        patch(
            "app.api.chat.run_agent",
            new_callable=AsyncMock,
            return_value="Please clarify the filename.",
        ) as mock_run,
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "summarize workernode from my library"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert mock_summary.await_count == 0
    assert mock_audit.await_args.kwargs["arguments"]["document_name"] == "workernode"
    injected_history = mock_run.await_args_list[0].args[0]
    assert any(
        m.get("role") == "system"
        and "Multiple documents match 'summarize workernode from my library'" in m.get("content", "")
        for m in injected_history
    )


@pytest.mark.asyncio
async def test_calendar_prompt_with_typo_does_not_block_tools(client):
    token = await _login_token(client, "chatcalendertyposafe", "chatcalendertyposafe@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    create = await client.post("/chat/sessions", json={"title": "Calendar Typo"}, headers=headers)
    session_id = create.json()["id"]

    captured = {}

    async def _fake_run_agent(messages, user_context, mode="chat", model_override=None, stage=None):
        captured["blocked_tools"] = list(user_context.blocked_tools or [])
        return "ok"

    with patch("app.api.chat.run_agent", new=AsyncMock(side_effect=_fake_run_agent)):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "What is on my calender for the next 5 days?"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert "list_events" not in captured["blocked_tools"]


@pytest.mark.asyncio
async def test_rest_chat_does_not_claim_calendar_mutation_without_confirmed_tool(client):
    token = await _login_token(client, "chatcalendarclaim", "chatcalendarclaim@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    create = await client.post("/chat/sessions", json={"title": "Calendar Claim"}, headers=headers)
    session_id = create.json()["id"]

    with patch(
        "app.api.chat.run_agent",
        new_callable=AsyncMock,
        return_value="I've created an event titled 'Lunch with Rod' on your calendar for next Wednesday at 12 PM.",
    ):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Create an event for next Wednesday at 12pm for lunch with Rod"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert "couldn't confirm" in resp.json()["content"].lower()


async def _login_token(client, username: str, email: str) -> str:
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": email,
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": username, "password": "pass123"},
    )
    return login.json()["access_token"]
