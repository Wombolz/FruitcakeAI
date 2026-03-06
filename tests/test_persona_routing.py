from __future__ import annotations

from unittest.mock import patch

from app.agent.persona_router import infer_persona_for_task
from app.autonomy.execution_profile import resolve_execution_profile
from app.db.models import Task, User


def _make_user(persona: str = "family_assistant") -> User:
    user = User(
        id=1,
        username="tester",
        email="tester@example.com",
        hashed_password="x",
        role="parent",
        persona=persona,
    )
    user.library_scopes = ["family_docs"]
    user.calendar_access = ["family"]
    return user


def test_infer_persona_for_task_matches_news_keywords():
    persona, confidence, reason = infer_persona_for_task(
        "Top headlines today",
        "Find breaking news and AP headlines",
    )
    assert persona == "news_researcher"
    assert confidence > 0.0
    assert reason.startswith("matched_")


def test_resolve_execution_profile_prefers_explicit_task_persona():
    user = _make_user(persona="family_assistant")
    task = Task(
        user_id=1,
        title="Work update",
        instruction="Prepare stakeholder report",
        persona="work_assistant",
        status="pending",
    )

    fake_tools = [
        {"type": "function", "function": {"name": "search_library"}},
        {"type": "function", "function": {"name": "create_task_plan"}},
    ]
    with patch("app.autonomy.execution_profile.get_tools_for_user", return_value=fake_tools):
        profile = resolve_execution_profile(task, user)

    assert profile.persona == "work_assistant"
    assert profile.allowed_tools == ["create_task_plan", "search_library"]


def test_resolve_execution_profile_falls_back_to_family_assistant_when_missing():
    user = _make_user(persona="does_not_exist")
    task = Task(
        user_id=1,
        title="General reminder",
        instruction="Remind me to call mom",
        persona=None,
        status="pending",
    )

    fake_tools = [{"type": "function", "function": {"name": "search_library"}}]
    with patch("app.autonomy.execution_profile.get_tools_for_user", return_value=fake_tools):
        profile = resolve_execution_profile(task, user)

    assert profile.persona == "family_assistant"
    assert profile.allowed_tools == ["search_library"]
