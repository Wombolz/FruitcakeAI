from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.autonomy.configured_executor import (
    ConfiguredDailyResearchBriefingExecution,
    _apply_light_title_cluster_diversity,
    build_preserved_runtime_state,
    infer_configured_executor,
    _prune_recently_reported_items,
)
from app.db.models import Task, TaskStep
from app.config import settings
from tests.conftest import TestSessionLocal


async def _headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _sample_executor_config() -> dict:
    inferred = infer_configured_executor(
        title="Daily Iran & Middle East Developments Briefing",
        instruction=(
            "Analyze the news about Iran and the Middle East from the past 24 hours and append the results "
            "to reports/iran_middle_east_developments.md in the workspace file."
        ),
        task_type="recurring",
        requested_profile=None,
    )
    assert inferred.executor_config
    return inferred.executor_config


def test_infer_configured_executor_for_daily_research_briefing():
    inferred = infer_configured_executor(
        title="Daily Iran & Middle East Developments Briefing",
        instruction=(
            "Analyze the news about Iran and the Middle East from the past 24 hours and append the results "
            "to reports/iran_middle_east_developments.md in the workspace file."
        ),
        task_type="recurring",
        requested_profile=None,
    )

    assert inferred.profile is None
    assert inferred.executor_config["tool_policy"] == "dataset_plus_workspace_append"
    assert inferred.executor_config["input"]["topic"] == "Iran and the Middle East"
    assert inferred.executor_config["input"]["window_hours"] == 24
    assert inferred.executor_config["persistence"]["path"] == "reports/iran_middle_east_developments.md"


def test_infer_configured_executor_for_daily_research_briefing_with_spaced_path_and_previous_window():
    inferred = infer_configured_executor(
        title="Daily NASA & Artemis II 24-Hour Summary",
        instruction=(
            "Each day at 08:00 America/New_York, gather all cached/news items from the user's curated RSS/cache "
            "for the previous 24 hours that match NASA and Artemis II. "
            "Produce the daily summary in markdown format to /workspace/NASA Artemis Mission/daily-summary-YYYY-MM-DD.md."
        ),
        task_type="recurring",
        requested_profile=None,
    )

    assert inferred.profile is None
    assert inferred.executor_config["kind"] == "configured_executor"
    assert inferred.executor_config["input"]["topic"] == "NASA & Artemis II"
    assert inferred.executor_config["input"]["window_hours"] == 24
    assert inferred.executor_config["persistence"]["path"] == "workspace/NASA Artemis Mission/daily-summary-YYYY-MM-DD.md"


def test_infer_configured_executor_for_daily_analysis_with_mention_language():
    inferred = infer_configured_executor(
        title="Daily Trump 24-Hour Analysis",
        instruction=(
            "Each day at 08:00 America/New_York, gather all cached/news items from the user's curated RSS feeds "
            "covering the previous 24 hours that mention \"Trump\" or directly relate to Donald Trump. "
            "Append the analysis to workspace/Politics/Trump/Trump_summary.md."
        ),
        task_type="recurring",
        requested_profile=None,
    )

    assert inferred.profile is None
    assert inferred.executor_config["kind"] == "configured_executor"
    assert inferred.executor_config["input"]["topic"] == "Trump"
    assert inferred.executor_config["persistence"]["path"] == "workspace/Politics/Trump/Trump_summary.md"


def test_infer_configured_executor_for_briefing_title_with_daily_suffix():
    inferred = infer_configured_executor(
        title="US Politics Daily Briefing (cached RSS, last 24h)",
        instruction=(
            "Generate a brief, source-grounded US politics roundup using ONLY cached items from my curated RSS catalog. "
            "Append the result to the workspace file at workspace/politics/US Politics.md."
        ),
        task_type="recurring",
        requested_profile=None,
    )

    assert inferred.executor_config["kind"] == "configured_executor"
    assert inferred.executor_config["input"]["topic"] == "US Politics"
    assert inferred.executor_config["persistence"]["path"] == "workspace/politics/US Politics.md"


@pytest.mark.asyncio
async def test_create_task_auto_selects_configured_executor(client):
    headers = await _headers(client, "configuredexecuser")

    created = await client.post(
        "/tasks",
        json={
            "title": "Daily Iran & Middle East Developments Briefing",
            "instruction": (
                "Analyze the news about Iran and the Middle East from the past 24 hours and append the results "
                "to reports/iran_middle_east_developments.md in the workspace file."
            ),
            "task_type": "recurring",
            "schedule": "0 08 * * *",
            "active_hours_tz": "America/New_York",
        },
        headers=headers,
    )

    assert created.status_code == 201
    async with TestSessionLocal() as db:
        rows = await db.execute(select(Task).where(Task.id == created.json()["id"]))
        task = rows.scalar_one()
        assert task.profile is None
        assert task.executor_config["kind"] == "configured_executor"
        assert task.executor_config["tool_policy"] == "dataset_plus_workspace_append"
        assert task.executor_config["output_mode"] == "daily_research_briefing"


def test_legacy_executor_config_without_tool_policy_still_normalizes():
    config = _sample_executor_config()
    config.pop("tool_policy", None)
    profile = ConfiguredDailyResearchBriefingExecution(config)

    blocked = profile.effective_blocked_tools(run_context={"executor_config": config})

    assert "append_file" in blocked
    assert "web_search" in blocked


@pytest.mark.asyncio
async def test_configured_executor_plan_is_deterministic(client):
    headers = await _headers(client, "configuredplanuser")
    created = await client.post(
        "/tasks",
        json={
            "title": "Daily Iran & Middle East Developments Briefing",
            "instruction": (
                "Analyze the news about Iran and the Middle East from the past 24 hours and append the results "
                "to reports/iran_middle_east_developments.md in the workspace file."
            ),
            "task_type": "recurring",
            "schedule": "0 08 * * *",
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    planned = await client.post(
        f"/tasks/{task_id}/plan",
        json={"goal": "Produce a daily research briefing", "max_steps": 8},
        headers=headers,
    )
    assert planned.status_code == 200

    async with TestSessionLocal() as db:
        rows = await db.execute(
            select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.step_index)
        )
        steps = rows.scalars().all()
        assert [step.title for step in steps] == [
            "Prepare Topic Dataset",
            "Draft Grounded Briefing",
        ]


def test_configured_executor_validate_finalize_accepts_grounded_briefing():
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)
    result, report = profile.validate_finalize(
        result=(
            "- Missile and sanctions developments intensified regional pressure.\n\n"
            "Implications:\n"
            "- Diplomatic channels may narrow if sanctions tighten further.\n\n"
            "Key indicators to watch:\n"
            "- Follow-up sanctions announcements and shipping disruptions.\n\n"
            "Links (from cached feeds):\n"
            "- Reuters: https://example.com/reuters-story"
        ),
        prior_full_outputs=[],
        run_context={
            "executor_config": config,
            "dataset": {
                "rss_items": [
                    {"url": "https://example.com/reuters-story"},
                ]
            },
        },
        is_final_step=True,
    )

    assert report is not None
    assert report["fatal"] is False
    assert report["selected_count"] == 1
    assert result.startswith(datetime.now(timezone.utc).strftime("%Y%m%dT"))


def test_configured_executor_validate_finalize_allows_heading_before_bullets():
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)
    result, report = profile.validate_finalize(
        result=(
            "**Iran & Middle East Daily Briefing**\n\n"
            "- Missile and sanctions developments intensified regional pressure.\n\n"
            "Implications:\n"
            "- Diplomatic channels may narrow if sanctions tighten further.\n\n"
            "Key indicators to watch:\n"
            "- Follow-up sanctions announcements and shipping disruptions.\n\n"
            "Links (from cached feeds):\n"
            "- Reuters: https://example.com/reuters-story"
        ),
        prior_full_outputs=[],
        run_context={
            "executor_config": config,
            "dataset": {
                "rss_items": [
                    {"url": "https://example.com/reuters-story"},
                ]
            },
        },
        is_final_step=True,
    )

    assert report is not None
    assert report["fatal"] is False
    assert report["selected_count"] == 1
    assert "Missile and sanctions developments" in result


def test_configured_executor_strips_memory_candidate_section_from_report_output():
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)
    result, report = profile.validate_finalize(
        result=(
            "**[Iran & Middle East] - New developments**\n\n"
            "- Missile and sanctions developments intensified regional pressure.\n\n"
            "Implications:\n"
            "- Diplomatic channels may narrow if sanctions tighten further.\n\n"
            "Key indicators to watch:\n"
            "- Follow-up sanctions announcements and shipping disruptions.\n\n"
            "Links (from cached feeds):\n"
            "- Reuters: https://example.com/reuters-story\n\n"
            "## Memory candidate\n"
            "2026-03-31: Escalation in conflict and energy pressure."
        ),
        prior_full_outputs=[],
        run_context={
            "executor_config": config,
            "dataset": {
                "rss_items": [
                    {"url": "https://example.com/reuters-story"},
                ]
            },
        },
        is_final_step=True,
    )

    assert report is not None
    assert report["fatal"] is False
    assert "## Memory candidate" not in result
    assert "Escalation in conflict and energy pressure." not in result


def test_configured_executor_validate_finalize_allows_no_update_when_dataset_empty():
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)
    result, report = profile.validate_finalize(
        result="NO_SIGNIFICANT_UPDATES",
        prior_full_outputs=[],
        run_context={"executor_config": config, "dataset": {"rss_items": []}},
        is_final_step=True,
    )

    assert report is not None
    assert report["fatal"] is False
    assert report["no_update"] is True
    assert "No significant developments" in result


def test_build_preserved_runtime_state_is_compact_and_structured():
    config = _sample_executor_config()
    state = build_preserved_runtime_state(
        executor_config=config,
        step_index=2,
        step_title="Draft Grounded Briefing",
        step_instruction="Draft the final briefing.",
        is_final_step=True,
        dataset={
            "rss_items": [
                {
                    "title": "Story A",
                    "source": "Reuters",
                    "url": "https://example.com/reuters-story",
                    "summary": "Long summary that should not be copied whole.",
                }
            ]
        },
        prior_step_summaries=[
            "Step 1: **[Iran & Middle East] - New developments**\n\n- **Story A** — Reuters — [Read More](https://example.com/reuters-story)"
        ],
        active_skill_slugs=["iran-watch"],
        skill_injection_details=[
            {"slug": "iran-watch", "included": True, "reason": "Topic-specific monitoring guidance."}
        ],
    )

    assert state["runtime_contract"]["tool_policy"] == "dataset_plus_workspace_append"
    assert state["current_step"]["is_final_step"] is True
    assert state["input_summary"]["selected_item_count"] == 1
    assert "summary" not in state["input_summary"]["selected_items"][0]
    assert state["persistence_target"]["path"] == "reports/iran_middle_east_developments.md"
    assert state["active_skills_summary"][0]["slug"] == "iran-watch"
    assert state["prior_step_summaries"] == ["Step 1: [Iran & Middle East] - New developments"]


def test_prune_recently_reported_items_keeps_one_overlap_and_new_items():
    items = [
        {"title": "Repeat A", "url": "https://example.com/a"},
        {"title": "Repeat B", "url": "https://example.com/b"},
        {"title": "Repeat C", "url": "https://example.com/c"},
        {"title": "New D", "url": "https://example.com/d"},
    ]
    latest_entry = (
        "20260401T013334Z\n"
        "- Prior item\n\n"
        "Links (from cached feeds):\n"
        "- https://example.com/a\n"
        "- https://example.com/b\n"
        "- https://example.com/c\n"
    )

    pruned, stats = _prune_recently_reported_items(
        items,
        latest_entry=latest_entry,
        now=datetime(2026, 4, 1, 1, 59, 59, tzinfo=timezone.utc),
    )

    assert [item["url"] for item in pruned] == [
        "https://example.com/a",
        "https://example.com/d",
    ]
    assert stats["recent_entry_considered"] is True
    assert stats["recent_overlap_count"] == 3
    assert stats["recent_repeat_pruned_count"] == 2


def test_apply_light_title_cluster_diversity_prunes_near_identical_titles():
    items = [
        {
            "title": "Trump says the US could end the Iran war in two to three weeks",
            "url": "https://example.com/a",
        },
        {
            "title": "Trump says U.S. could end war in Iran in two to three weeks",
            "url": "https://example.com/b",
        },
        {
            "title": "Dollar stays stable after Trump says Iran war could finish soon",
            "url": "https://example.com/c",
        },
    ]

    kept, stats = _apply_light_title_cluster_diversity(items)

    assert [item["url"] for item in kept] == [
        "https://example.com/a",
        "https://example.com/c",
    ]
    assert stats["title_cluster_count"] == 1
    assert stats["title_cluster_pruned_count"] == 1


@pytest.mark.asyncio
async def test_prepare_run_context_applies_recent_repeat_pruning_and_light_title_diversity(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)

    report_path = tmp_path / "1" / "reports"
    report_path.mkdir(parents=True, exist_ok=True)
    (report_path / "iran_middle_east_developments.md").write_text(
        "20260401T030729Z\n"
        "**[Iran & Middle East] - New developments**\n\n"
        "Links (from cached feeds):\n"
        "- https://example.com/repeat-a\n"
        "- https://example.com/repeat-b\n",
        encoding="utf-8",
    )

    async with TestSessionLocal() as db:
        task = Task(
            user_id=1,
            title="Daily Iran & Middle East Developments Briefing",
            instruction=(
                "Analyze the news about Iran and the Middle East from the past 24 hours and append the results "
                "to reports/iran_middle_east_developments.md in the workspace file."
            ),
            status="pending",
        )
        task.executor_config = config
        db.add(task)
        await db.flush()

        dataset_items = [
            {
                "title": "Trump says the US could end the Iran war in two to three weeks",
                "source": "Reuters",
                "source_category": "news",
                "url": "https://example.com/repeat-a",
                "summary": "A repeated line from the most recent entry.",
                "score": 1.5,
            },
            {
                "title": "Trump says U.S. could end war in Iran in two to three weeks",
                "source": "Reuters",
                "source_category": "news",
                "url": "https://example.com/repeat-b",
                "summary": "A near-identical desk variant.",
                "score": 1.49,
            },
            {
                "title": "Asia's factory activity slows on cost pressure from Iran war",
                "source": "Reuters",
                "source_category": "news",
                "url": "https://example.com/new-c",
                "summary": "New economic spillover reporting.",
                "score": 1.48,
            },
            {
                "title": "Oil nears highest price since start of Iran war",
                "source": "BBC",
                "source_category": "news",
                "url": "https://example.com/new-d",
                "summary": "Energy markets remain under pressure.",
                "score": 1.47,
            },
        ]

        async def fake_build_magazine_dataset(*args, **kwargs):
            return {"items": dataset_items, "refresh": {"enabled": True}}

        with patch("app.autonomy.configured_executor.build_magazine_dataset", new=fake_build_magazine_dataset):
            out = await profile.prepare_run_context(
                db=db,
                user_id=1,
                task_id=task.id,
                task_run_id=None,
            )

    urls = [item["url"] for item in out["dataset"]["rss_items"]]
    assert urls == [
        "https://example.com/repeat-a",
        "https://example.com/new-c",
        "https://example.com/new-d",
    ]
    assert out["dataset_stats"]["recent_entry_considered"] is True
    assert out["dataset_stats"]["recent_overlap_count"] == 2
    assert out["dataset_stats"]["recent_repeat_pruned_count"] == 1
    assert out["dataset_stats"]["title_cluster_pruned_count"] == 0


@pytest.mark.asyncio
async def test_configured_executor_persistence_writes_preamble_once(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)
    task = SimpleNamespace(user_id=7, executor_config=config)

    await profile.persist_run_records(
        db=None,
        task=task,
        run=None,
        final_markdown="20260331T120000Z\n- First item\n\nImplications:\n- One\n\nKey indicators to watch:\n- Two\n\nLinks (from cached feeds):\n- none",
        run_debug={},
    )
    await profile.persist_run_records(
        db=None,
        task=task,
        run=None,
        final_markdown="20260331T130000Z\n- Second item\n\nImplications:\n- Three\n\nKey indicators to watch:\n- Four\n\nLinks (from cached feeds):\n- none",
        run_debug={},
    )

    report_path = Path(tmp_path) / "7" / "reports" / "iran_middle_east_developments.md"
    text = report_path.read_text(encoding="utf-8")
    assert text.count("# Iran and the Middle East Developments") == 1
    assert "20260331T120000Z" in text
    assert "20260331T130000Z" in text


@pytest.mark.asyncio
async def test_configured_executor_persistence_suppresses_near_identical_recent_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)
    task = SimpleNamespace(user_id=7, executor_config=config)

    first_entry = (
        "20260331T120000Z\n"
        "**Iran & Middle East - New developments**\n\n"
        "- **Story A** — Reuters — [Read More](https://example.com/reuters-story)\n"
        "  Markets remain sensitive.\n\n"
        "Implications:\n"
        "- Energy volatility remains elevated.\n\n"
        "Key indicators to watch:\n"
        "- Oil price movements.\n\n"
        "Links (from cached feeds):\n"
        "- https://example.com/reuters-story\n"
    )
    second_entry = (
        "20260331T130000Z\n"
        "**Iran & Middle East - New developments**\n\n"
        "- **Story A** — Reuters — [Read More](https://example.com/reuters-story)\n"
        "  Markets remain sensitive.\n\n"
        "Implications:\n"
        "- Energy volatility remains elevated.\n\n"
        "Key indicators to watch:\n"
        "- Oil price movements.\n\n"
        "Links (from cached feeds):\n"
        "- https://example.com/reuters-story\n"
    )

    first_debug: dict = {}
    await profile.persist_run_records(
        db=None,
        task=task,
        run=None,
        final_markdown=first_entry,
        run_debug=first_debug,
    )
    second_debug: dict = {}
    await profile.persist_run_records(
        db=None,
        task=task,
        run=None,
        final_markdown=second_entry,
        run_debug=second_debug,
    )

    report_path = Path(tmp_path) / "7" / "reports" / "iran_middle_east_developments.md"
    text = report_path.read_text(encoding="utf-8")
    assert text.count("20260331T120000Z") == 1
    assert "20260331T130000Z" not in text
    assert second_debug["append_result"]["suppressed_duplicate"] is True
    assert "Suppressed duplicate" in second_debug["append_result"]["result"]


def test_configured_executor_artifacts_include_preserved_runtime_state():
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)

    payloads = profile.artifact_payloads(
        final_markdown="20260331T120000Z\n- First item",
        run_debug={
            "runtime_contract": {"kind": "configured_executor"},
            "preserved_runtime_state": {
                "runtime_contract": {"kind": "configured_executor"},
                "current_step": {"step_index": 2},
                "input_summary": {"selected_item_count": 1},
                "persistence_target": {"path": "reports/iran_middle_east_developments.md"},
                "active_skills_summary": [],
                "prior_step_summaries": ["Step 1: Gathered relevant feed items."],
            },
        },
    )

    by_type = {payload["artifact_type"]: payload for payload in payloads}
    assert "runtime_contract" in by_type
    assert "preserved_runtime_state" in by_type
    assert by_type["preserved_runtime_state"]["content_json"]["input_summary"]["selected_item_count"] == 1


def test_augment_prompt_includes_preserved_runtime_block_for_final_step():
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)
    prompt_parts: list[str] = []

    profile.augment_prompt(
        prompt_parts=prompt_parts,
        run_context={
            "dataset_prompt": "Prepared dataset body",
            "preserved_runtime_state": {
                "runtime_contract": {
                    "input_mode": "prepared_rss_topic_dataset",
                    "tool_policy": "dataset_plus_workspace_append",
                    "output_mode": "daily_research_briefing",
                    "persistence_mode": "append_workspace_file",
                    "validation_mode": "grounded_briefing",
                },
                "current_step": {
                    "step_index": 2,
                    "title": "Draft Grounded Briefing",
                    "is_final_step": True,
                },
                "input_summary": {
                    "topic": "Iran and the Middle East",
                    "window_hours": 24,
                    "selected_item_count": 1,
                    "selected_items": [
                        {
                            "title": "Story A",
                            "source": "Reuters",
                            "url": "https://example.com/reuters-story",
                        }
                    ],
                },
                "persistence_target": {
                    "path": "reports/iran_middle_east_developments.md",
                    "write_preamble_if_missing": True,
                },
                "active_skills_summary": [{"slug": "iran-watch", "reason": "Topic-specific monitoring guidance."}],
                "prior_step_summaries": ["Step 1: Gathered relevant feed items."],
            },
        },
        is_final_step=True,
    )

    joined = "\n\n".join(prompt_parts)
    assert "Preserved runtime contract for this configured executor run:" in joined
    assert "topic='Iran and the Middle East'" in joined
    assert "path='reports/iran_middle_east_developments.md'" in joined
    assert "Step 1: Gathered relevant feed items." in joined


def test_augment_prompt_uses_dataset_review_contract_for_non_final_step():
    config = _sample_executor_config()
    profile = ConfiguredDailyResearchBriefingExecution(config)
    prompt_parts: list[str] = []

    profile.augment_prompt(
        prompt_parts=prompt_parts,
        run_context={
            "dataset_prompt": "Prepared dataset body",
            "preserved_runtime_state": {
                "runtime_contract": {
                    "input_mode": "prepared_rss_topic_dataset",
                    "tool_policy": "dataset_plus_workspace_append",
                    "output_mode": "daily_research_briefing",
                    "persistence_mode": "append_workspace_file",
                    "validation_mode": "grounded_briefing",
                },
                "current_step": {
                    "step_index": 1,
                    "title": "Prepare Topic Dataset",
                    "is_final_step": False,
                },
                "input_summary": {
                    "topic": "Iran and the Middle East",
                    "window_hours": 24,
                    "selected_item_count": 2,
                    "selected_items": [],
                },
                "persistence_target": {
                    "path": "reports/iran_middle_east_developments.md",
                    "write_preamble_if_missing": True,
                },
                "active_skills_summary": [],
                "prior_step_summaries": [],
            },
        },
        is_final_step=False,
    )

    joined = "\n\n".join(prompt_parts)
    assert "Prepared dataset review contract:" in joined
    assert "Do not include sections named Implications, Key indicators to watch, or Links (from cached feeds)." in joined
    assert "Daily research briefing contract:" not in joined
