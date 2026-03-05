"""
FruitcakeAI v5 — Task step planning endpoints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


async def _headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_create_plan_and_list_steps(client):
    headers = await _headers(client, "stepowner")
    task = await client.post(
        "/tasks",
        json={"title": "Trip", "instruction": "Plan summer vacation"},
        headers=headers,
    )
    task_id = task.json()["id"]

    fake_steps = [
        {"title": "Set goals", "instruction": "Define budget and destination", "requires_approval": False},
        {"title": "Book travel", "instruction": "Book flights and lodging", "requires_approval": True},
    ]
    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)):
        planned = await client.post(
            f"/tasks/{task_id}/plan",
            json={"goal": "Plan vacation", "max_steps": 5, "notes": "", "style": "concise"},
            headers=headers,
        )
    assert planned.status_code == 200
    assert planned.json()["steps_created"] == 2

    steps = await client.get(f"/tasks/{task_id}/steps", headers=headers)
    assert steps.status_code == 200
    rows = steps.json()
    assert [r["step_index"] for r in rows] == [1, 2]
    assert rows[1]["requires_approval"] is True


@pytest.mark.asyncio
async def test_patch_step(client):
    headers = await _headers(client, "patchowner")
    task = await client.post(
        "/tasks",
        json={"title": "House", "instruction": "Do maintenance"},
        headers=headers,
    )
    task_id = task.json()["id"]

    fake_steps = [{"title": "Inspect", "instruction": "Inspect systems", "requires_approval": False}]
    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)):
        await client.post(
            f"/tasks/{task_id}/plan",
            json={"goal": "Maintenance"},
            headers=headers,
        )
    steps = await client.get(f"/tasks/{task_id}/steps", headers=headers)
    step_id = steps.json()[0]["id"]

    patch_resp = await client.patch(
        f"/tasks/{task_id}/steps/{step_id}",
        json={"title": "Inspect HVAC", "status": "running"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["title"] == "Inspect HVAC"
    assert patch_resp.json()["status"] == "running"


@pytest.mark.asyncio
async def test_steps_owner_scope(client):
    owner_headers = await _headers(client, "owner1")
    other_headers = await _headers(client, "owner2")

    task = await client.post(
        "/tasks",
        json={"title": "Scoped", "instruction": "Owner only"},
        headers=owner_headers,
    )
    task_id = task.json()["id"]

    fake_steps = [{"title": "One", "instruction": "Do one", "requires_approval": False}]
    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)):
        await client.post(
            f"/tasks/{task_id}/plan",
            json={"goal": "Goal"},
            headers=owner_headers,
        )

    other_get = await client.get(f"/tasks/{task_id}/steps", headers=other_headers)
    assert other_get.status_code == 404
