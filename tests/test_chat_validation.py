from unittest.mock import AsyncMock, patch

import pytest

from app.agent.chat_validation import (
    build_chat_retry_instruction,
    validate_chat_response,
)
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


def test_build_retry_instruction_has_reason_specific_text():
    assert "too brief/empty" in build_chat_retry_instruction("empty_result").lower()
    assert "grounded sources" in build_chat_retry_instruction("missing_links").lower()
    assert "invalid/placeholder links" in build_chat_retry_instruction("invalid_links").lower()


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
