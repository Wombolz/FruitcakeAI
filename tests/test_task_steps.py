"""
FruitcakeAI v5 — Task step planning endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models import ChatSession, RSSItem, RSSSource, Task, TaskRun, TaskRunArtifact, TaskStep
from app.autonomy.profiles.news_magazine import _ground_output
from app.autonomy.runner import _format_result_for_inbox
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
async def test_magazine_plan_uses_deterministic_steps_without_approval(client):
    headers = await _headers(client, "magplanowner")
    task = await client.post(
        "/tasks",
        json={
            "title": "Daily News Magazine",
            "instruction": "Create a daily magazine from prepared data",
            "profile": "news_magazine",
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
async def test_create_and_patch_task_profile_validation(client):
    headers = await _headers(client, "profileowner")

    created = await client.post(
        "/tasks",
        json={
            "title": "Magazine",
            "instruction": "Build periodic magazine",
            "profile": "news_magazine",
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["profile"] == "news_magazine"

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
            "profile": "news_magazine",
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
                    "## Daily News Magazine\\n\\n- [Story](https://mag.example/story-1)",
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
        assert "prepared_dataset" in by_type
        assert "final_output" in by_type
        assert "validation_report" in by_type
        assert "run_diagnostics" in by_type
