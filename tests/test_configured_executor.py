from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.autonomy.configured_executor import (
    ConfiguredDailyResearchBriefingExecution,
    infer_configured_executor,
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
