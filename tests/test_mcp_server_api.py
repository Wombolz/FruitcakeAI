from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import Document
from tests.conftest import TestSessionLocal


async def _token(client, username: str) -> str:
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
    return login.json()["access_token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _patch_tool_db_sessions():
    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        yield


@pytest.mark.asyncio
async def test_mcp_initialize_returns_server_info(client):
    token = await _token(client, "mcpinituser")

    resp = await client.post(
        "/mcp/fruitcake/initialize",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=_auth_headers(token),
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["result"]["protocolVersion"] == "2024-11-05"
    assert payload["result"]["serverInfo"]["name"] == "FruitcakeAI MCP"


@pytest.mark.asyncio
async def test_mcp_tools_list_includes_task_and_library_tools(client):
    token = await _token(client, "mcplistuser")

    resp = await client.post(
        "/mcp/fruitcake/tools/list",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=_auth_headers(token),
    )

    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert "fruitcake_propose_task_draft" in names
    assert "fruitcake_create_task" in names
    assert "fruitcake_list_library_documents" in names


@pytest.mark.asyncio
async def test_mcp_propose_task_draft_returns_recipe_payload(client):
    token = await _token(client, "mcpdraftuser")

    resp = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_propose_task_draft",
                "arguments": {
                    "title": "US Politics Daily Briefing",
                    "instruction": "Create a daily cached RSS briefing about US politics and save it to workspace/politics/us-politics.md",
                    "task_type": "recurring",
                    "schedule": "every:24h",
                    "recipe_family": "daily_research_briefing",
                    "recipe_params": {
                        "topic": "US Politics",
                        "path": "workspace/politics/us-politics.md",
                        "window_hours": 24,
                    },
                },
            },
        },
        headers=_auth_headers(token),
    )

    assert resp.status_code == 200
    text_payload = resp.json()["result"]["content"][0]["text"]
    draft = json.loads(text_payload)
    assert draft["proposed"] is True
    assert draft["task_recipe"]["family"] == "daily_research_briefing"


@pytest.mark.asyncio
async def test_mcp_create_task_and_list_tasks(client):
    token = await _token(client, "mcpcreateuser")
    headers = _auth_headers(token)

    created = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "fruitcake_create_task",
                "arguments": {
                    "title": "OpenClaw watch",
                    "instruction": "Watch OpenClaw mentions in my RSS feeds.",
                    "task_type": "recurring",
                    "schedule": "every:6h",
                    "recipe_family": "topic_watcher",
                    "recipe_params": {"topic": "OpenClaw", "threshold": "medium"},
                },
            },
        },
        headers=headers,
    )
    assert created.status_code == 200
    created_payload = json.loads(created.json()["result"]["content"][0]["text"])
    assert created_payload["created"] is True
    created_task_id = created_payload["task_id"]

    listed = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "fruitcake_list_tasks", "arguments": {"limit": 10}},
        },
        headers=headers,
    )
    assert listed.status_code == 200
    list_payload = json.loads(listed.json()["result"]["content"][0]["text"])
    assert list_payload["count"] >= 1
    assert any(int(task["id"]) == int(created_task_id) for task in list_payload["tasks"])


@pytest.mark.asyncio
async def test_mcp_list_library_documents_and_search(client):
    token = await _token(client, "mcplibuser")
    headers = _auth_headers(token)

    me = await client.get("/auth/me", headers=headers)
    user_id = int(me.json()["id"])

    async with TestSessionLocal() as db:
        doc = Document(
            owner_id=user_id,
            filename="manual.pdf",
            original_filename="manual.pdf",
            file_path="/tmp/manual.pdf",
            file_size_bytes=128,
            mime_type="application/pdf",
            scope="personal",
            processing_status="ready",
            title="manual.pdf",
        )
        db.add(doc)
        await db.commit()

    listed = await client.post(
        "/mcp/fruitcake/tools/call",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "fruitcake_list_library_documents", "arguments": {"limit": 10}},
        },
        headers=headers,
    )
    assert listed.status_code == 200
    list_payload = json.loads(listed.json()["result"]["content"][0]["text"])
    assert any(doc["filename"] == "manual.pdf" for doc in list_payload["documents"])

    fake_rag = SimpleNamespace(
        is_ready=True,
        query=AsyncMock(
            return_value=[
                {
                    "text": "OpenClaw is mentioned here.",
                    "score": 0.91,
                    "metadata": {"filename": "manual.pdf"},
                }
            ]
        ),
    )
    with patch("app.rag.service.get_rag_service", return_value=fake_rag):
        searched = await client.post(
            "/mcp/fruitcake/tools/call",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "fruitcake_search_library",
                    "arguments": {"query": "OpenClaw", "top_k": 5},
                },
            },
            headers=headers,
        )

    assert searched.status_code == 200
    text = searched.json()["result"]["content"][0]["text"]
    assert "manual.pdf" in text
    assert "OpenClaw is mentioned here." in text
