"""
Admin incognito session tests.

Covers:
- admin-only creation gating and payload exposure of is_incognito
- hard delete removes the session and all message rows
- persistent tool families removed from the offered surface
- dispatch-level hard block (prompt drift cannot bypass the surface filter)
- audit redaction for incognito tool calls
- task-draft accept/deny endpoints rejected for incognito sessions
- normal sessions unchanged
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agent.context import UserContext
from app.agent.tools import (
    dispatch_tool_calls,
    get_tools_for_user,
    is_incognito_blocked_tool,
)
from app.db.models import ChatMessage, ChatSession, User
from tests.conftest import TestSessionLocal


async def _token(client, username: str, *, role: str | None = None) -> str:
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "pass123",
        },
    )
    if role:
        async with TestSessionLocal() as db:
            user = (await db.execute(select(User).where(User.username == username))).scalar_one()
            user.role = role
            await db.commit()
    login = await client.post(
        "/auth/login",
        json={"username": username, "password": "pass123"},
    )
    return login.json()["access_token"]


def _incognito_context(**overrides) -> UserContext:
    defaults = dict(
        user_id=1,
        username="admin",
        role="admin",
        is_incognito=True,
        session_id=123,
    )
    defaults.update(overrides)
    return UserContext(**defaults)


# ── Session lifecycle ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_can_create_incognito_session(client):
    token = await _token(client, "incogadmin", role="admin")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/chat/sessions", headers=headers, json={"is_incognito": True})
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["is_incognito"] is True
    assert payload["title"] == "Incognito session"

    sessions = await client.get("/chat/sessions", headers=headers)
    assert sessions.status_code == 200
    listed = {s["id"]: s for s in sessions.json()}
    assert listed[payload["id"]]["is_incognito"] is True


@pytest.mark.asyncio
async def test_non_admin_cannot_create_incognito_session(client):
    token = await _token(client, "incogparent")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/chat/sessions", headers=headers, json={"is_incognito": True})
    assert resp.status_code == 403

    normal = await client.post("/chat/sessions", headers=headers, json={})
    assert normal.status_code == 201
    assert normal.json()["is_incognito"] is False


@pytest.mark.asyncio
async def test_delete_incognito_session_removes_all_message_rows(client):
    token = await _token(client, "incogdeleter", role="admin")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/chat/sessions", headers=headers, json={"is_incognito": True})
    session_id = resp.json()["id"]

    async with TestSessionLocal() as db:
        db.add_all(
            [
                ChatMessage(session_id=session_id, role="user", content="sensitive question"),
                ChatMessage(session_id=session_id, role="assistant", content="sensitive answer"),
                ChatMessage(session_id=session_id, role="tool", content="tool payload"),
            ]
        )
        await db.commit()

    deleted = await client.delete(f"/chat/sessions/{session_id}", headers=headers)
    assert deleted.status_code == 204

    async with TestSessionLocal() as db:
        remaining_session = await db.get(ChatSession, session_id)
        remaining_messages = (
            await db.execute(select(ChatMessage).where(ChatMessage.session_id == session_id))
        ).scalars().all()
    assert remaining_session is None
    assert remaining_messages == []


# ── Side-effect blocking ───────────────────────────────────────────────────────


def test_incognito_tool_surface_excludes_persistent_tools():
    context = _incognito_context()
    offered = {t["function"]["name"] for t in get_tools_for_user(context)}

    for blocked in (
        "create_memory",
        "propose_task_draft",
        "create_task",
        "run_task_now",
        "create_task_plan",
        "api_request",
    ):
        assert blocked not in offered, blocked

    # Read-only surfaces stay available.
    for allowed in ("search_library", "list_library_documents", "list_tasks", "get_task"):
        assert allowed in offered, allowed


def test_incognito_blocklist_covers_mutating_name_patterns():
    # Safety net: future tools with mutation-shaped names are blocked by
    # default until explicitly reviewed.
    assert is_incognito_blocked_tool("create_widget")
    assert is_incognito_blocked_tool("delete_everything")
    assert is_incognito_blocked_tool("write_report")
    assert not is_incognito_blocked_tool("search_widgets")
    assert not is_incognito_blocked_tool("list_widgets")


@pytest.mark.asyncio
async def test_incognito_dispatch_hard_blocks_persistent_tool(monkeypatch):
    import app.agent.tools as tools_module

    audit_calls: list[dict] = []

    async def _capture_audit(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(tools_module, "_write_audit_log", _capture_audit)

    results = await dispatch_tool_calls(
        [
            {
                "id": "call_1",
                "function": {"name": "create_memory", "arguments": "{\"content\": \"secret\"}"},
            }
        ],
        _incognito_context(),
    )

    assert len(results) == 1
    assert "disabled in this incognito session" in results[0]["content"]
    # Blocked calls never execute and never write an audit row.
    assert audit_calls == []


@pytest.mark.asyncio
async def test_incognito_dispatch_redacts_audit_for_allowed_tools(monkeypatch):
    import app.agent.tools as tools_module

    audit_calls: list[dict] = []

    async def _capture_audit(**kwargs):
        audit_calls.append(kwargs)

    async def _fake_call_tool(name, arguments, user_context):
        return "tool ran fine"

    monkeypatch.setattr(tools_module, "_write_audit_log", _capture_audit)
    monkeypatch.setattr(tools_module, "_call_tool", _fake_call_tool)

    results = await dispatch_tool_calls(
        [
            {
                "id": "call_2",
                "function": {"name": "search_library", "arguments": "{\"query\": \"sensitive\"}"},
            }
        ],
        _incognito_context(),
    )

    assert results[0]["content"] == "tool ran fine"
    # give the fire-and-forget audit task a tick to run
    import asyncio

    await asyncio.sleep(0)
    assert len(audit_calls) == 1
    assert audit_calls[0]["arguments"] == {}
    assert audit_calls[0]["result_summary"] == "[incognito: content withheld]"
    assert audit_calls[0]["tool_name"] == "search_library"


@pytest.mark.asyncio
async def test_normal_dispatch_audit_keeps_content(monkeypatch):
    import app.agent.tools as tools_module

    audit_calls: list[dict] = []

    async def _capture_audit(**kwargs):
        audit_calls.append(kwargs)

    async def _fake_call_tool(name, arguments, user_context):
        return "normal result"

    monkeypatch.setattr(tools_module, "_write_audit_log", _capture_audit)
    monkeypatch.setattr(tools_module, "_call_tool", _fake_call_tool)

    await dispatch_tool_calls(
        [
            {
                "id": "call_3",
                "function": {"name": "search_library", "arguments": "{\"query\": \"normal\"}"},
            }
        ],
        _incognito_context(is_incognito=False),
    )

    import asyncio

    await asyncio.sleep(0)
    assert len(audit_calls) == 1
    assert audit_calls[0]["arguments"] == {"query": "normal"}
    assert audit_calls[0]["result_summary"] == "normal result"


# ── Task-draft endpoint guard ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_draft_accept_rejected_for_incognito_session(client):
    token = await _token(client, "incogdraftadmin", role="admin")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/chat/sessions", headers=headers, json={"is_incognito": True})
    session_id = resp.json()["id"]

    async with TestSessionLocal() as db:
        message = ChatMessage(
            session_id=session_id,
            role="assistant",
            content="draft",
            tool_results='{"task_draft": {"title": "x", "instruction": "y"}}',
        )
        db.add(message)
        await db.commit()
        await db.refresh(message)
        message_id = message.id

    accept = await client.post(f"/chat/messages/{message_id}/task-draft/accept", headers=headers)
    assert accept.status_code == 409
    assert "incognito" in accept.json()["error"].lower()

    deny = await client.post(f"/chat/messages/{message_id}/task-draft/deny", headers=headers)
    assert deny.status_code == 409
