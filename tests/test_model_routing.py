from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.autonomy.model_routing import resolve_task_model_profile
from app.autonomy.planner import _generate_plan_steps
from app.config import settings
from app.db.models import Task, User


def _fake_user() -> User:
    return User(
        id=1,
        username="u",
        email="u@example.com",
        hashed_password="x",
        role="parent",
        persona="family_assistant",
        is_active=True,
    )


def _fake_task() -> Task:
    return Task(
        id=1,
        user_id=1,
        title="T",
        instruction="I",
        status="pending",
        task_type="one_shot",
    )


def test_resolve_task_model_profile_uses_env_defaults(monkeypatch):
    monkeypatch.setattr(settings, "task_model_routing_enabled", True)
    monkeypatch.setattr(settings, "task_small_model", "ollama_chat/qwen2.5:7b")
    monkeypatch.setattr(settings, "task_large_model", "ollama_chat/qwen2.5:14b")
    monkeypatch.setattr(settings, "task_force_large_for_planning", True)
    monkeypatch.setattr(settings, "task_force_large_for_final_synthesis", True)
    monkeypatch.setattr(settings, "task_large_retry_enabled", True)
    monkeypatch.setattr(settings, "task_large_retry_max_attempts", 1)

    profile = resolve_task_model_profile(_fake_task(), _fake_user())
    assert profile.planning_model == "ollama_chat/qwen2.5:14b"
    assert profile.execution_model == "ollama_chat/qwen2.5:7b"
    assert profile.final_synthesis_model == "ollama_chat/qwen2.5:14b"
    assert profile.large_retry_enabled is True
    assert profile.large_retry_max_attempts == 1


def test_resolve_task_model_profile_prefers_task_override(monkeypatch):
    monkeypatch.setattr(settings, "task_model_routing_enabled", True)
    monkeypatch.setattr(settings, "task_small_model", "ollama_chat/qwen2.5:7b")
    monkeypatch.setattr(settings, "task_large_model", "ollama_chat/qwen2.5:14b")

    task = _fake_task()
    task.llm_model_override = "gpt-5-mini"

    profile = resolve_task_model_profile(task, _fake_user())
    assert profile.planning_model == "gpt-5-mini"
    assert profile.execution_model == "gpt-5-mini"
    assert profile.final_synthesis_model == "gpt-5-mini"
    assert profile.large_retry_enabled is False


@pytest.mark.asyncio
async def test_generate_plan_steps_prefers_large_model_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "task_model_routing_enabled", True)
    monkeypatch.setattr(settings, "task_force_large_for_planning", True)
    monkeypatch.setattr(settings, "task_large_model", "ollama_chat/qwen2.5:14b")

    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='[{"title":"Step A","instruction":"Do A","requires_approval":false}]'
                )
            )
        ]
    )

    with patch("app.autonomy.planner.litellm.acompletion", new=AsyncMock(return_value=fake_resp)) as mocked:
        rows = await _generate_plan_steps(
            goal="G",
            user_id=1,
            task_id=1,
            task_instruction="I",
            max_steps=3,
            notes="",
            style="concise",
            model_override=None,
        )

    assert rows and rows[0]["title"] == "Step A"
    assert mocked.await_args.kwargs["model"] == "ollama_chat/qwen2.5:14b"
