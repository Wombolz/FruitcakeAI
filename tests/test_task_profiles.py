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
from app.autonomy.profiles.iss_pass_watcher import ISSPassWatcherExecutionProfile
from app.autonomy.profiles.weather_conditions import WeatherConditionsExecutionProfile
from app.autonomy.profiles.topic_watcher import (
    TopicWatcherExecutionProfile,
    _parse_topic_watcher_instruction,
)
from app.autonomy.runner import TaskRunner
from app.db.models import Memory, MemoryProposal, RSSPublishedItem, Task
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


def test_parse_topic_watcher_instruction_infers_topic_from_legacy_watcher_prompt():
    parsed = _parse_topic_watcher_instruction(
        'Every run: 1) Search my curated RSS feeds for news about enterprise AI agent platforms similar to OpenClaw using these case-insensitive keywords and hashtags in titles/summaries/content: ["Alibaba AI agent platform","Qwen agent"]; 2) Dedupe items already logged in the last 30 days.'
    )
    assert parsed["topic"] == "enterprise AI agent platforms similar to OpenClaw"
    assert "Inferred topic from legacy watcher instruction." in parsed["warnings"]


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


@pytest.mark.asyncio
async def test_topic_watcher_plan_steps_are_deterministic_and_two_stage():
    profile = TopicWatcherExecutionProfile()
    steps = await profile.plan_steps(
        goal="Watch OpenClaw",
        user_id=1,
        task_id=47,
        task_instruction="topic: OpenClaw",
        max_steps=5,
        notes="",
        style="concise",
        model_override=None,
        default_planner=AsyncMock(),
    )
    assert [step["title"] for step in steps] == [
        "Shortlist Candidate Matches",
        "Finalize Watcher Briefing",
    ]
    assert all(step["requires_approval"] is False for step in steps)


def test_maintenance_profile_blocks_memory_tools_and_caps_declared_tool():
    profile = MaintenanceExecutionProfile()
    blocked = profile.effective_blocked_tools(run_context={})
    assert "create_memory" in blocked
    assert "search_memory_graph" in blocked
    allowed = profile.effective_allowed_tools(
        run_context={"maintenance_config": {"tool": "refresh_rss_cache", "args": {}}}
    )
    assert allowed == {"refresh_rss_cache"}


def test_iss_profile_disables_skill_injection_and_limits_tools():
    profile = ISSPassWatcherExecutionProfile()
    assert profile.allow_skill_injection(run_context={}) is False
    assert profile.effective_allowed_tools(run_context={}) == {"api_request"}
    blocked = profile.effective_blocked_tools(run_context={})
    assert "web_search" in blocked
    assert "create_memory" in blocked


@pytest.mark.asyncio
async def test_iss_profile_prepare_run_context_includes_response_fields():
    profile = ISSPassWatcherExecutionProfile()

    async with TestSessionLocal() as db:
        task = Task(
            user_id=1,
            title="ISS",
            instruction="Check ISS passes",
            profile="iss_pass_watcher",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        context = await profile.prepare_run_context(
            db=db,
            user_id=1,
            task_id=task.id,
            task_run_id=101,
        )

    contract = context["api_contract"]
    assert contract["response_fields"] == {"passes": "passes"}
    assert contract["display_timezone"] == "UTC"
    assert context["dataset_stats"]["response_fields_present"] == ["passes"]


@pytest.mark.asyncio
async def test_iss_profile_prepare_run_context_uses_task_timezone_when_instruction_omits_one():
    profile = ISSPassWatcherExecutionProfile()

    async with TestSessionLocal() as db:
        task = Task(
            user_id=1,
            title="ISS",
            instruction="Check ISS passes",
            profile="iss_pass_watcher",
            active_hours_tz="America/Chicago",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        context = await profile.prepare_run_context(
            db=db,
            user_id=1,
            task_id=task.id,
            task_run_id=101,
        )

    assert context["api_contract"]["display_timezone"] == "America/Chicago"


def test_iss_profile_validate_finalize_falls_back_to_exact_tool_result_for_chatty_failure():
    profile = ISSPassWatcherExecutionProfile()
    result, report = profile.validate_finalize(
        result=(
            "I tried to check ISS visual passes using the N2YO API, but the API request failed. "
            "Would you like me to try again now?"
        ),
        prior_full_outputs=[],
        run_context={
            "last_tool_records": [
                {
                    "tool": "api_request",
                    "result_summary": "N2YO requests require a named secret.",
                }
            ]
        },
        is_final_step=True,
    )
    assert result == "N2YO requests require a named secret."
    assert report is not None
    assert report["fatal"] is False
    assert report["used_tool_result_fallback"] is True


def test_iss_profile_validate_finalize_formats_empty_structured_result_as_no_passes():
    profile = ISSPassWatcherExecutionProfile()
    result, report = profile.validate_finalize(
        result="API request failed.",
        prior_full_outputs=[],
        run_context={
            "last_tool_records": [
                {
                    "tool": "api_request",
                    "result_summary": '{"deduped": false, "fields": {"passes": []}}',
                }
            ]
        },
        is_final_step=True,
    )
    assert result == "No visible ISS passes found in the requested window."
    assert report is not None
    assert report["structured_api_result"] is True


@pytest.mark.asyncio
async def test_weather_profile_prepare_run_context_includes_response_fields():
    profile = WeatherConditionsExecutionProfile()

    async with TestSessionLocal() as db:
        task = Task(
            user_id=1,
            title="Weather",
            instruction="Check current conditions at lat=32.4485 lon=-81.7832 timezone=America/New_York",
            profile="weather_conditions",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        context = await profile.prepare_run_context(
            db=db,
            user_id=1,
            task_id=task.id,
            task_run_id=101,
        )

    contract = context["api_contract"]
    assert contract["service"] == "weather"
    assert contract["endpoint"] == "current_conditions"
    assert contract["response_fields"] == {"location": "location", "current_weather": "current_weather"}
    assert context["dataset_stats"]["response_fields_present"] == ["current_weather", "location"]


def test_weather_profile_validate_finalize_formats_structured_current_conditions():
    profile = WeatherConditionsExecutionProfile()
    result, report = profile.validate_finalize(
        result="API request failed.",
        prior_full_outputs=[],
        run_context={
            "api_contract": {"display_timezone": "America/New_York"},
            "last_tool_records": [
                {
                    "tool": "api_request",
                    "result_summary": (
                        '{"deduped": false, "fields": {"location": {"latitude": 32.4485, "longitude": -81.7832, '
                        '"city_name": "Statesboro", "country": "US"}, "current_weather": {"observed_at_utc": '
                        '"2026-04-01T14:00:00+00:00", "temperature_c": 22.3, "feels_like_c": 23.1, '
                        '"humidity_percent": 64, "pressure_hpa": 1014, "wind_speed_mps": 11.4, '
                        '"wind_direction_deg": 270, "weather_code": 802, "weather_main": "Clouds", '
                        '"description": "scattered clouds", "is_day": true}}}'
                    ),
                }
            ]
        },
        is_final_step=True,
    )
    assert "Weather briefing" in result
    assert "temperature_c=22.3" in result
    assert "weather_main=Clouds" in result
    assert "time_local=2026-04-01 10:00 AM EDT" in result
    assert "time_utc=2026-04-01T14:00:00+00:00" in result
    assert report is not None
    assert report["structured_api_result"] is True


@pytest.mark.asyncio
async def test_weather_profile_prepare_run_context_uses_task_timezone_when_instruction_omits_one():
    profile = WeatherConditionsExecutionProfile()

    async with TestSessionLocal() as db:
        task = Task(
            user_id=1,
            title="Weather",
            instruction="Check current conditions at lat=32.4485 lon=-81.7832",
            profile="weather_conditions",
            active_hours_tz="America/Los_Angeles",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        context = await profile.prepare_run_context(
            db=db,
            user_id=1,
            task_id=task.id,
            task_run_id=101,
        )

    assert context["api_contract"]["display_timezone"] == "America/Los_Angeles"


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


@pytest.mark.asyncio
async def test_topic_watcher_prepare_run_context_loads_same_topic_memory_history():
    profile = TopicWatcherExecutionProfile()

    async with TestSessionLocal() as db:
        task = Task(user_id=1, title="Iran Watch", instruction="topic: Iran\nthreshold: medium", profile="topic_watcher")
        db.add(task)
        await db.flush()
        recent_same_topic = Memory(
            user_id=1,
            memory_type="episodic",
            content="On 2026-03-24, reports about Iran indicated renewed diplomatic talks.",
            importance=0.65,
        )
        recent_same_topic.tags_list = ["topic_watcher", "iran"]
        recent_same_topic.created_at = datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc)
        other_topic = Memory(
            user_id=1,
            memory_type="episodic",
            content="On 2026-03-24, reports about China indicated trade friction.",
            importance=0.65,
        )
        other_topic.tags_list = ["topic_watcher", "china"]
        other_topic.created_at = datetime(2026, 3, 24, 13, 0, tzinfo=timezone.utc)
        expired_same_topic = Memory(
            user_id=1,
            memory_type="episodic",
            content="Old Iran development.",
            importance=0.65,
            expires_at=datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc),
        )
        expired_same_topic.tags_list = ["topic_watcher", "iran"]
        expired_same_topic.created_at = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
        db.add_all([recent_same_topic, other_topic, expired_same_topic])
        await db.commit()
        with patch("app.autonomy.profiles.topic_watcher.rss_sources.list_effective_sources", new=AsyncMock(return_value=[])):
            with patch(
                "app.autonomy.profiles.topic_watcher.build_magazine_dataset",
                new=AsyncMock(return_value={"items": [], "stats": {}, "refresh": {}}),
            ):
                out = await profile.prepare_run_context(
                    db=db,
                    user_id=1,
                    task_id=task.id,
                    task_run_id=1,
                )

    assert out["dataset_stats"]["topic_memory_count"] == 1
    assert out["dataset_stats"]["topic_memory_window_days"] == 30
    assert out["dataset_stats"]["topic_memory_summary_used"] is True
    history = out["dataset"]["topic_memory_history"]
    assert len(history) == 1
    assert "Iran" in out["topic_memory_timeline_summary"]
    assert "China" not in out["topic_memory_timeline_summary"]


@pytest.mark.asyncio
async def test_topic_watcher_prepare_run_context_prefilters_to_topic_matches():
    profile = TopicWatcherExecutionProfile()

    async with TestSessionLocal() as db:
        task = Task(user_id=1, title="Iran Watch", instruction="topic: Iran\nthreshold: medium", profile="topic_watcher")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        with patch("app.autonomy.profiles.topic_watcher.rss_sources.list_effective_sources", new=AsyncMock(return_value=[])):
            with patch(
                "app.autonomy.profiles.topic_watcher.build_magazine_dataset",
                new=AsyncMock(
                    return_value={
                        "items": [
                            {
                                "title": "US-Iran talks resume after sanctions warning",
                                "summary": "Renewed negotiations and sanctions pressure mark a notable diplomatic shift.",
                                "source": "Reuters",
                                "section": "World",
                                "url": "https://example.com/iran",
                                "score": 1.11,
                            },
                            {
                                "title": "Meta and Google found liable in landmark social media addiction trial",
                                "summary": "The verdict marks the end of a five-week trial on social media addiction.",
                                "source": "BBC News",
                                "section": "Tech",
                                "url": "https://example.com/meta",
                                "score": 1.19,
                            },
                        ],
                        "stats": {"selected_count": 2},
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

    titles = [item["title"] for item in out["dataset"]["rss_items"]]
    assert titles == ["US-Iran talks resume after sanctions warning"]
    assert out["dataset_stats"]["topic_match_candidate_count"] >= 1
    assert out["dataset_stats"]["topic_match_selected_count"] == 1
    assert out["dataset_stats"]["topic_match_fallback_used"] is False


def test_topic_watcher_augment_prompt_includes_topic_memory_timeline_summary():
    profile = TopicWatcherExecutionProfile()
    prompt_parts: list[str] = []

    profile.augment_prompt(
        prompt_parts=prompt_parts,
        run_context={
            "dataset_prompt": "Prepared dataset body",
            "topic_memory_timeline_summary": "- 2026-03-24: On 2026-03-24, reports about Iran indicated renewed diplomatic talks.",
        },
        is_final_step=True,
    )

    joined = "\n\n".join(prompt_parts)
    assert "Approved topic memory timeline" in joined
    assert "Do not cite memories as sources" in joined


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
            "topic_memory_history": [],
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["fired"] is True
    assert report["memory_candidate_emitted"] is False
    assert "## Memory candidates" not in result


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
            "topic_memory_history": [],
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["memory_candidate_emitted"] is True
    assert report["memory_candidate_type"] == "episodic"
    assert report["memory_candidate_support_count"] >= 1
    assert "## Memory candidates" in result
    assert "On 2026-03-25" in result
    assert len(report["memory_candidates"]) >= 1
    assert report["memory_candidates"][0]["proposal_key"]
    assert report["memory_candidates"][0]["expires_at"].startswith("2026-04-24T00:00:00")


def test_topic_watcher_validate_finalize_suppresses_duplicate_memory_candidate_from_topic_history():
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
            "topic_memory_history": [
                {
                    "id": 44,
                    "content": "On 2026-03-25, reports about Iran indicated diplomatic talks, based on coverage from Reuters, BBC.",
                    "memory_type": "episodic",
                    "created_at": "2026-03-25T02:00:00+00:00",
                }
            ],
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["fired"] is True
    assert report["memory_candidate_emitted"] is False
    assert report["topic_memory_context_considered"] is True
    assert report["topic_memory_duplicate_suppressed_count"] >= 1
    assert report["memory_candidates"] == []
    assert "## Memory candidates" not in result


def test_topic_watcher_validate_finalize_allows_distinct_evolving_narrative_memory_candidate():
    profile = TopicWatcherExecutionProfile()
    result, report = profile.validate_finalize(
        result=(
            "**Iran - New developments**\n\n"
            "- **At CPAC, Republicans close ranks behind Trump on Iran war** — Reuters — [Read More](https://example.com/1)\n"
            "Republicans largely backed Trump's Iran stance at CPAC.\n\n"
            "- **Three charts that are warning signs flashing for Trump on Iran war** — BBC — [Read More](https://example.com/2)\n"
            "Political and economic indicators point to mounting risks around the Iran conflict."
        ),
        prior_full_outputs=[],
        run_context={
            "watcher_config": {"topic": "Iran", "threshold": "medium"},
            "dataset": {
                "rss_items": [
                    {
                        "article_id": 1,
                        "url": "https://example.com/1",
                        "title": "At CPAC, Republicans close ranks behind Trump on Iran war",
                        "source": "Reuters",
                        "summary": "Republicans largely backed Trump's Iran stance at CPAC.",
                        "published_at": "2026-03-27T00:45:00+00:00",
                        "previously_published": False,
                    },
                    {
                        "article_id": 2,
                        "url": "https://example.com/2",
                        "title": "Three charts that are warning signs flashing for Trump on Iran war",
                        "source": "BBC",
                        "summary": "Political and economic indicators point to mounting risks around the Iran conflict.",
                        "published_at": "2026-03-27T00:13:09+00:00",
                        "previously_published": False,
                    },
                ]
            },
            "topic_memory_history": [
                {
                    "id": 19,
                    "content": "On 2026-03-25, reports about Iran indicated diplomatic talks, based on coverage from Reuters, BBC.",
                    "memory_type": "episodic",
                    "created_at": "2026-03-25T13:15:16+00:00",
                }
            ],
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["memory_candidate_emitted"] is True
    assert report["topic_memory_duplicate_suppressed_count"] == 0
    assert len(report["memory_candidates"]) == 1
    assert "## Memory candidates" in result
    assert "diplomatic talks" not in report["memory_candidates"][0]["content"].lower()


def test_topic_watcher_validate_finalize_prefers_cyber_or_information_theme_over_generic_diplomacy():
    profile = TopicWatcherExecutionProfile()
    result, report = profile.validate_finalize(
        result=(
            "**[Topic] - New developments**\n\n"
            "- **Gulf states tell US ending the war is not enough, Iran's capabilities must be degraded** — Reuters World News — [Read More](https://example.com/1)\n"
            "Regional governments are pressing for actions that weaken Iran's military capacity.\n\n"
            "- **Iran-linked hackers claim breach of FBI director's personal email; DOJ official confirms break-in** — Reuters World News — [Read More](https://example.com/2)\n"
            "A confirmed breach claim signals active Iranian-linked cyber operations.\n\n"
            "- **Iran Is Winning the AI Slop Propaganda War** — 404 Media — [Read More](https://example.com/3)\n"
            "Iran and proxies are using synthetic media and online propaganda effectively."
        ),
        prior_full_outputs=[],
        run_context={
            "watcher_config": {"topic": "Iran", "threshold": "medium"},
            "dataset": {
                "rss_items": [
                    {
                        "article_id": 1,
                        "url": "https://example.com/1",
                        "title": "Gulf states tell US ending the war is not enough, Iran's capabilities must be degraded",
                        "source": "Reuters World News",
                        "summary": "Regional governments are pressing for actions that weaken Iran's military capacity.",
                        "published_at": "2026-03-27T10:00:00+00:00",
                        "previously_published": False,
                    },
                    {
                        "article_id": 2,
                        "url": "https://example.com/2",
                        "title": "Iran-linked hackers claim breach of FBI director's personal email; DOJ official confirms break-in",
                        "source": "Reuters World News",
                        "summary": "A confirmed breach claim signals active Iranian-linked cyber operations.",
                        "published_at": "2026-03-27T10:15:00+00:00",
                        "previously_published": False,
                    },
                    {
                        "article_id": 3,
                        "url": "https://example.com/3",
                        "title": "Iran Is Winning the AI Slop Propaganda War",
                        "source": "404 Media",
                        "summary": "Iran and proxies are using synthetic media and online propaganda effectively.",
                        "published_at": "2026-03-27T10:20:00+00:00",
                        "previously_published": False,
                    },
                ]
            },
            "topic_memory_history": [],
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["memory_candidate_emitted"] is True
    assert "diplomatic talks" not in report["memory_candidates"][0]["content"].lower()
    assert "cyber and information operations" in report["memory_candidates"][0]["content"].lower()


def test_topic_watcher_validate_finalize_prefers_economic_pressure_theme_over_generic_diplomacy():
    profile = TopicWatcherExecutionProfile()
    result, report = profile.validate_finalize(
        result=(
            "**Iran - New developments**\n\n"
            "- **Global equity funds see biggest inflows in 2-1/2 months on Iran de-escalation hopes** — Reuters World News — [Read More](https://example.com/1)\n"
            "Markets are pricing in lower near-term Iran risk.\n\n"
            "- **Senate votes to fund most of DHS. And, Trump extends Iran's deadline to reopen strait** — NPR News — [Read More](https://example.com/2)\n"
            "A U.S. deadline for Iran to reopen the Strait of Hormuz shapes maritime and economic pressure."
        ),
        prior_full_outputs=[],
        run_context={
            "watcher_config": {"topic": "Iran", "threshold": "medium"},
            "dataset": {
                "rss_items": [
                    {
                        "article_id": 1,
                        "url": "https://example.com/1",
                        "title": "Global equity funds see biggest inflows in 2-1/2 months on Iran de-escalation hopes",
                        "source": "Reuters World News",
                        "summary": "Markets are pricing in lower near-term Iran risk.",
                        "published_at": "2026-03-27T12:00:00+00:00",
                        "previously_published": False,
                    },
                    {
                        "article_id": 2,
                        "url": "https://example.com/2",
                        "title": "Senate votes to fund most of DHS. And, Trump extends Iran's deadline to reopen strait",
                        "source": "NPR News",
                        "summary": "A U.S. deadline for Iran to reopen the Strait of Hormuz shapes maritime and economic pressure.",
                        "published_at": "2026-03-27T12:10:00+00:00",
                        "previously_published": False,
                    },
                ]
            },
            "topic_memory_history": [],
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["memory_candidate_emitted"] is True
    assert "diplomatic talks" not in report["memory_candidates"][0]["content"].lower()
    assert "sanctions and economic pressure" in report["memory_candidates"][0]["content"].lower()


def test_topic_watcher_validate_finalize_strips_model_emitted_memory_candidate_when_suppressed():
    profile = TopicWatcherExecutionProfile()
    result, report = profile.validate_finalize(
        result=(
            "**Iran - New developments**\n\n"
            "- **US-Iran talks resume after sanctions warning** — Reuters — [Read More](https://example.com/1)\n"
            "Renewed negotiations and sanctions pressure mark a notable diplomatic shift.\n\n"
            "- **Regional officials warn of missile strike risk** — BBC — [Read More](https://example.com/2)\n"
            "Military warnings suggest escalating regional pressure around Iran.\n\n"
            "## Memory candidate\n"
            "On 2026-03-25, reports about Iran indicated diplomatic talks and military pressure."
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
            "topic_memory_history": [
                {
                    "id": 44,
                    "content": "On 2026-03-25, reports about Iran indicated diplomatic talks, based on coverage from Reuters, BBC.",
                    "memory_type": "episodic",
                    "created_at": "2026-03-25T02:00:00+00:00",
                }
            ],
        },
        is_final_step=True,
    )
    assert report is not None
    assert report["memory_candidate_emitted"] is False
    assert report["memory_candidates"] == []
    assert "## Memory candidate" not in result
    assert "## Memory candidates" not in result


def test_topic_watcher_artifact_payloads_include_memory_candidates():
    profile = TopicWatcherExecutionProfile()
    artifacts = profile.artifact_payloads(
        final_markdown="**Iran - New developments**\n\n## Memory candidates\n- On 2026-03-25, reports about Iran indicated diplomatic talks.",
        run_debug={
            "dataset": {"topic": "Iran"},
            "watcher_config": {"topic": "Iran", "threshold": "medium"},
            "config_warnings": [],
            "dataset_stats": {},
            "refresh_stats": {},
            "grounding_report": {
                "fired": True,
                "memory_candidates": [
                    {
                        "proposal_key": "proposal-1",
                        "memory_type": "episodic",
                        "content": "On 2026-03-25, reports about Iran indicated diplomatic talks.",
                        "topic": "Iran",
                        "supporting_urls": ["https://example.com/1"],
                        "source_names": ["Reuters"],
                        "reason": "Strong medium-threshold watcher hit.",
                        "confidence": 0.8,
                        "expires_at": "2026-04-24T00:00:00+00:00",
                        "status": "pending",
                    }
                ],
            },
        },
    )
    artifact_types = [artifact["artifact_type"] for artifact in artifacts]
    assert "memory_candidates" in artifact_types


@pytest.mark.asyncio
async def test_topic_watcher_persist_run_records_writes_selected_items():
    profile = TopicWatcherExecutionProfile()
    async with TestSessionLocal() as db:
        task = SimpleNamespace(id=48, user_id=1)
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
                    "memory_candidates": [
                        {
                            "proposal_key": "proposal-1",
                            "memory_type": "episodic",
                            "content": "On 2026-03-25, reports about Iran indicated renewed diplomatic talks.",
                            "topic": "Iran",
                            "supporting_urls": ["https://example.com/item-7"],
                            "source_names": ["Reuters"],
                            "reason": "Strong watcher hit.",
                            "confidence": 0.8,
                            "expires_at": "2026-04-24T00:00:00+00:00",
                            "status": "pending",
                        }
                    ],
                },
            },
        )
        await db.commit()
        rows = await db.execute(select(RSSPublishedItem))
        saved = rows.scalars().all()
        proposals = (await db.execute(select(MemoryProposal))).scalars().all()
    assert len(saved) == 1
    assert saved[0].task_id == 48
    assert saved[0].url_canonical == "https://example.com/item-7"
    assert len(proposals) == 1
    assert proposals[0].source_type == "topic_watcher"
    assert proposals[0].status == "pending"
    assert proposals[0].proposal_payload["expires_at"] == "2026-04-24T00:00:00+00:00"


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
