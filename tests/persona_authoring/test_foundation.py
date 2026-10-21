"""Isolated persona composition/dispatch tests; no model or live database."""
import asyncio
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.context import UserContext
from app.agent.persona_loader import get_persona
from app.agent.prompt_layers import PromptLayer, render_prompt_layers
from app.agent import tools


# Captured from merged v0.7.44 before layering, using synthetic fixture identity.
LEGACY = {
    "family_assistant": "0ab1e9a28c3be81498b260cad0e70ffb820903d0d691239cae456aae89584581",
    "restricted_assistant": "5992d56ac42bd8ebf989571df6d7ec47a08d814e7eb802980689d33d2fc5956f",
    "work_assistant": "b4bfb35af79d3c7cf88a91963da9c36030273ea2da74832c00425454d141543f",
    "news_researcher": "d275b7be731dbd6bc786158799f38f4fb7b753de36a1aca26dbb887c1def8326",
    "roadmap_verifier": "af9a84ca5638e714487aae8a404bc8933b016ffd2ede985cd8abf2daf9c74ff2",
}


def context(name="family_assistant"):
    definition = get_persona(name)
    user = SimpleNamespace(id=7, username="fixture-user", role="parent", persona=name,
        library_scopes=definition.get("library_scopes", ["personal"]),
        calendar_access=definition.get("calendar_access", ["personal"]), active_hours_tz="UTC")
    return UserContext.from_user(user)


@pytest.mark.parametrize("name", LEGACY)
def test_layering_preserves_every_existing_persona_prompt(name):
    ctx = context(name)
    layers = ctx.stable_prompt_layers()
    assert [layer.source for layer in layers] == ["persona_identity", "session_context",
        "persona_behavior", "runtime_contract", "access_restrictions"]
    assert render_prompt_layers(layers) == ctx.to_stable_system_prompt()
    assert sha256(ctx.to_stable_system_prompt().encode()).hexdigest() == LEGACY[name]


def test_sources_separate_personality_from_runtime_and_access_restrictions():
    ctx = context("restricted_assistant")
    ctx.is_incognito = True
    layers = {layer.source: layer.content for layer in ctx.stable_prompt_layers()}
    assert "family household" in layers["persona_identity"]
    assert "Tone:" in layers["persona_behavior"]
    assert "Current user:" in layers["session_context"]
    assert "Never delete a calendar event" in layers["runtime_contract"]
    assert "STRICT" in layers["access_restrictions"]
    assert "Incognito session" in layers["access_restrictions"]
    assert "web_search" in layers["access_restrictions"]


def test_turn_context_does_not_change_stable_prefix_and_nonlocal_layout_survives(monkeypatch):
    ctx = context()
    before = ctx.to_stable_system_prompt()
    monkeypatch.setattr(ctx, "_current_time_str", lambda: "FIXED TEST TIME")
    ctx.skill_prompt_additions = ["Fixture skill guidance"]
    assert ctx.to_stable_system_prompt() == before
    prompt = ctx.to_system_prompt()
    assert prompt.splitlines()[2] == "Current date and time: FIXED TEST TIME"
    assert "Fixture skill guidance" in prompt
    assert "<fruitcake_turn_context>" not in prompt


def test_empty_personality_content_does_not_remove_managed_rules():
    ctx = context()
    layers = tuple(PromptLayer(layer.source, ()) if layer.source.startswith("persona_") else layer
                   for layer in ctx.stable_prompt_layers())
    text = render_prompt_layers(layers)
    assert "family household" not in text and "Tone:" not in text
    assert "Never delete a calendar event" in text
    assert "Current user: fixture-user" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["direct", "model", "approval_replay"])
@pytest.mark.parametrize("policy", ["blocked", "cap", "blocked_and_cap"])
async def test_forbidden_tools_never_reach_implementation_or_approval_replay(monkeypatch, entrypoint, policy):
    ctx = context()
    if policy in {"blocked", "blocked_and_cap"}:
        ctx.blocked_tools = ["write_file"]
    if policy in {"cap", "blocked_and_cap"}:
        ctx.allowed_tool_cap = ["read_file"] if policy == "cap" else ["write_file"]
    implementation, audit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(tools, "_call_tool", implementation)
    monkeypatch.setattr(tools, "_write_audit_log", audit)
    arguments = {"path": "note.txt", "content": "not logged or written"}
    if entrypoint == "direct":
        result = await tools._execute_tool_call("write_file", arguments, ctx)
    elif entrypoint == "approval_replay":
        with pytest.raises(PermissionError, match="not permitted"):
            await tools.replay_waiting_approval_tool({"tool_name": "write_file", "arguments": arguments}, ctx)
        result = "not permitted"
    else:
        result = (await tools.dispatch_tool_calls([{"id": "call-denied", "function": {
            "name": "write_file", "arguments": '{"path":"note.txt"}'}}], ctx))[0]["content"]
    assert "not permitted" in result
    implementation.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("cap", [False, True])
async def test_search_alias_cannot_bypass_target_policy(monkeypatch, replay, cap):
    ctx = context()
    if cap:
        ctx.allowed_tool_cap = ["search"]
    else:
        ctx.blocked_tools = ["web_search"]
    registry = SimpleNamespace(knows_tool=lambda name: name == "web_search", call_tool=AsyncMock())
    monkeypatch.setattr("app.mcp.registry.get_mcp_registry", lambda: registry)
    if replay:
        with pytest.raises(PermissionError, match="web_search"):
            await tools.replay_waiting_approval_tool({"tool_name": "search", "arguments": {"query": "fixture"}}, ctx)
    else:
        result = await tools._execute_tool_call("search", {"query": "fixture"}, ctx)
        assert "web_search" in result and "not permitted" in result
    registry.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_calls_keep_existing_dispatch_and_audit_behavior(monkeypatch):
    ctx = context()
    ctx.allowed_tool_cap = ["read_file"]
    implementation, audit = AsyncMock(return_value="fixture result"), AsyncMock()
    monkeypatch.setattr(tools, "_call_tool", implementation)
    monkeypatch.setattr(tools, "_write_audit_log", audit)
    token = tools.reset_tool_execution_records()
    try:
        assert await tools._execute_tool_call("read_file", {"path": "note.txt"}, ctx) == "fixture result"
        await asyncio.sleep(0)
        implementation.assert_awaited_once()
        audit.assert_awaited_once()
        assert len(tools.get_tool_execution_records()) == 1
    finally:
        tools.restore_tool_execution_records(token)
