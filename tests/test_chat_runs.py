from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.autonomy.approval import ApprovalRequired, build_blocked_tool_approval_payload
from app.db.models import ChatMessage, ChatRun

from tests.conftest import TestSessionLocal


async def _authenticated_session(client, username: str) -> tuple[dict[str, str], int]:
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": username, "password": "pass123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created = await client.post("/chat/sessions", headers=headers, json={"title": "Run test"})
    return headers, int(created.json()["id"])


@pytest.mark.asyncio
async def test_completed_chat_run_is_durable_and_queryable(client):
    headers, session_id = await _authenticated_session(client, "durablechat")

    with patch("app.api.chat._execute_chat_turn", new=AsyncMock(return_value="durable answer")):
        response = await client.post(
            f"/chat/sessions/{session_id}/messages",
            headers=headers,
            json={"content": "hello"},
        )

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    status_response = await client.get(f"/chat/runs/{run_id}/status", headers=headers)
    payload = status_response.json()
    assert payload["run_id"] == run_id
    assert payload["status"] == "completed"
    assert payload["phase"] == "completed"
    assert payload["active"] is False

    async with TestSessionLocal() as db:
        run = await db.get(ChatRun, run_id)
        assert run is not None
        assert run.user_message_id is not None
        assert run.assistant_message_id == response.json()["message_id"]


@pytest.mark.asyncio
async def test_inactive_running_chat_run_is_classified_as_interrupted(client):
    headers, session_id = await _authenticated_session(client, "interruptedchat")
    async with TestSessionLocal() as db:
        user_message = ChatMessage(session_id=session_id, role="user", content="hello")
        db.add(user_message)
        await db.flush()
        run = ChatRun(
            id="chat_run_interrupted",
            session_id=session_id,
            user_id=1,
            user_message_id=user_message.id,
            status="running",
            phase="model_active",
        )
        db.add(run)
        await db.commit()

    response = await client.get(f"/chat/runs/{run.id}/status", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["phase"] == "interrupted"
    assert response.json()["error_classification"] == "process_interrupted"


@pytest.mark.asyncio
async def test_chat_approval_replays_exact_call_and_resumes_same_run(client):
    headers, session_id = await _authenticated_session(client, "chatapprove")
    blocked_call = {
        "id": "call_append_1",
        "type": "function",
        "function": {
            "name": "append_file",
            "arguments": json.dumps({"path": "reports/test.md", "content": "exact line\n"}),
        },
    }

    async def _pause(*_args, pre_tool_callback=None, **_kwargs):
        await pre_tool_callback([blocked_call])
        raise ApprovalRequired(
            "append_file",
            payload=build_blocked_tool_approval_payload(
                "append_file",
                {"path": "reports/test.md", "content": "exact line\n"},
            ),
        )

    with patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=_pause)):
        waiting = await client.post(
            f"/chat/sessions/{session_id}/messages",
            headers=headers,
            json={"content": "append the exact line", "approval_mode": True},
        )

    assert waiting.status_code == 202
    waiting_payload = waiting.json()
    run_id = waiting_payload["run_id"]
    assert waiting_payload["state"] == "waiting_approval"
    assert waiting_payload["waiting_approval"]["blocked_tool"] == "append_file"
    assert waiting_payload["waiting_approval"]["arguments"]["content"] == "exact line\n"

    with (
        patch(
            "app.api.chat.replay_waiting_approval_tool",
            new=AsyncMock(
                return_value=(
                    "Appended exact line",
                    {
                        "tool_name": "append_file",
                        "arguments": {"path": "reports/test.md", "content": "exact line\n"},
                    },
                )
            ),
        ) as replay,
        patch("app.api.chat._execute_chat_turn", new=AsyncMock(return_value="The file was updated.")),
    ):
        resumed = await client.post(
            f"/chat/runs/{run_id}/approval",
            headers=headers,
            json={"approved": True},
        )

    assert resumed.status_code == 200
    assert resumed.json()["run_id"] == run_id
    assert resumed.json()["state"] == "completed"
    replay.assert_awaited_once()
    replay_payload = replay.await_args.args[0]
    assert replay_payload["tool_call_id"] == "call_append_1"
    assert replay_payload["arguments"]["path"] == "reports/test.md"

    async with TestSessionLocal() as db:
        run = await db.get(ChatRun, run_id)
        assert run.status == "completed"
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id)
            )
        ).scalars().all()
    assert sum("call_append_1" in str(row.tool_calls or "") for row in rows) == 1
    assert sum("call_append_1" in str(row.tool_results or "") for row in rows) == 1
    assert rows[-1].content == "The file was updated."


@pytest.mark.asyncio
async def test_chat_approval_denial_does_not_replay_tool(client):
    headers, session_id = await _authenticated_session(client, "chatdeny")
    async with TestSessionLocal() as db:
        user = (await db.execute(select(ChatMessage).where(ChatMessage.session_id == session_id))).scalars().first()
        if user is None:
            user = ChatMessage(session_id=session_id, role="user", content="write a file")
            db.add(user)
            await db.flush()
        run = ChatRun(
            id="chat_run_denied",
            session_id=session_id,
            user_id=1,
            user_message_id=user.id,
            status="waiting_approval",
            phase="waiting_approval",
            approval_kind="tool",
        )
        run.approval_payload = {
            "payload_type": "blocked_tool_call",
            "tool_name": "write_file",
            "arguments": {"path": "reports/no.md", "content": "no"},
            "reason": "Writing changes workspace data.",
            "tool_call_id": "call_no",
        }
        db.add(run)
        await db.commit()

    with patch("app.api.chat.replay_waiting_approval_tool", new=AsyncMock()) as replay:
        denied = await client.post(
            "/chat/runs/chat_run_denied/approval",
            headers=headers,
            json={"approved": False},
        )

    assert denied.status_code == 200
    assert denied.json()["status"] == "cancelled"
    assert denied.json()["phase"] == "approval_denied"
    replay.assert_not_awaited()
