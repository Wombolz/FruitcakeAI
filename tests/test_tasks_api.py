from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_task_defaults_to_requires_approval_true(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskapprovaluser",
            "email": "taskapproval@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskapprovaluser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Safe default task",
            "instruction": "Check something and report back.",
            "task_type": "one_shot",
            "deliver": True,
        },
        headers=headers,
    )

    assert created.status_code == 201
    assert created.json()["requires_approval"] is True
