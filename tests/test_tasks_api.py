from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import Task, TaskRun, User
from app.task_service import compute_next_run_at
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
    assert created.json()["effective_timezone"] == "America/New_York"
    assert created.json()["next_run_at_localized"].endswith(("EDT", "EST"))
    assert created.json()["created_at_localized"].endswith(("EDT", "EST"))


@pytest.mark.asyncio
async def test_create_task_returns_recipe_metadata_for_normalized_watcher(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskrecipeuser",
            "email": "taskrecipe@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskrecipeuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Iran Watch",
            "instruction": "Watch Iran and Middle East news for major updates.",
            "task_type": "recurring",
            "schedule": "every:2h",
            "deliver": True,
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["profile"] == "topic_watcher"
    assert payload["task_recipe"]["family"] == "topic_watcher"
    assert payload["task_recipe"]["instruction_style"] == "recipe_v1"


@pytest.mark.asyncio
async def test_create_task_accepts_explicit_recipe_family_from_editor(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskeditoruser",
            "email": "taskeditor@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskeditoruser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Daily Photography Briefing",
            "instruction": "Keep it concise and emphasize photo-industry business news.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
            "recipe_family": "briefing",
            "recipe_params": {
                "briefing_mode": "morning",
                "topic": "Photography",
                "path": "workspace/photography/daily.md",
                "window_hours": 24,
                "custom_guidance": "Keep it concise and emphasize photo-industry business news.",
            },
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "briefing"
    assert payload["task_recipe"]["params"]["briefing_mode"] == "morning"
    assert payload["task_recipe"]["selected_executor_kind"] == "configured_executor"
    assert "append a morning briefing" in payload["instruction"].lower()
    assert "photo-industry business news" in payload["instruction"].lower()
    assert payload["task_recipe"]["params"]["custom_guidance"].lower().startswith("keep it concise")


@pytest.mark.asyncio
async def test_create_task_accepts_explicit_watcher_recipe_params_from_editor(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskwatchereditoruser",
            "email": "taskwatchereditor@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskwatchereditoruser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "NASA Artemis Watcher",
            "instruction": "Keep this focused on major launches and mission updates.",
            "task_type": "recurring",
            "schedule": "every:2h",
            "deliver": True,
            "recipe_family": "topic_watcher",
            "recipe_params": {
                "topic": "NASA + Artemis",
                "threshold": "high",
                "sources": ["NASA Breaking News", "SpaceNews"],
            },
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "topic_watcher"
    assert payload["task_recipe"]["params"]["topic"] == "NASA + Artemis"
    assert payload["task_recipe"]["params"]["threshold"] == "high"


@pytest.mark.asyncio
async def test_create_task_respects_explicit_generic_family_from_editor(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskgenericeditoruser",
            "email": "taskgenericeditor@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskgenericeditoruser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Daily Photography Briefing",
            "instruction": "Append a daily research briefing about photography from the past 24 hours to workspace/photography/daily.md.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
            "recipe_family": "",
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["profile"] is None
    assert payload["task_recipe"] is None
    assert payload["instruction"].startswith("Append a daily research briefing")


@pytest.mark.asyncio
async def test_create_task_accepts_explicit_briefing_family_without_path_as_profile_briefing(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskbriefinginvaliduser",
            "email": "taskbriefinginvalid@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskbriefinginvaliduser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "US Politics Briefing",
            "instruction": "Keep me up to date on US politics.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
            "recipe_family": "briefing",
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "briefing"
    assert payload["task_recipe"]["params"]["briefing_mode"] == "morning"
    assert payload["profile"] == "briefing"
    assert payload["task_recipe"]["selected_executor_kind"] is None


@pytest.mark.asyncio
async def test_create_task_preserves_custom_guidance_for_morning_briefing(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskmorningeditoruser",
            "email": "taskmorningeditor@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskmorningeditoruser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Morning Briefing",
            "instruction": (
                "Prepare a morning briefing for today using my calendar and current headlines.\n"
                "Include today's schedule, notable headlines, and any important conflicts or priorities.\n"
                "Also include a short bit of trivia about this day in history."
            ),
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
            "recipe_family": "briefing",
            "recipe_params": {
                "briefing_mode": "morning",
            },
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "briefing"
    assert payload["task_recipe"]["params"]["briefing_mode"] == "morning"
    assert "day in history" in payload["instruction"].lower()
    assert payload["task_recipe"]["params"]["custom_guidance"].lower().startswith("also include")


@pytest.mark.asyncio
async def test_patch_task_can_clear_active_hours_and_recipe_family(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskpatchclearuser",
            "email": "taskpatchclear@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskpatchclearuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Morning local task",
            "instruction": "Watch Iran and Middle East news for major updates.",
            "task_type": "recurring",
            "schedule": "every:2h",
            "active_hours_start": "07:00",
            "active_hours_end": "22:00",
            "active_hours_tz": "America/New_York",
            "recipe_family": "topic_watcher",
        },
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    patched = await client.patch(
        f"/tasks/{task_id}",
        json={
            "active_hours_start": None,
            "active_hours_end": None,
            "active_hours_tz": None,
            "recipe_family": "",
            "recipe_params": None,
        },
        headers=headers,
    )

    assert patched.status_code == 200
    payload = patched.json()
    assert payload["active_hours_start"] is None
    assert payload["active_hours_end"] is None
    assert payload["active_hours_tz"] is None
    assert payload["profile"] is None
    assert payload["task_recipe"] is None


@pytest.mark.asyncio
async def test_patch_task_can_change_one_shot_to_recurring(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskpatchtypeuser",
            "email": "taskpatchtype@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskpatchtypeuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "One-time task",
            "instruction": "Do this once.",
            "task_type": "one_shot",
            "deliver": True,
        },
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    patched = await client.patch(
        f"/tasks/{task_id}",
        json={
            "task_type": "recurring",
            "schedule": "every:1d",
        },
        headers=headers,
    )

    assert patched.status_code == 200
    payload = patched.json()
    assert payload["task_type"] == "recurring"
    assert payload["schedule"] == "every:1d"
    assert payload["next_run_at"] is not None


@pytest.mark.asyncio
async def test_patch_task_can_repair_generic_task_into_daily_briefing(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskpatchbriefinguser",
            "email": "taskpatchbriefing@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskpatchbriefinguser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "US Politics Daily Briefing",
            "instruction": "Keep me updated on US politics.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
        },
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    patched = await client.patch(
        f"/tasks/{task_id}",
        json={
            "title": "US Politics Daily Briefing",
            "instruction": "Focus on policy, elections, and congressional movement.",
            "recipe_family": "briefing",
            "recipe_params": {
                "briefing_mode": "morning",
                "topic": "US Politics",
                "path": "workspace/politics/US Politics.md",
                "window_hours": 24,
                "custom_guidance": "Focus on policy, elections, and congressional movement.",
            },
        },
        headers=headers,
    )

    assert patched.status_code == 200
    payload = patched.json()
    assert payload["task_recipe"]["family"] == "briefing"
    assert payload["task_recipe"]["params"]["briefing_mode"] == "morning"
    assert payload["task_recipe"]["params"]["topic"] == "US Politics"
    assert payload["task_recipe"]["params"]["path"] == "workspace/politics/US Politics.md"
    assert payload["task_recipe"]["params"]["custom_guidance"].lower().startswith("focus on policy")
    assert "congressional movement" in payload["instruction"].lower()


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
    assert created.json()["effective_timezone"] == "America/New_York"
    assert created.json()["next_run_at_localized"].endswith(("EDT", "EST"))


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
    assert created.json()["effective_timezone"] == "UTC"
    assert created.json()["next_run_at_localized"].endswith("UTC")


def test_compute_next_run_at_preserves_local_hour_across_dst_boundary():
    after = datetime(2026, 3, 8, 11, 30, tzinfo=timezone.utc)

    next_run = compute_next_run_at(
        "0 08 * * *",
        after=after,
        task_timezone="America/New_York",
        user_timezone=None,
    )

    assert next_run is not None
    next_run_local = next_run.astimezone(ZoneInfo("America/New_York"))
    assert next_run_local.hour == 8
    assert next_run_local.minute == 0
    assert next_run_local.tzname() == "EDT"


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
