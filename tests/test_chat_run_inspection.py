from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.agent.runtime import AgentEvent, AgentEventType
from app.chat_runtime import apply_chat_run_event, build_chat_run_inspection
from app.db.models import ChatMessage, ChatRun, ChatRunEvent, ChatSession, LLMUsageEvent, User
from tests.conftest import TestSessionLocal


async def _auth(client, username: str, *, admin: bool = False) -> tuple[dict[str, str], int]:
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "pass123",
        },
    )
    if admin:
        async with TestSessionLocal() as db:
            user = (
                await db.execute(select(User).where(User.username == username))
            ).scalar_one()
            user.role = "admin"
            await db.commit()
    login = await client.post(
        "/auth/login",
        json={"username": username, "password": "pass123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = await client.get("/auth/me", headers=headers)
    return headers, int(me.json()["id"])


async def _seed_trace(*, user_id: int, model: str, suffix: str) -> str:
    started = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    async with TestSessionLocal() as db:
        session = ChatSession(
            user_id=user_id,
            title=f"Trace {suffix}",
            persona="family_assistant",
            llm_model=model,
        )
        db.add(session)
        await db.flush()
        user_message = ChatMessage(session_id=session.id, role="user", content="private prompt")
        db.add(user_message)
        await db.flush()
        run = ChatRun(
            id=f"chat_run_trace_{suffix}",
            session_id=session.id,
            user_id=user_id,
            user_message_id=user_message.id,
            status="running",
            phase="starting",
            mode="chat_orchestrated",
            stage="chat_complex",
            model=model,
            started_at=started,
        )
        db.add(run)
        await db.flush()

        events = [
            (AgentEventType.RUN_STARTED, {"model": model, "private_prompt": "do not retain"}),
            (AgentEventType.MODEL_TURN_STARTED, {"turn": 1, "tool_count": 12}),
            (
                AgentEventType.TOOL_REQUESTED,
                {
                    "turn": 1,
                    "tool_name": "web_search",
                    "tool_call_id": "call_1",
                    "argument_keys": ["query"],
                    "query": "private search query",
                },
            ),
            (
                AgentEventType.TOOL_COMPLETED,
                {
                    "turn": 1,
                    "tool_name": "web_search",
                    "tool_call_id": "call_1",
                    "citation_count": 2,
                    "artifact_count": 0,
                    "has_structured_content": True,
                },
            ),
            (AgentEventType.VALIDATION_RETRY, {"retry_reason": "missing_citation"}),
            (AgentEventType.RUN_COMPLETED, {"content_chars": 800}),
        ]
        for sequence, (event_type, payload) in enumerate(events, start=1):
            await apply_chat_run_event(
                db,
                run,
                AgentEvent(
                    event_id=f"{run.id}:{sequence}",
                    run_id=run.id,
                    sequence=sequence,
                    timestamp=started + timedelta(seconds=sequence - 1),
                    type=event_type,
                    session_id=session.id,
                    payload=payload,
                ),
            )

        run.status = "completed"
        run.phase = "completed"
        run.finished_at = started + timedelta(seconds=6)
        db.add(
            LLMUsageEvent(
                user_id=user_id,
                session_id=session.id,
                chat_run_id=run.id,
                source="chat_websocket",
                stage="chat_complex",
                model=model,
                provider="ollama" if model.startswith("ollama") else "openai",
                prompt_tokens=1000,
                completion_tokens=200,
                total_tokens=1200,
                cached_prompt_tokens=600,
                total_duration_ms=1750.0,
            )
        )
        await db.commit()
        return run.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "provider_family"),
    [
        ("ollama_chat/qwen3.6:35b", "ollama"),
        ("gpt-5-mini", "openai"),
    ],
)
async def test_chat_run_inspection_qualifies_local_and_openai_traces(
    client,
    model: str,
    provider_family: str,
):
    _headers, user_id = await _auth(client, f"trace{provider_family}")
    run_id = await _seed_trace(user_id=user_id, model=model, suffix=provider_family)

    async with TestSessionLocal() as db:
        run = await db.get(ChatRun, run_id)
        payload = await build_chat_run_inspection(db, run, active=False)

    assert payload["scenario"] == {
        "provider_family": provider_family,
        "mode": "chat_orchestrated",
        "stage": "chat_complex",
        "interaction": "tool_augmented",
    }
    assert payload["outcome"]["completed"] is True
    assert payload["outcome"]["grounding"] == "grounded"
    assert payload["metrics"]["tool_requests"] == 1
    assert payload["metrics"]["tool_completions"] == 1
    assert payload["metrics"]["validation_retries"] == 1
    assert payload["metrics"]["latency_ms"] == 6000.0
    assert payload["metrics"]["cached_prompt_tokens"] == 600
    assert payload["metrics"]["prompt_cache_percent"] == 60.0
    assert payload["tools"] == ["web_search"]
    serialized = json.dumps(payload, default=str)
    assert "private prompt" not in serialized
    assert "private search query" not in serialized


@pytest.mark.asyncio
async def test_admin_and_mcp_expose_same_chat_run_trace(client):
    owner_headers, owner_id = await _auth(client, "traceowner")
    admin_headers, _ = await _auth(client, "traceadmin", admin=True)
    run_id = await _seed_trace(
        user_id=owner_id,
        model="ollama_chat/qwen3.6:35b",
        suffix="surfaces",
    )

    admin = await client.get(f"/admin/chat-runs/{run_id}/inspect", headers=admin_headers)
    assert admin.status_code == 200
    assert admin.json()["metrics"]["tool_requests"] == 1

    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        mcp = await client.post(
            "/mcp/fruitcake/tools/call",
            headers=owner_headers,
            json={
                "jsonrpc": "2.0",
                "id": 44,
                "method": "tools/call",
                "params": {
                    "name": "fruitcake_inspect_chat_run",
                    "arguments": {"run_id": run_id},
                },
            },
        )
    assert mcp.status_code == 200
    assert "result" in mcp.json(), mcp.text
    payload = mcp.json()["result"]["structuredContent"]
    assert payload["found"] is True
    assert payload["run"]["run_id"] == run_id
    assert payload["metrics"] == admin.json()["metrics"]


@pytest.mark.asyncio
async def test_chat_trace_does_not_persist_text_delta_events(client):
    _headers, user_id = await _auth(client, "tracedelta")
    run_id = await _seed_trace(
        user_id=user_id,
        model="gpt-5-mini",
        suffix="delta",
    )
    async with TestSessionLocal() as db:
        run = await db.get(ChatRun, run_id)
        await apply_chat_run_event(
            db,
            run,
            AgentEvent(
                event_id=f"{run_id}:99",
                run_id=run_id,
                sequence=99,
                timestamp=datetime.now(timezone.utc),
                type=AgentEventType.TEXT_DELTA,
                payload={"content": "private generated text"},
            ),
        )
        await db.commit()
        rows = (
            await db.execute(select(ChatRunEvent).where(ChatRunEvent.run_id == run_id))
        ).scalars().all()

    assert all(row.event_type != AgentEventType.TEXT_DELTA.value for row in rows)
    assert all("private generated text" not in row.payload_json for row in rows)
