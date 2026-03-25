from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.autonomy.profiles.morning_briefing import MorningBriefingExecutionProfile
from app.autonomy.profiles.topic_watcher import (
    TopicWatcherExecutionProfile,
    _parse_topic_watcher_instruction,
)
from app.autonomy.runner import TaskRunner
from app.db.models import RSSPublishedItem, Task
from tests.conftest import TestSessionLocal


def test_parse_topic_watcher_instruction_defaults_threshold_and_sources():
    parsed = _parse_topic_watcher_instruction(
        "topic: AI regulation\nthreshold: noisy\nsources: Reuters, tech\n\nwatch rate decisions too"
    )
    assert parsed["topic"] == "AI regulation"
    assert parsed["threshold"] == "medium"
    assert parsed["sources"] == ["reuters", "tech"]
    assert "defaulted to medium" in parsed["warnings"][0]
    assert "watch rate decisions too" in parsed["notes"]


@pytest.mark.asyncio
async def test_morning_briefing_prepare_run_context_uses_calendar_and_rss():
    profile = MorningBriefingExecutionProfile()

    class _Provider:
        async def list_events(self, calendar_id, start, end, max_results):
            assert calendar_id is None
            assert max_results == 20
            return [
                {
                    "id": "evt1",
                    "summary": "School meeting",
                    "start": "2026-03-24T13:00:00+00:00",
                    "end": "2026-03-24T14:00:00+00:00",
                    "location": "Office",
                }
            ]

    async with TestSessionLocal() as db:
        task = Task(user_id=1, title="Morning", instruction="Brief me", profile="morning_briefing", active_hours_tz="UTC")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        with patch("app.autonomy.profiles.morning_briefing._get_provider", return_value=_Provider()):
            with patch(
                "app.autonomy.profiles.morning_briefing.build_magazine_dataset",
                new=AsyncMock(
                    return_value={
                        "items": [
                            {
                                "title": "Headline",
                                "source": "Reuters",
                                "section": "World",
                                "url": "https://example.com/story",
                            }
                        ],
                        "stats": {"selected_count": 1},
                        "refresh": {"sources_refreshed": 2},
                    }
                ),
            ):
                out = await profile.prepare_run_context(
                    db=db,
                    user_id=1,
                    task_id=task.id,
                    task_run_id=101,
                )

    assert out["dataset_stats"]["calendar_count"] == 1
    assert out["dataset_stats"]["rss_count"] == 1
    assert "School meeting" in out["dataset_prompt"]
    assert "Headline" in out["dataset_prompt"]


def test_morning_briefing_validate_finalize_accepts_calendar_only():
    profile = MorningBriefingExecutionProfile()
    result, report = profile.validate_finalize(
        result="## Today at a glance\n\n- 09:00 Doctor appointment",
        prior_full_outputs=[],
        run_context={
            "dataset": {"calendar_events": [{"title": "Doctor appointment"}], "rss_items": []}
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["fatal"] is False
    assert "Today at a glance" in result


def test_topic_watcher_validate_finalize_suppresses_push_for_nothing_new():
    profile = TopicWatcherExecutionProfile()
    result, report = profile.validate_finalize(
        result="NOTHING_NEW",
        prior_full_outputs=[],
        run_context={
            "watcher_config": {"topic": "AI regulation", "threshold": "medium"},
            "dataset": {"rss_items": []},
        },
        is_final_step=True,
    )
    assert result == "NOTHING_NEW"
    assert report is not None
    assert report["fired"] is False
    assert report["suppress_push"] is True


@pytest.mark.asyncio
async def test_topic_watcher_persist_run_records_writes_selected_items():
    profile = TopicWatcherExecutionProfile()
    async with TestSessionLocal() as db:
        task = SimpleNamespace(id=48)
        run = SimpleNamespace(id=9001, finished_at=datetime.now(timezone.utc))
        await profile.persist_run_records(
            db=db,
            task=task,
            run=run,
            final_markdown="ignored",
            run_debug={
                "watcher_config": {"threshold": "medium"},
                "grounding_report": {
                    "fired": True,
                    "reuse_fallback_triggered": False,
                    "published_items": [
                        {"rss_item_id": 7, "url_canonical": "https://example.com/item-7", "reused": False}
                    ],
                },
            },
        )
        await db.commit()
        rows = await db.execute(select(RSSPublishedItem))
        saved = rows.scalars().all()
    assert len(saved) == 1
    assert saved[0].task_id == 48
    assert saved[0].url_canonical == "https://example.com/item-7"


@pytest.mark.asyncio
async def test_topic_watcher_runner_does_not_push_nothing_new(client):
    headers = await _headers(client, "watcherowner")
    created = await client.post(
        "/tasks",
        json={
            "title": "AI watcher",
            "instruction": "topic: AI regulation\nthreshold: medium",
            "profile": "topic_watcher",
            "deliver": True,
        },
        headers=headers,
    )
    task_id = created.json()["id"]
    runner = TaskRunner()
    task_ref = type("TaskRef", (), {"id": task_id})()

    with patch("app.db.session.AsyncSessionLocal", new=TestSessionLocal):
        with patch("app.autonomy.runner._preflight_llm_dispatch", new=AsyncMock(return_value=None)):
            with patch(
                "app.autonomy.profiles.topic_watcher.build_magazine_dataset",
                new=AsyncMock(return_value={"items": [], "stats": {}, "refresh": {}}),
            ):
                with patch("app.agent.core.run_agent", new=AsyncMock(return_value="NOTHING_NEW")):
                    with patch.object(runner, "_push", new=AsyncMock()) as push_mock:
                        await runner.execute(task_ref)

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.result == "NOTHING_NEW"
    push_mock.assert_not_awaited()


async def _headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
