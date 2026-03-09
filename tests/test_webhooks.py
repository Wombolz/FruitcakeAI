"""
FruitcakeAI v5 — Webhooks API tests (Phase 5.1)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import TestSessionLocal

async def _auth_headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": password,
        },
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_webhook_create_and_list(client):
    headers = await _auth_headers(client, "hookuser")

    create = await client.post(
        "/webhooks",
        json={"name": "GitHub Push", "instruction": "Summarize this push event"},
        headers=headers,
    )
    assert create.status_code == 201
    created = create.json()
    assert created["name"] == "GitHub Push"
    assert created["active"] is True
    assert created["webhook_key"]

    listed = await client.get("/webhooks", headers=headers)
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert items[0]["id"] == created["id"]


@pytest.mark.asyncio
async def test_webhook_delete_is_owner_scoped(client):
    owner_headers = await _auth_headers(client, "owner")
    other_headers = await _auth_headers(client, "other")

    create = await client.post(
        "/webhooks",
        json={"name": "Deploy hook", "instruction": "Handle deploy trigger"},
        headers=owner_headers,
    )
    webhook_id = create.json()["id"]

    other_delete = await client.delete(f"/webhooks/{webhook_id}", headers=other_headers)
    assert other_delete.status_code == 404

    owner_delete = await client.delete(f"/webhooks/{webhook_id}", headers=owner_headers)
    assert owner_delete.status_code == 204


@pytest.mark.asyncio
async def test_webhook_trigger_accepts_and_enqueues(client):
    headers = await _auth_headers(client, "triggeruser")
    create = await client.post(
        "/webhooks",
        json={"name": "GitHub", "instruction": "Check this payload"},
        headers=headers,
    )
    cfg = create.json()

    with patch("app.api.webhooks._execute_webhook", new=AsyncMock()) as execute_mock:
        resp = await client.post(f"/webhooks/trigger/{cfg['webhook_key']}", json={"event": "push"})
        assert resp.status_code == 202
        assert resp.json()["accepted"] is True
        execute_mock.assert_awaited_once_with(cfg["id"], {"event": "push"})


@pytest.mark.asyncio
async def test_webhook_trigger_rejects_invalid_json(client):
    headers = await _auth_headers(client, "badjsonuser")
    create = await client.post(
        "/webhooks",
        json={"name": "Bad JSON", "instruction": "Handle input"},
        headers=headers,
    )
    cfg = create.json()

    resp = await client.post(
        f"/webhooks/trigger/{cfg['webhook_key']}",
        content="{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "Invalid JSON payload" in resp.json()["error"]


@pytest.mark.asyncio
async def test_webhook_trigger_inactive_key_not_found(client):
    headers = await _auth_headers(client, "inactiveuser")
    create = await client.post(
        "/webhooks",
        json={"name": "Inactive", "instruction": "Do work", "active": False},
        headers=headers,
    )
    cfg = create.json()

    resp = await client.post(f"/webhooks/trigger/{cfg['webhook_key']}", json={"event": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_webhook_crud_requires_auth(client):
    list_resp = await client.get("/webhooks")
    assert list_resp.status_code == 403

    create_resp = await client.post(
        "/webhooks",
        json={"name": "No auth", "instruction": "Fail"},
    )
    assert create_resp.status_code == 403


@pytest.mark.asyncio
async def test_execute_webhook_uses_small_then_large_fallback(monkeypatch, client):
    from app.api import webhooks as webhooks_module
    from app.config import settings

    monkeypatch.setattr(settings, "task_model_routing_enabled", True)
    monkeypatch.setattr(settings, "task_small_model", "ollama_chat/qwen2.5:7b")
    monkeypatch.setattr(settings, "task_large_model", "ollama_chat/qwen2.5:14b")
    monkeypatch.setattr(settings, "task_large_retry_enabled", True)
    monkeypatch.setattr(settings, "task_large_retry_max_attempts", 1)

    headers = await _auth_headers(client, "webhookmodelowner")
    create = await client.post(
        "/webhooks",
        json={"name": "Model route hook", "instruction": "Execute task mode"},
        headers=headers,
    )
    webhook_id = create.json()["id"]

    calls = []

    async def _fake_run_agent(messages, user_context, mode="chat", model_override=None, stage=None):
        calls.append((model_override, stage))
        if len(calls) == 1:
            raise RuntimeError("small model failed")
        return "fallback succeeded"

    with patch.object(webhooks_module, "AsyncSessionLocal", new=TestSessionLocal):
        with patch.object(webhooks_module, "run_agent", new=AsyncMock(side_effect=_fake_run_agent)):
            await webhooks_module._execute_webhook(webhook_id, {"event": "push"})

    assert calls[0] == ("ollama_chat/qwen2.5:7b", "webhook_execution")
    assert calls[1] == ("ollama_chat/qwen2.5:14b", "webhook_execution_fallback")
