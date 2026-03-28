from unittest.mock import AsyncMock, patch

import pytest

from app.agent.chat_validation import (
    build_chat_retry_instruction,
    validate_chat_response,
)
from app.agent.context import UserContext
from app.config import settings
from app.metrics import metrics
from app.api.chat import _execute_chat_turn


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


def test_build_retry_instruction_has_reason_specific_text():
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
async def test_execute_chat_turn_honors_retry_override_zero():
    user_context = UserContext(user_id=1, username="tester", role="parent")
    history = [{"role": "user", "content": "Research the latest headlines and cite sources"}]

    with (
        patch.object(settings, "chat_validation_enabled", True),
        patch.object(settings, "chat_validation_retry_enabled", True),
        patch.object(settings, "chat_validation_retry_max_attempts", 1),
        patch(
            "app.api.chat.run_agent",
            new_callable=AsyncMock,
            return_value="Top stories today include several major events.",
        ) as mock_run,
    ):
        content = await _execute_chat_turn(
            history,
            user_context,
            user_prompt=history[0]["content"],
            mode="chat_orchestrated",
            model_override=None,
            stage="chat_complex",
            enable_validation=True,
            retry_max_attempts_override=0,
        )

    assert "major events" in content
    assert mock_run.await_count == 1


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
