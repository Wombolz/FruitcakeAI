from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.autonomy.profiles.maintenance import (
    MaintenanceExecutionProfile,
    _parse_maintenance_instruction,
)
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


def test_parse_maintenance_instruction_requires_tool_and_parses_args():
    parsed = _parse_maintenance_instruction(
        'tool: refresh_rss_cache\nargs: {"max_items_per_source": 20}\n\nrefresh cache'
    )
    assert parsed["tool"] == "refresh_rss_cache"
    assert parsed["args"] == {"max_items_per_source": 20}
    assert parsed["errors"] == []


def test_parse_maintenance_instruction_rejects_malformed_args():
    parsed = _parse_maintenance_instruction("tool: refresh_rss_cache\nargs: {bad json}")
    assert parsed["tool"] == "refresh_rss_cache"
    assert parsed["errors"]
    assert "Malformed maintenance args JSON" in parsed["errors"][0]


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


@pytest.mark.asyncio
async def test_maintenance_profile_plan_steps_are_deterministic():
    profile = MaintenanceExecutionProfile()
    steps = await profile.plan_steps(
        goal="Refresh cache",
        user_id=1,
        task_id=47,
        task_instruction='tool: refresh_rss_cache\nargs: {"max_items_per_source": 20}',
        max_steps=5,
        notes="",
        style="concise",
        model_override=None,
        default_planner=AsyncMock(),
    )
    assert steps == [
        {
            "title": "Execute Maintenance Action",
            "instruction": (
                "Call the declared maintenance tool exactly once with the declared args. "
                "Return the exact tool output with no extra text.\n\n"
                "Declared tool: refresh_rss_cache\n"
                'Declared args: {"max_items_per_source": 20}'
            ),
            "requires_approval": False,
        }
    ]


def test_maintenance_profile_blocks_memory_tools_and_caps_declared_tool():
    profile = MaintenanceExecutionProfile()
    blocked = profile.effective_blocked_tools(run_context={})
    assert "create_memory" in blocked
    assert "search_memory_graph" in blocked
    allowed = profile.effective_allowed_tools(
        run_context={"maintenance_config": {"tool": "refresh_rss_cache", "args": {}}}
    )
    assert allowed == {"refresh_rss_cache"}


def test_maintenance_validate_finalize_requires_exact_tool_output():
    profile = MaintenanceExecutionProfile()
    result, report = profile.validate_finalize(
        result="sources_refreshed=12 items_cached=150",
        prior_full_outputs=[],
        run_context={
            "maintenance_config": {"tool": "refresh_rss_cache", "args": {}, "errors": []},
            "last_tool_records": [
                {
                    "tool": "refresh_rss_cache",
                    "result_summary": "sources_refreshed=12 items_cached=150",
                }
            ],
        },
        is_final_step=True,
    )
    assert result == "sources_refreshed=12 items_cached=150"
    assert report is not None
    assert report["fatal"] is False
    assert report["declared_tool_called"] is True
    assert report["exact_output_match"] is True


def test_maintenance_validate_finalize_rejects_missing_tool_call():
    profile = MaintenanceExecutionProfile()
    _, report = profile.validate_finalize(
        result="sources_refreshed=12 items_cached=150",
        prior_full_outputs=[],
        run_context={
            "maintenance_config": {"tool": "refresh_rss_cache", "args": {}, "errors": []},
            "last_tool_records": [],
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["fatal"] is True
    assert "exactly once" in report["fatal_reason"]


def test_maintenance_validate_finalize_replaces_non_exact_output_with_tool_result():
    profile = MaintenanceExecutionProfile()
    result, report = profile.validate_finalize(
        result="The RSS cache has been refreshed successfully.",
        prior_full_outputs=[],
        run_context={
            "maintenance_config": {"tool": "refresh_rss_cache", "args": {}, "errors": []},
            "last_tool_records": [
                {
                    "tool": "refresh_rss_cache",
                    "result_summary": "RSS_REFRESH_OK\nsources_refreshed: 12\nitems_seen: 150\ntimestamp_utc: 2026-03-25T03:15:03+00:00",
                }
            ],
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["fatal"] is False
    assert report["exact_output_match"] is False
    assert report["output_replaced_with_tool_result"] is True
    assert result == (
        "RSS_REFRESH_OK\nsources_refreshed: 12\nitems_seen: 150\n"
        "timestamp_utc: 2026-03-25T03:15:03+00:00"
    )


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
    assert report["memory_candidate_emitted"] is False


def test_topic_watcher_blocks_runtime_source_and_memory_management_tools():
    profile = TopicWatcherExecutionProfile()
    blocked = profile.effective_blocked_tools(run_context={})
    assert "list_rss_sources" in blocked
    assert "add_rss_source" in blocked
    assert "search_memory_graph" in blocked
    assert "open_memory_graph_nodes" in blocked
    assert "create_memory_entities" in blocked
    assert "create_memory_relations" in blocked
    assert "add_memory_observations" in blocked
    assert "search_my_feeds" in blocked


def test_topic_watcher_validate_finalize_skips_memory_candidate_for_weak_update():
    profile = TopicWatcherExecutionProfile()
    result, report = profile.validate_finalize(
        result="**Iran - New developments**\n\n- **Minor commentary** — BBC — [Read More](https://example.com/1)\nRoutine analysis only.",
        prior_full_outputs=[],
        run_context={
            "watcher_config": {"topic": "Iran", "threshold": "medium"},
            "dataset": {
                "rss_items": [
                    {
                        "article_id": 1,
                        "url": "https://example.com/1",
                        "title": "Minor commentary",
                        "source": "BBC",
                        "summary": "Routine analysis only.",
                        "published_at": "2026-03-25T01:00:00+00:00",
                        "previously_published": False,
                    }
                ]
            },
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["fired"] is True
    assert report["memory_candidate_emitted"] is False
    assert "## Memory candidate" not in result


def test_topic_watcher_validate_finalize_appends_memory_candidate_for_strong_update():
    profile = TopicWatcherExecutionProfile()
    result, report = profile.validate_finalize(
        result=(
            "**Iran - New developments**\n\n"
            "- **US-Iran talks resume after sanctions warning** — Reuters — [Read More](https://example.com/1)\n"
            "Renewed negotiations and sanctions pressure mark a notable diplomatic shift.\n\n"
            "- **Regional officials warn of missile strike risk** — BBC — [Read More](https://example.com/2)\n"
            "Military warnings suggest escalating regional pressure around Iran."
        ),
        prior_full_outputs=[],
        run_context={
            "watcher_config": {"topic": "Iran", "threshold": "medium"},
            "dataset": {
                "rss_items": [
                    {
                        "article_id": 1,
                        "url": "https://example.com/1",
                        "title": "US-Iran talks resume after sanctions warning",
                        "source": "Reuters",
                        "summary": "Renewed negotiations and sanctions pressure mark a notable diplomatic shift.",
                        "published_at": "2026-03-25T01:00:00+00:00",
                        "previously_published": False,
                    },
                    {
                        "article_id": 2,
                        "url": "https://example.com/2",
                        "title": "Regional officials warn of missile strike risk",
                        "source": "BBC",
                        "summary": "Military warnings suggest escalating regional pressure around Iran.",
                        "published_at": "2026-03-25T01:10:00+00:00",
                        "previously_published": False,
                    },
                ]
            },
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["memory_candidate_emitted"] is True
    assert report["memory_candidate_type"] in {"episodic", "semantic"}
    assert report["memory_candidate_support_count"] == 2
    assert "## Memory candidate" in result
    assert "On 2026-03-25" in result


def test_topic_watcher_artifact_payloads_include_memory_candidates():
    profile = TopicWatcherExecutionProfile()
    artifacts = profile.artifact_payloads(
        final_markdown="**Iran - New developments**\n\n## Memory candidate\n- On 2026-03-25, reports about Iran indicated diplomatic talks.",
        run_debug={
            "dataset": {"topic": "Iran"},
            "watcher_config": {"topic": "Iran", "threshold": "medium"},
            "config_warnings": [],
            "dataset_stats": {},
            "refresh_stats": {},
            "grounding_report": {
                "fired": True,
                "memory_candidate": {
                    "memory_type": "episodic",
                    "content": "On 2026-03-25, reports about Iran indicated diplomatic talks.",
                    "topic": "Iran",
                    "supporting_urls": ["https://example.com/1"],
                    "source_names": ["Reuters"],
                    "reason": "Strong medium-threshold watcher hit.",
                    "confidence": 0.8,
                },
            },
        },
    )
    artifact_types = [artifact["artifact_type"] for artifact in artifacts]
    assert "memory_candidates" in artifact_types


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
async def test_topic_watcher_prepare_run_context_includes_source_inventory():
    profile = TopicWatcherExecutionProfile()
    async with TestSessionLocal() as db:
        task = Task(
            user_id=1,
            title="Iran Watch",
            instruction="topic: Iran\nthreshold: high\nsources: BBC, world",
            profile="topic_watcher",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        with patch(
            "app.autonomy.profiles.topic_watcher.rss_sources.list_effective_sources",
            new=AsyncMock(
                return_value=[
                    {
                        "id": 11,
                        "name": "BBC News",
                        "url": "https://feeds.bbci.co.uk/news/rss.xml",
                        "category": "world",
                        "scope": "user",
                        "active": True,
                    },
                    {
                        "id": 12,
                        "name": "TechCrunch",
                        "url": "https://techcrunch.com/feed/",
                        "category": "tech",
                        "scope": "user",
                        "active": True,
                    },
                ]
            ),
        ):
            with patch(
                "app.autonomy.profiles.topic_watcher.build_magazine_dataset",
                new=AsyncMock(return_value={"items": [], "stats": {}, "refresh": {}}),
            ):
                out = await profile.prepare_run_context(
                    db=db,
                    user_id=1,
                    task_id=task.id,
                    task_run_id=101,
                )

    inventory = out["dataset"]["source_inventory"]
    assert len(inventory["active_sources"]) == 2
    assert len(inventory["matching_active_sources"]) == 1
    assert inventory["matching_active_sources"][0]["name"] == "BBC News"
    assert "Matching active sources:" in out["dataset_prompt"]
    assert "BBC News" in out["dataset_prompt"]


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
