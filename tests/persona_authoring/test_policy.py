from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.context import UserContext
from app.agent.persona_loader import get_persona
from app.agent.persona_policy import PersonaPolicyError, resolve_persona_policy
from app.agent.persona_router import infer_persona_for_task
from app.agent.tools import _execute_tool_call, get_tools_for_user


def user(**changes):
    values = dict(id=7, username="fixture-user", role="parent", persona="family_assistant",
                  library_scopes=["family_docs", "work_docs", "projects", "kids_books", "homework"],
                  calendar_access=["family", "personal", "work"], active_hours_tz="UTC")
    return SimpleNamespace(**(values | changes))


def test_neutral_has_no_household_identity_tone_or_generic_style_guidance(monkeypatch):
    account = user()
    default = UserContext.from_user(account)
    neutral = UserContext.from_user(account, "neutral")
    for prompt in (neutral.to_stable_system_prompt(), neutral.to_system_prompt()):
        assert "family household" not in prompt
        assert "household procedure" not in prompt
        assert "Tone:" not in prompt
        assert "Be helpful" not in prompt
        assert "Do not end every response" not in prompt
        assert "Never delete a calendar event" in prompt
        assert "Only surface documents" in prompt
    monkeypatch.setattr(neutral, "_current_time_str", lambda: "FIXED")
    lines = neutral.to_system_prompt().splitlines()
    assert lines[:2] == ["Current date and time: FIXED", "Current user: fixture-user (role: parent)"]
    assert default.blocked_tools == neutral.blocked_tools
    assert default.library_scopes == neutral.library_scopes
    assert default.calendar_access == neutral.calendar_access
    registry = SimpleNamespace(_is_ready=False)
    monkeypatch.setattr("app.mcp.registry.get_mcp_registry", lambda: registry)
    assert get_tools_for_user(default) == get_tools_for_user(neutral)


@pytest.mark.parametrize("selected", ["neutral", "family_assistant", "work_assistant", "news_researcher"])
def test_restricted_role_floor_survives_default_and_selected_personalities(selected):
    # Even an account accidentally assigned the general default retains its role floor.
    account = user(role="restricted")
    ctx = UserContext.from_user(account, selected)
    assert ctx.content_filter == "strict"
    assert set(ctx.blocked_tools) >= set(get_persona("restricted_assistant")["blocked_tools"])
    assert set(ctx.library_scopes) <= {"kids_books", "homework"}
    assert set(ctx.calendar_access) <= {"family"}


def test_administrator_managed_baseline_cannot_be_replaced_by_selected_persona():
    account = user(persona="restricted_assistant")
    ctx = UserContext.from_user(account, "neutral")
    assert ctx.content_filter == "strict"
    assert ctx.library_scopes == ["kids_books", "homework"]
    assert ctx.calendar_access == ["family"]
    assert "web_search" in ctx.blocked_tools


def test_stored_account_grants_are_ceiling_not_persona_supplied_grants():
    account = user(library_scopes=["work_docs"], calendar_access=["personal"])
    ctx = UserContext.from_user(account, "roadmap_verifier")
    assert ctx.library_scopes == ["work_docs"]
    assert ctx.calendar_access == ["personal"]
    empty = UserContext.from_user(user(library_scopes=[], calendar_access=[]), "neutral")
    assert empty.library_scopes == [] and empty.calendar_access == []
    assert set(empty.blocked_tools) >= {"search_library", "summarize_document", "list_library_documents",
                                      "list_events", "search_events", "create_event", "delete_event"}


def test_selected_scopes_can_only_narrow_and_explicit_empty_is_not_inheritance(monkeypatch):
    from app.agent import persona_loader
    definitions = dict(persona_loader.list_personas())
    definitions["narrow"] = {"library_scopes": [], "calendar_access": [], "blocked_tools": ["read_file"]}
    monkeypatch.setattr(persona_loader, "_personas", definitions)
    policy = resolve_persona_policy(user(), "narrow")
    assert policy.library_scopes == () and policy.calendar_access == ()
    assert "read_file" in policy.blocked_tools
    assert resolve_persona_policy(user(), "neutral").library_scopes


def test_missing_persona_or_restricted_policy_fails_without_fallback(monkeypatch):
    with pytest.raises(PersonaPolicyError, match="Unknown persona"):
        UserContext.from_user(user(), "missing")
    with pytest.raises(PersonaPolicyError, match="Unknown persona"):
        UserContext.from_user(user(persona="missing"), "neutral")
    from app.agent import persona_loader
    definitions = dict(persona_loader.list_personas())
    definitions.pop("restricted_assistant")
    monkeypatch.setattr(persona_loader, "_personas", definitions)
    with pytest.raises(PersonaPolicyError, match="Restricted account policy"):
        UserContext.from_user(user(role="restricted"), "neutral")


@pytest.mark.parametrize("grants", [None, "all", [""], [" work_docs"], [None]])
def test_malformed_scope_policy_fails_closed(grants):
    with pytest.raises(PersonaPolicyError):
        UserContext.from_user(user(library_scopes=grants), "neutral")


@pytest.mark.asyncio
async def test_neutral_cannot_restore_blocked_calls_or_empty_scope_tools(monkeypatch):
    from app.agent import tools
    implementation = AsyncMock()
    monkeypatch.setattr(tools, "_call_tool", implementation)
    ctx = UserContext.from_user(user(role="restricted", calendar_access=[]), "neutral")
    for name in ["web_search", "create_event"]:
        assert "not permitted" in await _execute_tool_call(name, {}, ctx)
    implementation.assert_not_awaited()


def test_neutral_has_no_task_inference_keywords():
    assert not get_persona("neutral").get("intent_keywords")
    assert not get_persona("neutral").get("intent_phrases")
    assert infer_persona_for_task("neutral", "plain personality")[0] == "family_assistant"
