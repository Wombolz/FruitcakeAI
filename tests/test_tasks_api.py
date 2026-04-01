from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import Task, TaskRun, User
from tests.conftest import TestSessionLocal


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


@pytest.mark.asyncio
async def test_create_task_computes_next_run_at_from_task_timezone(client):
    await client.post(
        "/auth/register",
        json={
            "username": "tasktzuser",
            "email": "tasktz@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "tasktzuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Morning local task",
            "instruction": "Run every morning.",
            "task_type": "recurring",
            "schedule": "0 08 * * *",
            "active_hours_tz": "America/New_York",
        },
        headers=headers,
    )

    assert created.status_code == 201
    next_run = datetime.fromisoformat(created.json()["next_run_at"])
    next_run_local = next_run.astimezone(ZoneInfo("America/New_York"))
    assert next_run_local.hour == 8
    assert next_run_local.minute == 0
    assert next_run.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_create_task_uses_user_timezone_when_task_timezone_missing(client):
    await client.post(
        "/auth/register",
        json={
            "username": "usertzuser",
            "email": "usertz@example.com",
            "password": "pass123",
        },
    )
    async with TestSessionLocal() as db:
        rows = await db.execute(select(User).where(User.username == "usertzuser"))
        user = rows.scalar_one_or_none()
        assert user is not None
        user.active_hours_tz = "America/New_York"
        await db.commit()

    login = await client.post(
        "/auth/login",
        json={"username": "usertzuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Morning user tz task",
            "instruction": "Run every morning.",
            "task_type": "recurring",
            "schedule": "0 08 * * *",
        },
        headers=headers,
    )

    assert created.status_code == 201
    next_run = datetime.fromisoformat(created.json()["next_run_at"])
    next_run_local = next_run.astimezone(ZoneInfo("America/New_York"))
    assert next_run_local.hour == 8
    assert next_run_local.minute == 0


@pytest.mark.asyncio
async def test_create_task_falls_back_to_utc_for_invalid_timezone(client):
    await client.post(
        "/auth/register",
        json={
            "username": "badtzuser",
            "email": "badtz@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "badtzuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Fallback UTC task",
            "instruction": "Run in fallback UTC.",
            "task_type": "recurring",
            "schedule": "0 08 * * *",
            "active_hours_tz": "Mars/Olympus_Mons",
        },
        headers=headers,
    )

    assert created.status_code == 201
    next_run = datetime.fromisoformat(created.json()["next_run_at"])
    assert next_run.tzinfo == timezone.utc
    assert next_run.hour == 8
    assert next_run.minute == 0


@pytest.mark.asyncio
async def test_manual_run_rejects_when_task_has_active_run(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskrunconflictuser",
            "email": "taskrunconflict@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskrunconflictuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Conflict task",
            "instruction": "Do the thing.",
            "task_type": "one_shot",
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        task.status = "pending"
        db.add(TaskRun(task_id=task_id, status="running"))
        await db.commit()

    resp = await client.post(f"/tasks/{task_id}/run", headers=headers)
    assert resp.status_code == 409
    assert "Task is already running" in resp.text
