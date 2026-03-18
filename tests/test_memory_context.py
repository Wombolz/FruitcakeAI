from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from http import HTTPStatus

from app.db.models import Memory, Task
from app.memory.service import get_memory_service
from tests.conftest import TestSessionLocal


async def _headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _user_id(client, headers: dict[str, str]) -> int:
    me = await client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    return int(me.json()["id"])


async def _seed_memory(user_id: int, content: str = "User prefers concise answers.") -> int:
    async with TestSessionLocal() as db:
        memory = Memory(
            user_id=user_id,
            memory_type="semantic",
            content=content,
            importance=0.8,
            access_count=0,
            tags="[]",
            is_active=True,
        )
        db.add(memory)
        await db.commit()
        await db.refresh(memory)
        return int(memory.id)


@pytest.mark.asyncio
async def test_retrieve_for_context_does_not_increment_access_count(client):
    headers = await _headers(client, "memorypassive")
    user_id = await _user_id(client, headers)
    memory_id = await _seed_memory(user_id)

    async with TestSessionLocal() as db:
        svc = get_memory_service()
        results = await svc.retrieve_for_context(db, user_id, query="What do I prefer?")
        assert [m.id for m in results] == [memory_id]

    async with TestSessionLocal() as db:
        memory = await db.get(Memory, memory_id)
        assert memory is not None
        assert memory.access_count == 0
        assert memory.last_accessed_at is None


@pytest.mark.asyncio
async def test_recall_memory_endpoint_increments_access_and_timestamp(client):
    headers = await _headers(client, "memoryrecall")
    user_id = await _user_id(client, headers)
    memory_id = await _seed_memory(user_id, content="Remember the family cabin code.")

    resp = await client.post(f"/memories/{memory_id}/recall", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == memory_id
    assert data["access_count"] == 1
    assert data["last_accessed_at"] is not None


@pytest.mark.asyncio
async def test_chat_rest_injects_memory_context_before_agent_execution(client):
    headers = await _headers(client, "chatmemoryuser")
    user_id = await _user_id(client, headers)
    memory_id = await _seed_memory(user_id, content="This user likes bullet summaries.")

    session = await client.post("/chat/sessions", headers=headers)
    session_id = session.json()["id"]

    captured: dict[str, object] = {}

    async def _fake_run_agent(messages, user_context, mode="chat", model_override=None, stage=None):
        captured["messages"] = messages
        return "reply"

    with patch("app.api.chat.run_agent", new=AsyncMock(side_effect=_fake_run_agent)):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "How should you respond to me?"},
            headers=headers,
        )

    assert resp.status_code == 200
    messages = captured["messages"]
    assert isinstance(messages, list)
    system_notes = [m for m in messages if m.get("role") == "system"]
    assert any("## What I know about you" in str(m.get("content", "")) for m in system_notes)
    assert any("This user likes bullet summaries." in str(m.get("content", "")) for m in system_notes)

    async with TestSessionLocal() as db:
        memory = await db.get(Memory, memory_id)
        assert memory is not None
        assert memory.access_count == 0


@pytest.mark.asyncio
async def test_successful_task_marks_recalled_memories_accessed(client):
    from app.autonomy.runner import TaskRunner

    headers = await _headers(client, "taskmemoryuser")
    user_id = await _user_id(client, headers)
    memory_id = await _seed_memory(user_id, content="Always include the user's preferred summary style.")

    created = await client.post(
        "/tasks",
        json={
            "title": "Memory-aware task",
            "instruction": "Use what you know about the user and reply briefly",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    runner = TaskRunner()
    with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
        with patch("app.autonomy.runner._preflight_llm_dispatch", new=AsyncMock(return_value=None)):
            with patch("app.agent.core.run_agent", new=AsyncMock(return_value="done")):
                await runner.execute(type("TaskRef", (), {"id": task_id})())

    async with TestSessionLocal() as db:
        memory = await db.get(Memory, memory_id)
        task = await db.get(Task, task_id)
        assert memory is not None
        assert task is not None
        assert task.status == "completed"
        assert memory.access_count == 1
        assert memory.last_accessed_at is not None


@pytest.mark.asyncio
async def test_export_memories_includes_inactive_history(client):
    headers = await _headers(client, "memoryexport")
    user_id = await _user_id(client, headers)
    active_id = await _seed_memory(user_id, content="Active semantic memory")
    inactive_id = await _seed_memory(user_id, content="Inactive semantic memory")

    async with TestSessionLocal() as db:
        inactive = await db.get(Memory, inactive_id)
        inactive.is_active = False
        await db.commit()

    resp = await client.get("/memories/export", headers=headers)
    assert resp.status_code == HTTPStatus.OK
    assert "attachment;" in resp.headers.get("content-disposition", "")
    data = resp.json()
    assert data["user_id"] == user_id
    assert data["memory_count"] == 2
    ids = {m["id"] for m in data["memories"]}
    assert ids == {active_id, inactive_id}
    inactive_rows = [m for m in data["memories"] if m["id"] == inactive_id]
    assert inactive_rows[0]["is_active"] is False


@pytest.mark.asyncio
async def test_bulk_delete_memories_only_affects_current_user(client):
    headers_a = await _headers(client, "memorywipea")
    user_a = await _user_id(client, headers_a)
    headers_b = await _headers(client, "memorywipeb")
    user_b = await _user_id(client, headers_b)

    mem_a1 = await _seed_memory(user_a, content="A one")
    mem_a2 = await _seed_memory(user_a, content="A two")
    mem_b1 = await _seed_memory(user_b, content="B one")

    resp = await client.post("/memories/bulk-delete", headers=headers_a)
    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert data["deactivated_count"] == 2
    assert data["deleted_at"] is not None

    async with TestSessionLocal() as db:
        a1 = await db.get(Memory, mem_a1)
        a2 = await db.get(Memory, mem_a2)
        b1 = await db.get(Memory, mem_b1)
        assert a1 is not None and a1.is_active is False
        assert a2 is not None and a2.is_active is False
        assert b1 is not None and b1.is_active is True


@pytest.mark.asyncio
async def test_bulk_delete_memories_is_idempotent(client):
    headers = await _headers(client, "memorywipec")
    user_id = await _user_id(client, headers)
    await _seed_memory(user_id, content="Single memory")

    first = await client.post("/memories/bulk-delete", headers=headers)
    second = await client.post("/memories/bulk-delete", headers=headers)

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK
    assert first.json()["deactivated_count"] == 1
    assert second.json()["deactivated_count"] == 0
