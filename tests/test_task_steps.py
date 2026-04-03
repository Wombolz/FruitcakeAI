"""
FruitcakeAI v5 — Task step planning endpoints.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.autonomy.planner import _normalize_steps
from app.db.models import AuditLog, ChatSession, LLMUsageEvent, Memory, MemoryProposal, RSSItem, RSSPublishedItem, RSSSource, Task, TaskRun, TaskRunArtifact, TaskStep
from app.autonomy.profiles.news_magazine import _ground_output
from app.autonomy.runner import _format_result_for_inbox, _persist_run_artifacts
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


async def _admin_headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": password,
            "role": "admin",
        },
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
async def test_task_create_and_patch_support_llm_model_override(client):
    headers = await _headers(client, "taskmodelowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Model-bound task",
            "instruction": "Use a specific model",
            "llm_model_override": "gpt-5-mini",
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["llm_model_override"] == "gpt-5-mini"

    updated = await client.patch(
        f"/tasks/{created.json()['id']}",
        json={"llm_model_override": "ollama_chat/qwen2.5:14b"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["llm_model_override"] == "ollama_chat/qwen2.5:14b"

    cleared = await client.patch(
        f"/tasks/{created.json()['id']}",
        json={"llm_model_override": None},
        headers=headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["llm_model_override"] is None


@pytest.mark.asyncio
async def test_magazine_plan_uses_deterministic_steps_without_approval(client):
    headers = await _headers(client, "magplanowner")
    task = await client.post(
        "/tasks",
        json={
            "title": "Daily News Magazine",
            "instruction": "Create a daily magazine from prepared data",
            "profile": "rss_newspaper",
        },
        headers=headers,
    )
    task_id = task.json()["id"]

    planned = await client.post(
        f"/tasks/{task_id}/plan",
        json={"goal": "Publish hourly news magazine", "max_steps": 8},
        headers=headers,
    )
    assert planned.status_code == 200
    assert planned.json()["steps_created"] == 2

    steps = await client.get(f"/tasks/{task_id}/steps", headers=headers)
    rows = steps.json()
    assert rows[0]["title"] == "Draft Magazine from Dataset"
    assert rows[-1]["title"] == "Final Dedupe and Publish"
    assert all(row["requires_approval"] is False for row in rows)


@pytest.mark.asyncio
async def test_maintenance_plan_uses_single_deterministic_step(client):
    headers = await _headers(client, "maintplanowner")
    task = await client.post(
        "/tasks",
        json={
            "title": "Refresh RSS cache",
            "instruction": 'tool: refresh_rss_cache\nargs: {"max_items_per_source": 20}',
            "profile": "maintenance",
        },
        headers=headers,
    )
    task_id = task.json()["id"]

    planned = await client.post(
        f"/tasks/{task_id}/plan",
        json={"goal": "Refresh RSS cache", "max_steps": 5},
        headers=headers,
    )
    assert planned.status_code == 200
    assert planned.json()["steps_created"] == 1

    steps = await client.get(f"/tasks/{task_id}/steps", headers=headers)
    rows = steps.json()
    assert [row["title"] for row in rows] == ["Execute Maintenance Action"]
    assert all(row["requires_approval"] is False for row in rows)


def test_normalize_steps_drops_redundant_task_setup_steps():
    rows = _normalize_steps(
        [
            {
                "title": "Set Daily Reminder",
                "instruction": "Create a daily reminder for 08:00 America/New_York to start the data collection process.",
                "requires_approval": False,
            },
            {
                "title": "Collect Data",
                "instruction": "Gather cached RSS items for the previous 24 hours.",
                "requires_approval": False,
            },
        ],
        max_steps=8,
    )

    assert [row["title"] for row in rows] == ["Collect Data"]


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


@pytest.mark.asyncio
async def test_tasks_summary_includes_current_step_and_waiting_tool(client):
    headers = await _headers(client, "summaryowner")
    task_resp = await client.post(
        "/tasks",
        json={"title": "Approval task", "instruction": "Do approval flow"},
        headers=headers,
    )
    task_id = task_resp.json()["id"]

    fake_steps = [
        {"title": "Prepare", "instruction": "Prepare details", "requires_approval": False},
        {"title": "Create calendar event", "instruction": "Create event", "requires_approval": True},
    ]
    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)):
        await client.post(
            f"/tasks/{task_id}/plan",
            json={"goal": "Plan with approval", "max_steps": 5},
            headers=headers,
        )

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        task.status = "waiting_approval"
        task.current_step_index = 2
        rows = await db.execute(
            select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step_index == 2)
        )
        step = rows.scalar_one()
        step.waiting_approval_tool = "create_event"
        await db.commit()

    list_resp = await client.get("/tasks", headers=headers)
    assert list_resp.status_code == 200
    listed = next(item for item in list_resp.json() if item["id"] == task_id)
    assert listed["current_step_title"] == "Create calendar event"
    assert listed["waiting_approval_tool"] == "create_event"

    detail_resp = await client.get(f"/tasks/{task_id}", headers=headers)
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["current_step_title"] == "Create calendar event"
    assert detail["waiting_approval_tool"] == "create_event"


@pytest.mark.asyncio
async def test_recurring_task_auto_plans_once_when_missing_plan(client):
    from app.autonomy.runner import TaskRunner

    headers = await _headers(client, "runnerowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Recurring plan task",
            "instruction": "Gather updates and summarize",
            "task_type": "recurring",
            "schedule": "every:30m",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    fake_steps = [
        {"title": "Collect info", "instruction": "Collect the latest updates", "requires_approval": False},
        {"title": "Summarize", "instruction": "Summarize in bullets", "requires_approval": False},
    ]

    async def _run_once():
        runner = TaskRunner()
        with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
            await runner.execute(type("TaskRef", (), {"id": task_id})())

    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)) as gen_mock:
        with patch("app.agent.core.run_agent", new=AsyncMock(return_value="ok")):
            await _run_once()
            await _run_once()

    assert gen_mock.await_count == 1


@pytest.mark.asyncio
async def test_runner_claims_task_once_under_duplicate_dispatch(client):
    from app.autonomy.runner import TaskRunner

    headers = await _headers(client, "dupedispatchowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "RSS Cache Refresh",
            "instruction": "Refresh RSS cache and notify once",
            "task_type": "one_shot",
            "deliver": True,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    async def _slow_run_agent(*_args, **_kwargs):
        await asyncio.sleep(0.01)
        return "cache refreshed"

    runner = TaskRunner()
    task_ref = type("TaskRef", (), {"id": task_id})()

    with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
        with patch("app.autonomy.runner._preflight_llm_dispatch", new=AsyncMock(return_value=None)):
            with patch("app.agent.core.run_agent", new=AsyncMock(side_effect=_slow_run_agent)) as run_agent_mock:
                with patch.object(runner, "_push", new=AsyncMock()) as push_mock:
                    await asyncio.gather(
                        runner.execute(task_ref),
                        runner.execute(task_ref),
                    )

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status == "completed"

    assert run_agent_mock.await_count == 1
    push_mock.assert_awaited_once_with(task_id, "cache refreshed")


@pytest.mark.asyncio
async def test_delete_task_removes_row(client):
    headers = await _headers(client, "deleteowner")
    task = await client.post(
        "/tasks",
        json={"title": "Delete me", "instruction": "Temporary task"},
        headers=headers,
    )
    task_id = task.json()["id"]

    delete_resp = await client.delete(f"/tasks/{task_id}", headers=headers)
    assert delete_resp.status_code == 204

    list_resp = await client.get("/tasks", headers=headers)
    assert list_resp.status_code == 200
    ids = [row["id"] for row in list_resp.json()]
    assert task_id not in ids

    detail_resp = await client.get(f"/tasks/{task_id}", headers=headers)
    assert detail_resp.status_code == 404


@pytest.mark.asyncio
async def test_recurring_task_skips_stale_queued_dispatch_after_reschedule(client):
    from app.autonomy.runner import TaskRunner

    headers = await _headers(client, "stalequeuedowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Recurring digest",
            "instruction": "Compile recurring updates",
            "task_type": "recurring",
            "schedule": "every:30m",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        queued_run_at = task.next_run_at

    assert queued_run_at is not None

    runner = TaskRunner()
    with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
        with patch("app.autonomy.runner._preflight_llm_dispatch", new=AsyncMock(return_value=None)):
                with patch("app.agent.core.run_agent", new=AsyncMock(return_value="FINAL RECURRING OUTPUT")) as run_agent_mock:
                    with patch.object(runner, "_push", new=AsyncMock()):
                        await runner._run(
                            task_id,
                            expected_next_run_at=queued_run_at,
                            trigger_source="test",
                        )
                        await_count_after_first = run_agent_mock.await_count
                        await runner._run(
                            task_id,
                            expected_next_run_at=queued_run_at,
                            trigger_source="test",
                        )

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status == "pending"
        assert task.next_run_at is not None
        assert task.next_run_at > queued_run_at

        runs = await db.execute(
            select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.started_at.desc())
        )
        run_rows = runs.scalars().all()
        assert len(run_rows) == 1
        assert run_rows[0].status == "completed"

    assert await_count_after_first > 0
    assert run_agent_mock.await_count == await_count_after_first


@pytest.mark.asyncio
async def test_stop_task_marks_task_and_run_cancelled(client):
    headers = await _headers(client, "stopowner")
    task = await client.post(
        "/tasks",
        json={"title": "Stop me", "instruction": "Long running task"},
        headers=headers,
    )
    task_id = task.json()["id"]

    async with TestSessionLocal() as db:
        row = await db.get(Task, task_id)
        row.status = "running"
        run = TaskRun(task_id=task_id, status="running")
        db.add(run)
        await db.commit()

    stop_resp = await client.post(f"/tasks/{task_id}/stop", headers=headers)
    assert stop_resp.status_code == 200
    payload = stop_resp.json()
    assert payload["status"] == "cancelled"
    assert payload["error"] == "Stopped by user"

    async with TestSessionLocal() as db:
        row = await db.get(Task, task_id)
        assert row.status == "cancelled"
        runs = await db.execute(
            select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.started_at.desc())
        )
        latest = runs.scalars().first()
        assert latest is not None
        assert latest.status == "cancelled"


def test_format_result_for_inbox_splits_single_line_prose():
    text = (
        "Here are the highlights. Markets rose on policy news. "
        "Analysts expect volatility tomorrow."
    )
    formatted = _format_result_for_inbox(text)
    assert "\n\n" in formatted
    assert formatted.startswith("Here are the highlights.")


def test_format_result_for_inbox_preserves_existing_newlines():
    text = "Line one.\n\nLine two."
    assert _format_result_for_inbox(text) == text


def test_ground_magazine_output_removes_unverified_links():
    source_text = (
        "1. Story One\n"
        "URL: https://news.example.org/one\n"
        "2. Story Two\n"
        "URL: https://bad.example.org/two\n"
    )
    cleaned, report = _ground_output(
        source_text,
        allowed_urls={"https://news.example.org/one"},
    )
    assert report["fatal"] is False
    assert "https://news.example.org/one" in cleaned
    assert "https://bad.example.org/two" not in cleaned


def test_ground_magazine_output_fails_when_all_links_ungrounded():
    source_text = "URL: https://fake.example.org/a\nURL: https://fake.example.org/b"
    cleaned, report = _ground_output(
        source_text,
        allowed_urls={"https://real.example.org/a"},
    )
    assert cleaned == ""
    assert report["fatal"] is True
    assert "fatal_reason" in report


@pytest.mark.asyncio
async def test_planned_task_uses_last_step_result_as_final_output(client):
    from app.autonomy.runner import TaskRunner

    headers = await _headers(client, "finalstepowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "News summary once",
            "instruction": "Get top headlines and summarize",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    fake_steps = [
        {"title": "Collect sources", "instruction": "Find sources", "requires_approval": False},
        {"title": "Final synthesis", "instruction": "Produce final output", "requires_approval": False},
    ]
    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)):
        plan_resp = await client.post(
            f"/tasks/{task_id}/plan",
            json={"goal": "Top headlines", "max_steps": 4},
            headers=headers,
        )
    assert plan_resp.status_code == 200

    async def _run_once():
        runner = TaskRunner()
        with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
            await runner.execute(type("TaskRef", (), {"id": task_id})())

    with patch("app.agent.core.run_agent", new=AsyncMock(side_effect=["INTERMEDIATE DRAFT", "FINAL SYNTHESIS OUTPUT"])):
        await _run_once()

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.result == "FINAL SYNTHESIS OUTPUT"


@pytest.mark.asyncio
async def test_recurring_run_summary_keeps_step_snapshot_before_reset(client):
    from app.autonomy.runner import TaskRunner

    headers = await _headers(client, "snapshotowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Recurring digest",
            "instruction": "Compile recurring updates",
            "task_type": "recurring",
            "schedule": "every:30m",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    fake_steps = [
        {"title": "Gather", "instruction": "Gather updates", "requires_approval": False},
        {"title": "Synthesize", "instruction": "Synthesize result", "requires_approval": False},
    ]

    async def _run_once():
        runner = TaskRunner()
        with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
            await runner.execute(type("TaskRef", (), {"id": task_id})())

    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)):
        with patch("app.agent.core.run_agent", new=AsyncMock(side_effect=["GATHERED DATA", "FINAL RECURRING OUTPUT"])):
            await _run_once()

    async with TestSessionLocal() as db:
        runs = await db.execute(
            select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.started_at.desc())
        )
        latest = runs.scalars().first()
        assert latest is not None
        assert latest.status == "completed"
        assert latest.summary is not None
        assert "Step snapshot from previous run" in latest.summary
        assert "Step 1: Gather" in latest.summary
        assert "Step 2: Synthesize" in latest.summary


@pytest.mark.asyncio
async def test_create_task_infers_persona_and_validates_explicit_persona(client):
    headers = await _headers(client, "personarouteowner")

    inferred = await client.post(
        "/tasks",
        json={
            "title": "Get today's top headlines",
            "instruction": "Find today's news and summarize top stories",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    assert inferred.status_code == 201
    assert inferred.json()["persona"] == "news_researcher"

    explicit = await client.post(
        "/tasks",
        json={
            "title": "Project status",
            "instruction": "Draft work update for stakeholders",
            "persona": "work_assistant",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    assert explicit.status_code == 201
    assert explicit.json()["persona"] == "work_assistant"

    invalid = await client.post(
        "/tasks",
        json={
            "title": "Bad persona",
            "instruction": "Test invalid persona",
            "persona": "does_not_exist",
        },
        headers=headers,
    )
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_create_task_infers_topic_watcher_profile_for_clear_recurring_watch_request(client):
    headers = await _headers(client, "watchprofileowner")

    inferred = await client.post(
        "/tasks",
        json={
            "title": "OpenClaw Watch",
            "instruction": "Watch my RSS feeds for OpenClaw and summarize new headlines.",
            "task_type": "recurring",
            "schedule": "every:6h",
            "deliver": True,
        },
        headers=headers,
    )
    assert inferred.status_code == 201
    assert inferred.json()["profile"] == "topic_watcher"


@pytest.mark.asyncio
async def test_create_and_patch_task_profile_validation(client):
    headers = await _headers(client, "profileowner")

    created = await client.post(
        "/tasks",
        json={
            "title": "Magazine",
            "instruction": "Build periodic magazine",
            "profile": "rss_newspaper",
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["profile"] == "rss_newspaper"

    morning = await client.post(
        "/tasks",
        json={
            "title": "Morning briefing",
            "instruction": "Brief me on my day",
            "profile": "morning_briefing",
        },
        headers=headers,
    )
    assert morning.status_code == 201
    assert morning.json()["profile"] == "morning_briefing"

    watcher = await client.post(
        "/tasks",
        json={
            "title": "Watcher",
            "instruction": "topic: AI regulation",
            "profile": "topic_watcher",
        },
        headers=headers,
    )
    assert watcher.status_code == 201
    assert watcher.json()["profile"] == "topic_watcher"

    maintenance = await client.post(
        "/tasks",
        json={
            "title": "Maintenance",
            "instruction": "tool: refresh_rss_cache",
            "profile": "maintenance",
        },
        headers=headers,
    )
    assert maintenance.status_code == 201
    assert maintenance.json()["profile"] == "maintenance"

    patch = await client.patch(
        f"/tasks/{created.json()['id']}",
        json={"profile": "default"},
        headers=headers,
    )
    assert patch.status_code == 200
    assert patch.json()["profile"] == "default"

    invalid = await client.patch(
        f"/tasks/{created.json()['id']}",
        json={"profile": "not_real"},
        headers=headers,
    )
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_maintenance_profile_runner_requires_exact_tool_output(client):
    from app.autonomy.runner import TaskRunner

    headers = await _headers(client, "maintowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Maintenance task: refresh RSS cache.",
            "instruction": 'tool: refresh_rss_cache\nargs: {"max_items_per_source": 20}',
            "profile": "maintenance",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    plan_resp = await client.post(
        f"/tasks/{task_id}/plan",
        json={"goal": "Refresh RSS cache", "max_steps": 5},
        headers=headers,
    )
    assert plan_resp.status_code == 200

    runner = TaskRunner()
    task_ref = type("TaskRef", (), {"id": task_id})()
    tool_records = [
        {
            "tool": "refresh_rss_cache",
            "result_summary": "sources_refreshed=12 items_cached=150",
        }
    ]

    with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
        with patch("app.autonomy.runner._preflight_llm_dispatch", new=AsyncMock(return_value=None)):
            with patch(
                "app.autonomy.runner.get_tool_execution_records",
                side_effect=[tool_records],
            ):
                with patch("app.autonomy.runner.reset_tool_execution_records", return_value=object()):
                    with patch("app.autonomy.runner.restore_tool_execution_records", return_value=None):
                        with patch(
                            "app.autonomy.runner.TaskRunner._run_step_with_model_policy",
                            new=AsyncMock(return_value="sources_refreshed=12 items_cached=150"),
                        ):
                            await runner.execute(task_ref)

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.result == "sources_refreshed=12 items_cached=150"
        run_rows = await db.execute(
            select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.started_at.desc())
        )
        run = run_rows.scalars().first()
        assert run is not None
        artifacts = await db.execute(
            select(TaskRunArtifact).where(TaskRunArtifact.task_run_id == run.id)
        )
        validation = next(
            artifact for artifact in artifacts.scalars().all() if artifact.artifact_type == "validation_report"
        )
        payload = json.loads(validation.content_json)
        assert payload["declared_tool"] == "refresh_rss_cache"
        assert payload["declared_tool_called"] is True
        assert payload["exact_output_match"] is True


@pytest.mark.asyncio
async def test_admin_task_run_inspect_returns_ordered_payloads_and_diagnostics(client):
    admin_headers = await _admin_headers(client, "inspectadmin")
    owner_headers = await _headers(client, "inspectowner")

    task_resp = await client.post(
        "/tasks",
        json={
            "title": "Inspect me",
            "instruction": "Build debug payload",
            "profile": "rss_newspaper",
        },
        headers=owner_headers,
    )
    task_id = task_resp.json()["id"]

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        session = ChatSession(user_id=task.user_id, title="task session", is_task_session=True)
        db.add(session)
        await db.flush()

        run = TaskRun(
            task_id=task_id,
            session_id=session.id,
            status="completed",
            summary="Published magazine",
            started_at=datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 3, 18, 12, 5, tzinfo=timezone.utc),
        )
        db.add(run)
        await db.flush()

        db.add_all(
            [
                AuditLog(
                    user_id=task.user_id,
                    tool="list_recent_feed_items",
                    arguments='{"max_results": 5}',
                    result_summary="Recent feed items (5)",
                    session_id=session.id,
                    created_at=datetime(2026, 3, 18, 12, 1, tzinfo=timezone.utc),
                ),
                AuditLog(
                    user_id=task.user_id,
                    tool="render_magazine",
                    arguments='{"section":"Top"}',
                    result_summary="Rendered top section",
                    session_id=session.id,
                    created_at=datetime(2026, 3, 18, 12, 2, tzinfo=timezone.utc),
                ),
            ]
        )
        db.add_all(
            [
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="validation_report",
                    content_json='{"publish_mode":"full","fatal":false}',
                ),
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="final_output",
                    content_text="# Final magazine",
                ),
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="edition_export",
                    content_json='{"pdf_relative_path":"exports/newspapers/task-1/2026-03-18/demo/edition.pdf","download_path":"/admin/task-runs/1/edition.pdf"}',
                ),
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="prepared_dataset",
                    content_json='{"stats":{"selected_count":12},"refresh":{"sources_refreshed":9}}',
                ),
                TaskRunArtifact(
                    task_run_id=run.id,
                    artifact_type="run_diagnostics",
                    content_json='{"active_skills":["rss-grounded-briefing"],"skill_selection_mode":"embedding","skill_injection_events":[{"stage":"step_1"}],"dataset_stats":{"selected_count":12},"refresh_stats":{"sources_refreshed":9},"suppression_events":[]}',
                ),
            ]
        )
        await db.commit()
        run_id = run.id

    inspect = await client.get(f"/admin/task-runs/{run_id}/inspect", headers=admin_headers)
    assert inspect.status_code == 200
    payload = inspect.json()

    assert payload["run"]["id"] == run_id
    assert payload["run"]["duration_seconds"] == 300.0
    assert payload["task"]["id"] == task_id
    assert payload["task"]["profile"] == "rss_newspaper"
    assert payload["execution"]["active_skills"] == ["rss-grounded-briefing"]
    assert payload["execution"]["refresh_stats"] == {"sources_refreshed": 9}
    assert [row["tool"] for row in payload["tool_timeline"]] == [
        "list_recent_feed_items",
        "render_magazine",
    ]
    assert [row["artifact_type"] for row in payload["artifacts"]] == [
        "prepared_dataset",
        "final_output",
        "edition_export",
        "validation_report",
        "run_diagnostics",
    ]
    assert payload["diagnostics"]["active_skills"] == ["rss-grounded-briefing"]
    assert payload["diagnostics"]["validation_report"]["publish_mode"] == "full"
    assert payload["execution"]["edition_export"]["pdf_relative_path"].endswith("edition.pdf")


@pytest.mark.asyncio
async def test_approve_topic_watcher_memory_candidate_creates_memory_and_updates_artifact(client):
    headers = await _headers(client, "memorycandidateowner")
    admin_headers = await _admin_headers(client, "memorycandidateadmin")

    task_resp = await client.post(
        "/tasks",
        json={
            "title": "Iran Watch",
            "instruction": "topic: Iran\nthreshold: medium",
            "profile": "topic_watcher",
        },
        headers=headers,
    )
    task_id = task_resp.json()["id"]

    async with TestSessionLocal() as db:
        run = TaskRun(
            task_id=task_id,
            status="completed",
            summary="Watcher fired",
            started_at=datetime(2026, 3, 25, 1, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 3, 25, 1, 2, tzinfo=timezone.utc),
        )
        db.add(run)
        await db.flush()
        db.add(
            TaskRunArtifact(
                task_run_id=run.id,
                artifact_type="memory_candidates",
                content_json=json.dumps(
                    {
                        "candidates": [
                            {
                                "memory_type": "episodic",
                                "content": "On 2026-03-25, reports about Iran indicated renewed diplomatic talks and sanctions pressure.",
                                "topic": "Iran",
                                "supporting_urls": ["https://example.com/1", "https://example.com/2"],
                                "source_names": ["Reuters", "BBC"],
                                "reason": "Strong medium-threshold watcher hit.",
                                "confidence": 0.8,
                                "expires_at": "2026-04-24T00:00:00+00:00",
                                "status": "pending",
                                "approved_memory_id": None,
                                "approved_at": None,
                                "approved_by_user_id": None,
                            }
                        ]
                    }
                ),
            )
        )
        await db.commit()
        run_id = run.id

    approve = await client.post(
        f"/tasks/{task_id}/runs/{run_id}/memory-candidates/0/approve",
        headers=headers,
    )
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["task_id"] == task_id
    assert payload["run_id"] == run_id
    assert payload["candidate_index"] == 0
    assert payload["candidate"]["status"] == "approved"
    assert payload["candidate"]["approved_by_user_id"] is not None
    assert payload["memory"]["memory_type"] == "episodic"
    assert payload["memory"]["content"].startswith("On 2026-03-25")
    assert payload["memory"]["expires_at"] == "2026-04-24T00:00:00Z"
    assert "topic_watcher" in payload["memory"]["tags"]
    assert "iran" in payload["memory"]["tags"]

    async with TestSessionLocal() as db:
        memories = (await db.execute(select(Memory).order_by(Memory.id.desc()))).scalars().all()
        assert len(memories) == 1
        assert memories[0].content == payload["memory"]["content"]
        artifact = (
            await db.execute(
                select(TaskRunArtifact).where(
                    TaskRunArtifact.task_run_id == run_id,
                    TaskRunArtifact.artifact_type == "memory_candidates",
                )
            )
        ).scalar_one()
        artifact_payload = json.loads(artifact.content_json)
        assert artifact_payload["candidates"][0]["status"] == "approved"
        assert artifact_payload["candidates"][0]["approved_memory_id"] == memories[0].id

    inspect = await client.get(f"/admin/task-runs/{run_id}/inspect", headers=admin_headers)
    assert inspect.status_code == 200
    inspect_payload = inspect.json()
    candidate_artifact = next(
        row for row in inspect_payload["artifacts"] if row["artifact_type"] == "memory_candidates"
    )
    assert candidate_artifact["content_json"]["candidates"][0]["status"] == "approved"


@pytest.mark.asyncio
async def test_approve_topic_watcher_memory_candidate_rejects_duplicate_approval(client):
    headers = await _headers(client, "memoryduplicateowner")

    task_resp = await client.post(
        "/tasks",
        json={"title": "Watcher", "instruction": "topic: AI", "profile": "topic_watcher"},
        headers=headers,
    )
    task_id = task_resp.json()["id"]

    async with TestSessionLocal() as db:
        run = TaskRun(task_id=task_id, status="completed", summary="done")
        db.add(run)
        await db.flush()
        db.add(
            TaskRunArtifact(
                task_run_id=run.id,
                artifact_type="memory_candidates",
                content_json=json.dumps(
                    {
                        "candidates": [
                            {
                                "memory_type": "semantic",
                                "content": "AI regulation entered a new review phase.",
                                "topic": "AI regulation",
                                "supporting_urls": ["https://example.com/ai"],
                                "source_names": ["Reuters"],
                                "reason": "Strong watcher hit.",
                                "confidence": 0.82,
                                "expires_at": "2026-04-24T00:00:00+00:00",
                                "status": "approved",
                                "approved_memory_id": 99,
                                "approved_at": "2026-03-25T02:00:00+00:00",
                                "approved_by_user_id": 1,
                            }
                        ]
                    }
                ),
            )
        )
        await db.commit()
        run_id = run.id

    approve = await client.post(
        f"/tasks/{task_id}/runs/{run_id}/memory-candidates/0/approve",
        headers=headers,
    )
    assert approve.status_code == 409


@pytest.mark.asyncio
async def test_approve_topic_watcher_memory_candidate_rejects_other_users_run(client):
    owner_headers = await _headers(client, "memoryowner")
    other_headers = await _headers(client, "memoryother")

    task_resp = await client.post(
        "/tasks",
        json={"title": "Watcher", "instruction": "topic: Iran", "profile": "topic_watcher"},
        headers=owner_headers,
    )
    task_id = task_resp.json()["id"]

    async with TestSessionLocal() as db:
        run = TaskRun(task_id=task_id, status="completed", summary="done")
        db.add(run)
        await db.flush()
        db.add(
            TaskRunArtifact(
                task_run_id=run.id,
                artifact_type="memory_candidates",
                content_json=json.dumps(
                    {
                        "candidates": [
                            {
                                "memory_type": "episodic",
                                "content": "On 2026-03-25, reports about Iran indicated renewed diplomatic talks.",
                                "topic": "Iran",
                                "supporting_urls": ["https://example.com/iran"],
                                "source_names": ["BBC"],
                                "reason": "Strong watcher hit.",
                                "confidence": 0.8,
                                "expires_at": "2026-04-24T00:00:00+00:00",
                                "status": "pending",
                                "approved_memory_id": None,
                                "approved_at": None,
                                "approved_by_user_id": None,
                            }
                        ]
                    }
                ),
            )
        )
        await db.commit()
        run_id = run.id

    approve = await client.post(
        f"/tasks/{task_id}/runs/{run_id}/memory-candidates/0/approve",
        headers=other_headers,
    )
    assert approve.status_code == 404


@pytest.mark.asyncio
async def test_approve_topic_watcher_memory_candidate_returns_404_without_artifact(client):
    headers = await _headers(client, "memorymissingartifact")
    task_resp = await client.post(
        "/tasks",
        json={"title": "Watcher", "instruction": "topic: Iran", "profile": "topic_watcher"},
        headers=headers,
    )
    task_id = task_resp.json()["id"]

    async with TestSessionLocal() as db:
        run = TaskRun(task_id=task_id, status="completed", summary="done")
        db.add(run)
        await db.commit()
        run_id = run.id

    approve = await client.post(
        f"/tasks/{task_id}/runs/{run_id}/memory-candidates/0/approve",
        headers=headers,
    )
    assert approve.status_code == 404


@pytest.mark.asyncio
async def test_memory_review_list_and_approve_proposal(client):
    headers = await _headers(client, "reviewowner")

    task_resp = await client.post(
        "/tasks",
        json={"title": "Watcher", "instruction": "topic: Iran", "profile": "topic_watcher"},
        headers=headers,
    )
    task_id = task_resp.json()["id"]

    async with TestSessionLocal() as db:
        run = TaskRun(task_id=task_id, status="completed", summary="done")
        db.add(run)
        await db.flush()
        proposal = MemoryProposal(
            proposal_key="proposal-review-1",
            user_id=1,
            proposal_type="flat_memory_create",
            source_type="topic_watcher",
            status="pending",
            task_id=task_id,
            task_run_id=run.id,
            content="On 2026-03-25, reports about Iran indicated renewed diplomatic talks.",
            confidence=0.8,
            reason="Strong watcher hit.",
        )
        proposal.proposal_payload = {
            "proposal_key": "proposal-review-1",
            "memory_type": "episodic",
            "content": "On 2026-03-25, reports about Iran indicated renewed diplomatic talks.",
            "topic": "Iran",
            "supporting_urls": ["https://example.com/iran-1"],
            "source_names": ["Reuters"],
            "reason": "Strong watcher hit.",
            "confidence": 0.8,
            "expires_at": "2026-04-24T00:00:00+00:00",
        }
        db.add(proposal)
        db.add(
            TaskRunArtifact(
                task_run_id=run.id,
                artifact_type="memory_candidates",
                content_json=json.dumps(
                    {
                        "candidates": [
                            {
                                "proposal_key": "proposal-review-1",
                                "memory_type": "episodic",
                                "content": "On 2026-03-25, reports about Iran indicated renewed diplomatic talks.",
                                "topic": "Iran",
                                "supporting_urls": ["https://example.com/iran-1"],
                                "source_names": ["Reuters"],
                                "reason": "Strong watcher hit.",
                                "confidence": 0.8,
                                "status": "pending",
                                "proposal_id": None,
                            }
                        ]
                    }
                ),
            )
        )
        await db.commit()
        proposal_id = proposal.id
        run_id = run.id

    listing = await client.get("/memories/review", headers=headers)
    assert listing.status_code == 200
    items = listing.json()
    assert len(items) == 1
    assert items[0]["id"] == proposal_id
    assert items[0]["status"] == "pending"
    assert items[0]["proposal"]["topic"] == "Iran"

    detail = await client.get(f"/memories/review/{proposal_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["id"] == proposal_id

    approve = await client.post(f"/memories/review/{proposal_id}/approve", headers=headers)
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["proposal"]["status"] == "approved"
    assert payload["memory"]["memory_type"] == "episodic"
    assert payload["memory"]["expires_at"] == "2026-04-24T00:00:00"
    assert "topic_watcher" in payload["memory"]["tags"]

    async with TestSessionLocal() as db:
        proposal = await db.get(MemoryProposal, proposal_id)
        assert proposal is not None
        assert proposal.status == "approved"
        assert proposal.approved_memory_id is not None
        artifact = (
            await db.execute(
                select(TaskRunArtifact).where(
                    TaskRunArtifact.task_run_id == run_id,
                    TaskRunArtifact.artifact_type == "memory_candidates",
                )
            )
        ).scalar_one()
        artifact_payload = json.loads(artifact.content_json)
        assert artifact_payload["candidates"][0]["status"] == "approved"
        assert artifact_payload["candidates"][0]["proposal_id"] == proposal_id


@pytest.mark.asyncio
async def test_memory_review_reject_updates_status_without_creating_memory(client):
    headers = await _headers(client, "reviewrejectowner")

    async with TestSessionLocal() as db:
        task = Task(user_id=1, title="Watcher", instruction="topic: AI", profile="topic_watcher")
        db.add(task)
        await db.flush()
        run = TaskRun(task_id=task.id, status="completed", summary="done")
        db.add(run)
        await db.flush()
        proposal = MemoryProposal(
            proposal_key="proposal-review-reject",
            user_id=1,
            proposal_type="flat_memory_create",
            source_type="topic_watcher",
            status="pending",
            task_id=task.id,
            task_run_id=run.id,
            content="AI regulation entered a new review phase.",
            confidence=0.72,
            reason="Strong watcher hit.",
        )
        proposal.proposal_payload = {
            "proposal_key": "proposal-review-reject",
            "memory_type": "semantic",
            "content": "AI regulation entered a new review phase.",
            "topic": "AI regulation",
            "supporting_urls": ["https://example.com/ai-1"],
            "source_names": ["Reuters"],
            "reason": "Strong watcher hit.",
            "confidence": 0.72,
            "expires_at": "2026-04-24T00:00:00+00:00",
        }
        db.add(proposal)
        await db.commit()
        proposal_id = proposal.id

    reject = await client.post(f"/memories/review/{proposal_id}/reject", headers=headers)
    assert reject.status_code == 200
    assert reject.json()["status"] == "rejected"

    async with TestSessionLocal() as db:
        proposal = await db.get(MemoryProposal, proposal_id)
        assert proposal is not None
        assert proposal.status == "rejected"
        memories = (await db.execute(select(Memory))).scalars().all()
        assert memories == []


@pytest.mark.asyncio
async def test_memory_review_approve_is_idempotent_for_already_approved_proposal(client):
    headers = await _headers(client, "reviewduplicateowner")

    async with TestSessionLocal() as db:
        memory = Memory(
            user_id=1,
            memory_type="semantic",
            content="Duplicate proposal",
            importance=0.65,
            tags=json.dumps(["topic_watcher", "iran"]),
            is_active=True,
        )
        db.add(memory)
        await db.flush()
        proposal = MemoryProposal(
            proposal_key="proposal-review-duplicate",
            user_id=1,
            proposal_type="flat_memory_create",
            source_type="topic_watcher",
            status="approved",
            content="Duplicate proposal",
            confidence=0.7,
            reason="Strong watcher hit.",
            approved_memory_id=memory.id,
            resolved_by_user_id=1,
            resolved_at=datetime(2026, 3, 25, 4, 0, tzinfo=timezone.utc),
        )
        proposal.proposal_payload = {
            "proposal_key": "proposal-review-duplicate",
            "memory_type": "semantic",
            "content": "Duplicate proposal",
            "topic": "Iran",
            "supporting_urls": [],
            "source_names": [],
            "reason": "Strong watcher hit.",
            "confidence": 0.7,
            "expires_at": "2026-04-24T00:00:00+00:00",
        }
        db.add(proposal)
        await db.commit()
        proposal_id = proposal.id

    approve = await client.post(f"/memories/review/{proposal_id}/approve", headers=headers)
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["proposal"]["status"] == "approved"
    assert payload["memory"]["content"] == "Duplicate proposal"


@pytest.mark.asyncio
async def test_memory_review_approve_links_existing_duplicate_memory(client):
    headers = await _headers(client, "reviewexistingmemoryowner")

    async with TestSessionLocal() as db:
        memory = Memory(
            user_id=1,
            memory_type="episodic",
            content="On 2026-03-25, reports about Iran indicated renewed diplomatic talks.",
            importance=0.65,
            tags=json.dumps(["topic_watcher", "iran"]),
            is_active=True,
        )
        db.add(memory)
        await db.flush()
        proposal = MemoryProposal(
            proposal_key="proposal-review-existing-memory",
            user_id=1,
            proposal_type="flat_memory_create",
            source_type="topic_watcher",
            status="pending",
            content="On 2026-03-25, reports about Iran indicated renewed diplomatic talks.",
            confidence=0.8,
            reason="Strong watcher hit.",
        )
        proposal.proposal_payload = {
            "proposal_key": "proposal-review-existing-memory",
            "memory_type": "episodic",
            "content": "On 2026-03-25, reports about Iran indicated renewed diplomatic talks.",
            "topic": "Iran",
            "supporting_urls": ["https://example.com/iran-1"],
            "source_names": ["Reuters"],
            "reason": "Strong watcher hit.",
            "confidence": 0.8,
            "expires_at": "2026-04-24T00:00:00+00:00",
        }
        db.add(proposal)
        await db.commit()
        proposal_id = proposal.id
        memory_id = memory.id

    approve = await client.post(f"/memories/review/{proposal_id}/approve", headers=headers)
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["proposal"]["status"] == "approved"
    assert payload["proposal"]["approved_memory_id"] == memory_id
    assert payload["memory"]["id"] == memory_id


@pytest.mark.asyncio
async def test_memory_review_scopes_proposals_to_current_user(client):
    owner_headers = await _headers(client, "reviewscopeowner")
    other_headers = await _headers(client, "reviewscopeother")

    async with TestSessionLocal() as db:
        proposal = MemoryProposal(
            proposal_key="proposal-scope-1",
            user_id=1,
            proposal_type="flat_memory_create",
            source_type="topic_watcher",
            status="pending",
            content="Scoped proposal",
            confidence=0.6,
            reason="Strong watcher hit.",
        )
        proposal.proposal_payload = {
            "proposal_key": "proposal-scope-1",
            "memory_type": "semantic",
            "content": "Scoped proposal",
            "topic": "Iran",
            "supporting_urls": [],
            "source_names": [],
            "reason": "Strong watcher hit.",
            "confidence": 0.6,
            "expires_at": "2026-04-24T00:00:00+00:00",
        }
        db.add(proposal)
        await db.commit()
        proposal_id = proposal.id

    own_list = await client.get("/memories/review", headers=owner_headers)
    assert own_list.status_code == 200
    assert len(own_list.json()) == 1

    other_list = await client.get("/memories/review", headers=other_headers)
    assert other_list.status_code == 200
    assert other_list.json() == []

    other_detail = await client.get(f"/memories/review/{proposal_id}", headers=other_headers)
    assert other_detail.status_code == 404


@pytest.mark.asyncio
async def test_memories_usage_returns_latest_user_scoped_events(client):
    owner_headers = await _headers(client, "usageowner")
    other_headers = await _headers(client, "usageother")

    async with TestSessionLocal() as db:
        db.add_all(
            [
                LLMUsageEvent(
                    user_id=1,
                    task_id=53,
                    task_run_id=941,
                    source="task_runner",
                    stage="task_final",
                    model="gpt-4o",
                    prompt_tokens=120,
                    completion_tokens=30,
                    total_tokens=150,
                    estimated_cost_usd=0.012,
                    created_at=datetime(2026, 3, 25, 18, 0, tzinfo=timezone.utc),
                ),
                LLMUsageEvent(
                    user_id=1,
                    session_id=188,
                    source="chat_rest",
                    stage="chat_simple",
                    model="qwen2.5:32b",
                    prompt_tokens=80,
                    completion_tokens=20,
                    total_tokens=100,
                    estimated_cost_usd=0.0,
                    created_at=datetime(2026, 3, 25, 17, 0, tzinfo=timezone.utc),
                ),
                LLMUsageEvent(
                    user_id=2,
                    task_id=99,
                    source="task_runner",
                    stage="task_final",
                    model="gpt-4o-mini",
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    estimated_cost_usd=0.001,
                    created_at=datetime(2026, 3, 25, 19, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        await db.commit()

    resp = await client.get("/memories/usage", headers=owner_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert rows[0]["scope_label"] == "task:53"
    assert rows[0]["task_id"] == 53
    assert rows[0]["task_run_id"] == 941
    assert rows[0]["stage"] == "task_final"
    assert rows[0]["model"] == "gpt-4o"
    assert rows[0]["total_tokens"] == 150
    assert rows[1]["scope_label"] == "chat:188"
    assert rows[1]["session_id"] == 188
    assert rows[1]["source"] == "chat_rest"

    limited = await client.get("/memories/usage?limit=1", headers=owner_headers)
    assert limited.status_code == 200
    assert len(limited.json()) == 1
    assert limited.json()[0]["scope_label"] == "task:53"

    other = await client.get("/memories/usage", headers=other_headers)
    assert other.status_code == 200
    assert len(other.json()) == 1
    assert other.json()[0]["scope_label"] == "task:99"


@pytest.mark.asyncio
async def test_admin_task_run_inspect_handles_sparse_runs(client):
    admin_headers = await _admin_headers(client, "inspectadmin2")
    owner_headers = await _headers(client, "inspectowner2")

    task_resp = await client.post(
        "/tasks",
        json={"title": "Sparse inspect", "instruction": "No artifacts"},
        headers=owner_headers,
    )
    task_id = task_resp.json()["id"]

    async with TestSessionLocal() as db:
        run = TaskRun(
            task_id=task_id,
            status="cancelled",
            error="paused_unavailable",
            summary="No run data",
            started_at=datetime(2026, 3, 18, 13, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 3, 18, 13, 1, tzinfo=timezone.utc),
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    inspect = await client.get(f"/admin/task-runs/{run_id}/inspect", headers=admin_headers)
    assert inspect.status_code == 200
    payload = inspect.json()
    assert payload["run"]["error"] == "paused_unavailable"
    assert payload["tool_timeline"] == []
    assert payload["artifacts"] == []
    assert payload["diagnostics"]["active_skills"] == []


@pytest.mark.asyncio
async def test_admin_task_run_inspect_returns_404_for_unknown_run(client):
    admin_headers = await _admin_headers(client, "inspectadmin3")
    resp = await client.get("/admin/task-runs/999999/inspect", headers=admin_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_task_run_edition_pdf_download_returns_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    admin_headers = await _admin_headers(client, "editionadmin")
    owner_headers = await _headers(client, "editionowner")

    task_resp = await client.post(
        "/tasks",
        json={"title": "Edition download", "instruction": "Publish magazine", "profile": "rss_newspaper"},
        headers=owner_headers,
    )
    task_id = task_resp.json()["id"]

    pdf_dir = tmp_path / "exports" / "newspapers" / "task-1" / "2026-03-18" / "demo"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / "edition.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% demo pdf\n")

    async with TestSessionLocal() as db:
        run = TaskRun(
            task_id=task_id,
            status="completed",
            started_at=datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 3, 18, 12, 5, tzinfo=timezone.utc),
        )
        db.add(run)
        await db.flush()
        db.add(
            TaskRunArtifact(
                task_run_id=run.id,
                artifact_type="edition_export",
                content_json='{"pdf_relative_path":"exports/newspapers/task-1/2026-03-18/demo/edition.pdf"}',
            )
        )
        await db.commit()
        run_id = run.id

    resp = await client.get(f"/admin/task-runs/{run_id}/edition.pdf", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content.startswith(b"%PDF-1.4")


@pytest.mark.asyncio
async def test_persist_run_artifacts_exports_full_news_magazine_edition(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))

    async with TestSessionLocal() as db:
        task = Task(
            user_id=1,
            title="Hourly paper",
            instruction="Publish hourly paper",
            profile="rss_newspaper",
            status="completed",
        )
        db.add(task)
        await db.flush()

        run = TaskRun(
            task_id=task.id,
            session_id=688,
            status="completed",
            started_at=datetime(2026, 3, 19, 15, 54, tzinfo=timezone.utc),
            finished_at=datetime(2026, 3, 19, 16, 0, 21, tzinfo=timezone.utc),
        )
        db.add(run)
        await db.flush()

        markdown = (
            "Fruitcake News\n"
            "Top Stories\n"
            "- **Headline:** Story One\n"
            "Source: Reuters\n"
            "Published at: 2026-03-19T15:56:03+00:00\n"
            "Summary: First summary.\n"
            "[Read More](https://example.com/one)\n"
        )
        run_debug = {
            "profile": "rss_newspaper",
            "grounding_report": {"publish_mode": "partial", "fatal": False},
            "dataset_stats": {"selected_count": 100},
            "refresh_stats": {"sources_refreshed": 137},
            "active_skills": ["rss-grounded-briefing"],
        }

        await _persist_run_artifacts(
            db,
            task_run_id=run.id,
            final_markdown=markdown,
            run_debug=run_debug,
        )
        await db.commit()

        artifacts = (
            await db.execute(select(TaskRunArtifact).where(TaskRunArtifact.task_run_id == run.id))
        ).scalars().all()
        by_type = {a.artifact_type: a for a in artifacts}
        assert "edition_export" in by_type
        payload = json.loads(by_type["edition_export"].content_json)
        assert payload["task_id"] == task.id
        assert payload["task_run_id"] == run.id
        assert payload["publish_mode"] == "partial"
        assert payload["download_path"] == f"/admin/task-runs/{run.id}/edition.pdf"

        pdf_path = tmp_path / payload["pdf_relative_path"]
        md_path = tmp_path / payload["markdown_relative_path"]
        manifest_path = tmp_path / payload["manifest_relative_path"]
        assert pdf_path.exists()
        assert md_path.exists()
        assert manifest_path.exists()


@pytest.mark.asyncio
async def test_runner_lazy_backfills_task_persona_and_uses_it_for_session(client):
    from app.autonomy.runner import TaskRunner

    headers = await _headers(client, "personabackfillowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Top headlines run",
            "instruction": "Find current news headlines",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    # Simulate a legacy row created before task.persona existed.
    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        task.persona = None
        await db.commit()

    runner = TaskRunner()
    with patch("app.agent.core.run_agent", new=AsyncMock(return_value="ok")):
        with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
            await runner.execute(type("TaskRef", (), {"id": task_id})())

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.persona == "news_researcher"
        assert task.last_session_id is not None
        session = await db.get(ChatSession, task.last_session_id)
        assert session is not None
        assert session.persona == "news_researcher"


@pytest.mark.asyncio
async def test_runner_uses_small_for_intermediate_and_large_for_final_step(client, monkeypatch):
    from app.autonomy.runner import TaskRunner

    monkeypatch.setattr(settings, "task_model_routing_enabled", True)
    monkeypatch.setattr(settings, "task_small_model", "ollama_chat/qwen2.5:7b")
    monkeypatch.setattr(settings, "task_large_model", "ollama_chat/qwen2.5:14b")
    monkeypatch.setattr(settings, "task_force_large_for_final_synthesis", True)
    monkeypatch.setattr(settings, "task_large_retry_enabled", True)
    monkeypatch.setattr(settings, "task_large_retry_max_attempts", 1)

    headers = await _headers(client, "routingowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Two step route",
            "instruction": "Run two steps",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    fake_steps = [
        {"title": "Collect", "instruction": "Collect details", "requires_approval": False},
        {"title": "Final synthesis", "instruction": "Produce final output", "requires_approval": False},
    ]
    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)):
        await client.post(
            f"/tasks/{task_id}/plan",
            json={"goal": "Route test", "max_steps": 4},
            headers=headers,
        )

    calls = []

    async def _fake_run_agent(messages, user_context, mode="chat", model_override=None, stage=None):
        calls.append((model_override, stage))
        if stage == "task_execution_step":
            return "INTERMEDIATE"
        return "FINAL OUTPUT"

    runner = TaskRunner()
    with patch("app.agent.core.run_agent", new=AsyncMock(side_effect=_fake_run_agent)):
        with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
            await runner.execute(type("TaskRef", (), {"id": task_id})())

    assert len(calls) >= 2
    assert calls[0][0] == "ollama_chat/qwen2.5:7b"
    assert calls[0][1] == "task_execution_step"
    assert calls[-1][0] == "ollama_chat/qwen2.5:14b"
    assert calls[-1][1] == "task_final_synthesis"


@pytest.mark.asyncio
async def test_runner_retries_non_final_step_once_with_large_model(client, monkeypatch):
    from app.autonomy.runner import TaskRunner

    monkeypatch.setattr(settings, "task_model_routing_enabled", True)
    monkeypatch.setattr(settings, "task_small_model", "ollama_chat/qwen2.5:7b")
    monkeypatch.setattr(settings, "task_large_model", "ollama_chat/qwen2.5:14b")
    monkeypatch.setattr(settings, "task_force_large_for_final_synthesis", True)
    monkeypatch.setattr(settings, "task_large_retry_enabled", True)
    monkeypatch.setattr(settings, "task_large_retry_max_attempts", 1)

    headers = await _headers(client, "retryrouteowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Retry route",
            "instruction": "Run with fallback",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    fake_steps = [
        {"title": "Fetch data", "instruction": "Call tools", "requires_approval": False},
        {"title": "Final synthesis", "instruction": "Summarize", "requires_approval": False},
    ]
    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)):
        await client.post(
            f"/tasks/{task_id}/plan",
            json={"goal": "Retry test", "max_steps": 4},
            headers=headers,
        )

    calls = []

    async def _fake_run_agent(messages, user_context, mode="chat", model_override=None, stage=None):
        calls.append((model_override, stage))
        if len(calls) == 1:
            raise RuntimeError("tool-call failed")
        if stage == "task_execution_step":
            return "RECOVERED STEP"
        return "FINAL RESULT"

    runner = TaskRunner()
    with patch("app.agent.core.run_agent", new=AsyncMock(side_effect=_fake_run_agent)):
        with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
            await runner.execute(type("TaskRef", (), {"id": task_id})())

    # first step: small -> fallback large; final step: large
    assert calls[0] == ("ollama_chat/qwen2.5:7b", "task_execution_step")
    assert calls[1] == ("ollama_chat/qwen2.5:14b", "task_execution_step")
    assert calls[-1] == ("ollama_chat/qwen2.5:14b", "task_final_synthesis")


@pytest.mark.asyncio
async def test_runner_suppresses_repeated_identical_tool_failures(client, monkeypatch):
    from app.autonomy.runner import TaskRunner

    monkeypatch.setattr(settings, "task_model_routing_enabled", True)
    monkeypatch.setattr(settings, "task_small_model", "ollama_chat/qwen2.5:7b")
    monkeypatch.setattr(settings, "task_large_model", "ollama_chat/qwen2.5:14b")
    monkeypatch.setattr(settings, "task_force_large_for_final_synthesis", True)
    monkeypatch.setattr(settings, "task_large_retry_enabled", True)
    monkeypatch.setattr(settings, "task_large_retry_max_attempts", 3)

    headers = await _headers(client, "suppressionowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Data gather task",
            "instruction": "Run tool workflow",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    fake_steps = [
        {"title": "Fetch data", "instruction": "Call tools", "requires_approval": False},
        {"title": "Final synthesis", "instruction": "Summarize", "requires_approval": False},
    ]
    with patch("app.autonomy.planner._generate_plan_steps", new=AsyncMock(return_value=fake_steps)):
        await client.post(
            f"/tasks/{task_id}/plan",
            json={"goal": "Suppression test", "max_steps": 4},
            headers=headers,
        )

    async def _always_fail(*_args, **_kwargs):
        raise RuntimeError("Tool search failed: timeout")

    runner = TaskRunner()
    with patch("app.agent.core.run_agent", new=AsyncMock(side_effect=_always_fail)):
        with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
            await runner.execute(type("TaskRef", (), {"id": task_id})())

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status in {"pending", "failed"}
        rows = await db.execute(
            select(TaskStep)
            .where(TaskStep.task_id == task_id, TaskStep.step_index == 1)
            .limit(1)
        )
        first_step = rows.scalar_one_or_none()
        assert first_step is not None
        assert (first_step.error or "").startswith("Suppressed repeated tool failure:")


@pytest.mark.asyncio
async def test_magazine_run_persists_dataset_and_grounding_artifacts(client):
    from app.autonomy.runner import TaskRunner

    headers = await _headers(client, "magazineowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "Daily News Magazine",
            "instruction": "Build a daily news magazine from prepared sources",
            "profile": "rss_newspaper",
            "task_type": "one_shot",
            "deliver": False,
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    async with TestSessionLocal() as db:
        task_row = await db.get(Task, task_id)
        src = RSSSource(
            user_id=task_row.user_id,
            name="Magazine Feed",
            url="https://mag.example/feed.xml",
            url_canonical="https://mag.example/feed.xml",
            category="news",
            active=True,
            trust_level="manual",
            update_interval_minutes=60,
        )
        db.add(src)
        await db.flush()
        db.add(
            RSSItem(
                source_id=src.id,
                item_uid="m1",
                title="Test story",
                link="https://mag.example/story-1",
                summary="Summary",
                published_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    await client.post(
        f"/tasks/{task_id}/plan",
        json={"goal": "Magazine output", "max_steps": 2},
        headers=headers,
    )

    with patch(
        "app.autonomy.magazine_pipeline.rss_sources.refresh_active_sources_cache",
        new=AsyncMock(return_value={"sources": 1, "items": 1}),
    ):
        with patch(
            "app.agent.core.run_agent",
            new=AsyncMock(
                side_effect=[
                    "Draft created from dataset.",
                    "# Fruitcake News\\n\\n## Top Stories\\n\\n- **Headline:** Story\\n**Source:** Magazine Feed\\n**Published at:** 2026-03-19T12:00:00+00:00\\n**Summary:** Summary\\n[Read More](https://mag.example/story-1)",
                ]
            ),
        ):
            runner = TaskRunner()
            with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
                await runner.execute(type("TaskRef", (), {"id": task_id})())

    async with TestSessionLocal() as db:
        runs = await db.execute(
            select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.started_at.desc())
        )
        latest = runs.scalars().first()
        assert latest is not None
        artifacts = await db.execute(
            select(TaskRunArtifact).where(TaskRunArtifact.task_run_id == latest.id)
        )
        by_type = {a.artifact_type: a for a in artifacts.scalars().all()}
        published = await db.execute(
            select(RSSPublishedItem).where(RSSPublishedItem.task_id == task_id)
        )
        published_rows = published.scalars().all()
        assert "prepared_dataset" in by_type
        assert "final_output" in by_type
        assert "validation_report" in by_type
        assert "run_diagnostics" in by_type
        assert len(published_rows) == 1
        assert published_rows[0].url_canonical == "https://mag.example/story-1"
