"""
FruitcakeAI v5 — Agent tests

Covers:
- Tool schema format validation (all schemas have required keys)
- Persona-based tool filtering (kids_assistant blocks web/rss tools)
- dispatch_tool_calls with an unknown tool returns an error string
- Tool count: family_assistant sees all built-in + MCP tools;
  kids_assistant sees only the unblocked subset

No real LLM calls are made — the agent dispatch path is tested at the
tool-registry level using mock UserContext objects.
"""

from __future__ import annotations

import pytest

from app.agent.context import UserContext
from app.agent.tools import TOOL_SCHEMAS, get_tools_for_user


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_context(persona: str = "family_assistant", blocked: list[str] | None = None) -> UserContext:
    return UserContext(
        user_id=1,
        username="tester",
        role="parent",
        persona=persona,
        blocked_tools=blocked or [],
    )


# ── Schema format tests ────────────────────────────────────────────────────────

def test_all_tool_schemas_have_required_keys():
    """Every built-in tool schema must be valid LiteLLM function-calling format."""
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function", f"Missing 'type' in {schema}"
        fn = schema["function"]
        assert "name" in fn, f"Missing 'name' in {schema}"
        assert "description" in fn, f"Missing 'description' in {fn['name']}"
        assert "parameters" in fn, f"Missing 'parameters' in {fn['name']}"
        params = fn["parameters"]
        assert params.get("type") == "object", f"'parameters.type' must be 'object' in {fn['name']}"
        assert "properties" in params, f"Missing 'properties' in {fn['name']}"


def test_search_library_has_query_parameter():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "search_library")
    props = schema["function"]["parameters"]["properties"]
    assert "query" in props
    assert props["query"]["type"] == "string"


def test_search_library_top_k_default():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "search_library")
    props = schema["function"]["parameters"]["properties"]
    assert "top_k" in props
    assert props["top_k"].get("default", 0) >= 20, "top_k default should be ≥ 20"


def test_summarize_document_has_document_name_parameter():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "summarize_document")
    required = schema["function"]["parameters"].get("required", [])
    assert "document_name" in required


# ── Persona / blocked-tools filtering ─────────────────────────────────────────

def test_family_assistant_no_blocked_tools():
    """family_assistant should not block any built-in tools."""
    from unittest.mock import patch, MagicMock
    ctx = _make_context(persona="family_assistant", blocked=[])
    mock_registry = MagicMock()
    mock_registry._is_ready = False
    # get_mcp_registry is imported locally inside get_tools_for_user — patch at source module
    with patch("app.mcp.registry.get_mcp_registry", return_value=mock_registry):
        tools = get_tools_for_user(ctx)
    names = [t["function"]["name"] for t in tools]
    assert "search_library" in names
    assert "summarize_document" in names


def test_kids_assistant_blocks_web_tools():
    """kids_assistant blocks web_search, fetch_page, get_feed_items, search_feeds."""
    from unittest.mock import patch, MagicMock
    kids_blocked = ["web_search", "fetch_page", "get_feed_items", "search_feeds"]
    ctx = _make_context(persona="kids_assistant", blocked=kids_blocked)

    fake_mcp_tools = [
        {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object", "properties": {}}}}
        for name in ["web_search", "fetch_page", "get_feed_items", "search_feeds", "list_events"]
    ]
    mock_registry = MagicMock()
    mock_registry._is_ready = True
    mock_registry.get_tools_for_agent.return_value = fake_mcp_tools

    with patch("app.mcp.registry.get_mcp_registry", return_value=mock_registry):
        tools = get_tools_for_user(ctx)

    names = [t["function"]["name"] for t in tools]
    for blocked in kids_blocked:
        assert blocked not in names, f"Blocked tool '{blocked}' still in kids tool list"
    assert "list_events" in names


def test_blocked_tools_list_exact_match():
    """Blocking a tool by exact name must not affect other tools."""
    from unittest.mock import patch, MagicMock
    ctx = _make_context(blocked=["search_library"])
    mock_registry = MagicMock()
    mock_registry._is_ready = False
    with patch("app.mcp.registry.get_mcp_registry", return_value=mock_registry):
        tools = get_tools_for_user(ctx)
    names = [t["function"]["name"] for t in tools]
    assert "search_library" not in names
    assert "summarize_document" in names


# ── Dispatch tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error_string():
    """Dispatching an unknown tool must not raise — returns error string."""
    from unittest.mock import AsyncMock, MagicMock, patch
    import app.agent.tools as tools_module

    ctx = _make_context()

    mock_call = MagicMock()
    mock_call.function.name = "does_not_exist"
    mock_call.function.arguments = "{}"
    mock_call.id = "call_abc"

    mock_registry = MagicMock()
    mock_registry.knows_tool.return_value = False

    with patch.object(tools_module, "_write_audit_log", new_callable=AsyncMock):
        with patch("app.mcp.registry.get_mcp_registry", return_value=mock_registry):
            results = await tools_module.dispatch_tool_calls([mock_call], ctx)

    assert len(results) == 1
    assert results[0]["role"] == "tool"
    assert "Unknown tool" in results[0]["content"]


@pytest.mark.asyncio
async def test_dispatch_returns_tool_role_messages():
    """dispatch_tool_calls always returns messages with role='tool'."""
    from unittest.mock import AsyncMock, MagicMock, patch
    import app.agent.tools as tools_module

    ctx = _make_context()

    mock_call = MagicMock()
    mock_call.function.name = "search_library"
    mock_call.function.arguments = '{"query": "test"}'
    mock_call.id = "call_xyz"

    with patch.object(tools_module, "_call_tool", new_callable=AsyncMock, return_value="mock result"):
        with patch.object(tools_module, "_write_audit_log", new_callable=AsyncMock):
            results = await tools_module.dispatch_tool_calls([mock_call], ctx)

    assert results[0]["role"] == "tool"
    assert results[0]["tool_call_id"] == "call_xyz"
    assert results[0]["content"] == "mock result"
