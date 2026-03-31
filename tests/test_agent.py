"""
FruitcakeAI v5 — Agent tests

Covers:
- Tool schema format validation (all schemas have required keys)
- Persona-based tool filtering (restricted_assistant blocks web/rss tools)
- dispatch_tool_calls with an unknown tool returns an error string
- Tool count: family_assistant sees all built-in + MCP tools;
  restricted_assistant sees only the unblocked subset

No real LLM calls are made — the agent dispatch path is tested at the
tool-registry level using mock UserContext objects.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, patch

from app.agent.context import UserContext
from app.agent.tools import TOOL_SCHEMAS, _parse_iso_datetime, get_tools_for_user
from tests.conftest import TestSessionLocal


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


def test_list_library_documents_schema_fields():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "list_library_documents")
    props = schema["function"]["parameters"]["properties"]
    assert "limit" in props
    assert "scope_filter" in props


def test_list_library_documents_description_distinguishes_uploaded_library_docs():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "list_library_documents")
    description = schema["function"]["description"].lower()
    assert "uploaded" in description
    assert "library" in description
    assert "workspace" in description


def test_system_prompt_prefers_available_shell_tool_over_generic_refusal():
    ctx = _make_context(persona="family_assistant", blocked=[])
    prompt = ctx.to_system_prompt().lower()
    assert "use it and report the tool result" in prompt
    assert "do not claim a tool is unavailable" in prompt
    assert "shell_exec" in prompt
    assert "let the shell tool enforce what is blocked" in prompt


def test_system_prompt_includes_narrow_memory_capture_guidance():
    ctx = _make_context(persona="family_assistant", blocked=[])
    prompt = ctx.to_system_prompt().lower()
    assert "stable user fact" in prompt
    assert "durable preference" in prompt
    assert "recurring household procedure" in prompt
    assert "do not create memories for trivial one-off chatter" in prompt


def test_system_prompt_requires_confirmation_before_calendar_delete():
    ctx = _make_context(persona="family_assistant", blocked=[])
    prompt = ctx.to_system_prompt().lower()
    assert "never delete a calendar event unless the user explicitly confirms" in prompt
    assert "identify the exact event id" in prompt
    assert "delete_event" in prompt


def test_parse_iso_datetime_accepts_z_suffix():
    dt = _parse_iso_datetime("2026-04-01T00:00:00Z")
    assert dt.isoformat() == "2026-04-01T00:00:00+00:00"


def test_parse_iso_datetime_assumes_utc_for_naive_values():
    dt = _parse_iso_datetime("2026-04-01T00:00:00")
    assert dt.isoformat() == "2026-04-01T00:00:00+00:00"


def test_create_memory_schema_discourages_trivial_chatter():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "create_memory")
    description = schema["function"]["description"].lower()
    assert "stable personal facts" in description
    assert "durable preferences" in description
    assert "trivial one-off chatter" in description


def test_create_task_plan_schema_has_required_fields():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "create_task_plan")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "task_id" in props
    assert "goal" in props
    assert "goal" in required


def test_create_and_run_task_plan_schema_has_required_fields():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "create_and_run_task_plan")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "task_id" in props
    assert "goal" in props
    assert "goal" in required


def test_create_task_schema_has_required_fields():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "create_task")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "title" in props
    assert "instruction" in props
    assert "task_type" in props
    assert "deliver" in props
    assert set(["title", "instruction", "task_type", "deliver"]).issubset(set(required))


def test_run_task_now_schema_has_required_task_id():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "run_task_now")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "task_id" in props
    assert required == ["task_id"]


def test_search_places_schema_has_required_query():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "search_places")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "query" in props
    assert "near" in props
    assert "limit" in props
    assert required == ["query"]


def test_api_request_schema_has_required_fields():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "api_request")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "service" in props
    assert "endpoint" in props
    assert "query_params" in props
    assert "secret_name" in props
    assert required == ["service", "endpoint", "query_params"]


def test_get_daily_market_data_schema_has_required_symbol():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "get_daily_market_data")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "symbol" in props
    assert "days" in props
    assert "provider" in props
    assert "save_to_library" in props
    assert "output_format" in props
    assert required == ["symbol"]


def test_get_intraday_market_data_schema_has_required_fields():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "get_intraday_market_data")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "symbol" in props
    assert "interval" in props
    assert "bars" in props
    assert "provider" in props
    assert "save_to_library" in props
    assert "output_format" in props
    assert "extended_hours" in props
    assert required == ["symbol", "interval"]


def test_update_task_schema_has_required_fields():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "update_task")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "task_id" in props
    assert required == ["task_id"]


def test_list_tasks_schema_fields():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "list_tasks")
    props = schema["function"]["parameters"]["properties"]
    assert "limit" in props
    assert "status" in props


def test_get_task_schema_has_required_task_id():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "get_task")
    props = schema["function"]["parameters"]["properties"]
    required = schema["function"]["parameters"].get("required", [])
    assert "task_id" in props
    assert required == ["task_id"]


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


def test_restricted_assistant_blocks_web_tools():
    """restricted_assistant blocks web_search, fetch_page, get_feed_items, search_feeds."""
    from unittest.mock import patch, MagicMock
    restricted_blocked = ["web_search", "fetch_page", "get_feed_items", "search_feeds"]
    ctx = _make_context(persona="restricted_assistant", blocked=restricted_blocked)

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
    for blocked in restricted_blocked:
        assert blocked not in names, f"Blocked tool '{blocked}' still in restricted tool list"
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


def test_prefers_web_search_over_generic_search_tool():
    """Hide generic 'search' when internal 'web_search' is available."""
    from unittest.mock import patch, MagicMock

    ctx = _make_context(persona="family_assistant", blocked=[])
    fake_mcp_tools = [
        {"type": "function", "function": {"name": "web_search", "description": "", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "search", "description": "", "parameters": {"type": "object", "properties": {}}}},
    ]
    mock_registry = MagicMock()
    mock_registry._is_ready = True
    mock_registry.get_tools_for_agent.return_value = fake_mcp_tools

    with patch("app.mcp.registry.get_mcp_registry", return_value=mock_registry):
        tools = get_tools_for_user(ctx)

    names = [t["function"]["name"] for t in tools]
    assert "web_search" in names
    assert "search" not in names


def test_family_assistant_sees_filesystem_mcp_tools_when_registry_ready():
    """Filesystem MCP tools should flow through the normal agent tool surface."""
    from unittest.mock import patch, MagicMock

    ctx = _make_context(persona="family_assistant", blocked=[])
    fake_mcp_tools = [
        {"type": "function", "function": {"name": "list_directory", "description": "", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "find_files", "description": "", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "stat_file", "description": "", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "write_file", "description": "", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "make_directory", "description": "", "parameters": {"type": "object", "properties": {}}}},
    ]
    mock_registry = MagicMock()
    mock_registry._is_ready = True
    mock_registry.get_tools_for_agent.return_value = fake_mcp_tools

    with patch("app.mcp.registry.get_mcp_registry", return_value=mock_registry):
        tools = get_tools_for_user(ctx)

    names = [t["function"]["name"] for t in tools]
    assert "list_directory" in names
    assert "find_files" in names
    assert "stat_file" in names
    assert "read_file" in names
    assert "write_file" in names
    assert "make_directory" in names


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


@pytest.mark.asyncio
async def test_dispatch_propagates_approval_required():
    """ApprovalRequired must bubble out of dispatch_tool_calls for TaskRunner to handle."""
    from unittest.mock import AsyncMock, MagicMock, patch
    import app.agent.tools as tools_module
    from app.autonomy.approval import ApprovalRequired

    ctx = _make_context()

    mock_call = MagicMock()
    mock_call.function.name = "create_calendar_event"
    mock_call.function.arguments = "{}"
    mock_call.id = "call_approval"

    with patch.object(
        tools_module,
        "_call_tool",
        new_callable=AsyncMock,
        side_effect=ApprovalRequired("create_calendar_event"),
    ):
        with patch.object(tools_module, "_write_audit_log", new_callable=AsyncMock):
            with pytest.raises(ApprovalRequired):
                await tools_module.dispatch_tool_calls([mock_call], ctx)


@pytest.mark.asyncio
async def test_call_tool_routes_generic_search_to_web_search():
    """Generic MCP 'search' calls should flow through internal web_search when present."""
    from unittest.mock import AsyncMock, MagicMock, patch
    import app.agent.tools as tools_module

    ctx = _make_context()
    mock_registry = MagicMock()
    mock_registry.knows_tool.side_effect = lambda n: n == "web_search"
    mock_registry.call_tool = AsyncMock(return_value="ok")

    with patch("app.mcp.registry.get_mcp_registry", return_value=mock_registry):
        result = await tools_module._call_tool("search", {"query": "ap headlines", "limit": 3}, ctx)

    assert result == "ok"
    mock_registry.call_tool.assert_awaited_once_with(
        "web_search",
        {"query": "ap headlines", "max_results": 3},
        ctx,
    )


@pytest.mark.asyncio
async def test_search_places_tool_uses_backend_json_service():
    import app.agent.tools as tools_module

    ctx = _make_context()

    with patch("app.json_api.fetch_json", new=AsyncMock(return_value=[
        {
            "name": "Chili's Grill & Bar",
            "display_name": "Chili's Grill & Bar, 701 Northside Drive E, Statesboro, Georgia 30458, United States",
            "lat": "32.4481",
            "lon": "-81.7830",
        }
    ])):
        result = await tools_module._search_places(
            {"query": "Chili's", "near": "Statesboro, GA", "limit": 3},
            ctx,
        )

    assert "Place search results for: Chili's near Statesboro, GA" in result
    assert "701 Northside Drive E" in result


@pytest.mark.asyncio
async def test_api_request_tool_uses_backend_api_service():
    import app.agent.tools as tools_module

    ctx = _make_context()

    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        with patch("app.api_service.execute_api_request", new=AsyncMock(return_value="ISS visible pass results:\n\n[1] start_utc=2026-03-27T02:30:00+00:00 | duration_seconds=348 | max_elevation_deg=67.0")) as mocked:
            result = await tools_module._api_request(
                {
                    "service": "n2yo",
                    "endpoint": "iss_visual_passes",
                    "query_params": {
                        "satellite_id": 25544,
                        "lat": 32.4485,
                        "lon": -81.7832,
                        "alt_meters": 60,
                        "days": 1,
                        "min_visibility_seconds": 120,
                    },
                    "secret_name": "n2yo_api_key",
                },
                ctx,
            )

    assert "ISS visible pass results" in result
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_daily_market_data_tool_uses_market_data_service():
    import app.agent.tools as tools_module

    ctx = _make_context()

    fake_payload = {
        "rendered": "Alpha Vantage daily market data for SPY:\n\n[1] date=2026-03-27 | open=552.10 | high=555.25 | low=549.80 | close=553.42 | volume=1000000",
        "saved_document_id": 42,
        "saved_document_name": "spy_alphavantage_daily_1.csv",
    }

    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        with patch("app.market_data_service.get_daily_market_data", new=AsyncMock(return_value=fake_payload)) as mocked:
            result = await tools_module._get_daily_market_data(
                {
                    "symbol": "SPY",
                    "days": 1,
                    "provider": "alphavantage",
                    "save_to_library": True,
                    "output_format": "csv",
                },
                ctx,
            )

    assert "Alpha Vantage daily market data for SPY" in result
    assert "Saved to library as spy_alphavantage_daily_1.csv" in result
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_intraday_market_data_tool_uses_market_data_service():
    import app.agent.tools as tools_module

    ctx = _make_context()

    fake_payload = {
        "rendered": "Alpha Vantage intraday market data for SPY (60min):\n\n[1] timestamp=2026-03-27 16:00:00 | open=552.10 | high=555.25 | low=549.80 | close=553.42 | volume=1000000",
        "saved_document_id": 43,
        "saved_document_name": "spy_alphavantage_60m_intraday_1.csv",
    }

    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        with patch("app.market_data_service.get_intraday_market_data", new=AsyncMock(return_value=fake_payload)) as mocked:
            result = await tools_module._get_intraday_market_data(
                {
                    "symbol": "SPY",
                    "interval": "60min",
                    "bars": 1,
                    "provider": "alphavantage",
                    "save_to_library": True,
                    "output_format": "csv",
                },
                ctx,
            )

    assert "Alpha Vantage intraday market data for SPY (60min)" in result
    assert "Saved to library as spy_alphavantage_60m_intraday_1.csv" in result
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_task_tool_persists_recurring_task():
    import app.agent.tools as tools_module
    from app.db.models import Task

    ctx = _make_context()

    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        result = await tools_module._create_task(
            {
                "title": "Iran Watch",
                "instruction": "topic: Iran\nthreshold: medium",
                "task_type": "recurring",
                "schedule": "every:2h",
                "deliver": True,
                "profile": "topic_watcher",
            },
            ctx,
        )

    payload = json.loads(result)
    assert payload["created"] is True
    assert payload["task_type"] == "recurring"
    assert payload["profile"] == "topic_watcher"

    async with TestSessionLocal() as db:
        task = await db.get(Task, payload["task_id"])
        assert task is not None
        assert task.schedule == "every:2h"
        assert task.profile == "topic_watcher"
        assert task.requires_approval is True


@pytest.mark.asyncio
async def test_create_task_tool_defaults_to_requires_approval_true():
    import app.agent.tools as tools_module
    from app.db.models import Task

    ctx = _make_context()

    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        result = await tools_module._create_task(
            {
                "title": "Approval default",
                "instruction": "Do a saved task safely",
                "task_type": "one_shot",
                "deliver": True,
            },
            ctx,
        )

    payload = json.loads(result)
    assert payload["created"] is True
    assert payload["requires_approval"] is True

    async with TestSessionLocal() as db:
        task = await db.get(Task, payload["task_id"])
        assert task is not None
        assert task.requires_approval is True


@pytest.mark.asyncio
async def test_update_task_tool_updates_schedule_and_marks_plan_change():
    import app.agent.tools as tools_module
    from app.db.models import Task

    ctx = _make_context()

    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        created = json.loads(
            await tools_module._create_task(
                {
                    "title": "Morning Briefing",
                    "instruction": "Prepare a morning briefing for today using my calendar and current headlines.",
                    "task_type": "recurring",
                    "schedule": "every:1d",
                    "deliver": True,
                    "profile": "morning_briefing",
                },
                ctx,
            )
        )

        updated = json.loads(
            await tools_module._update_task(
                {"task_id": created["task_id"], "schedule": "every:2h"},
                ctx,
            )
        )

    assert updated["updated"] is True
    assert updated["schedule"] == "every:2h"
    assert updated["plan_inputs_changed"] is True

    async with TestSessionLocal() as db:
        task = await db.get(Task, created["task_id"])
        assert task is not None
        assert task.schedule == "every:2h"


@pytest.mark.asyncio
async def test_run_task_now_tool_queues_existing_task():
    import app.agent.tools as tools_module
    from app.db.models import Task

    ctx = _make_context()

    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        created = json.loads(
            await tools_module._create_task(
                {
                    "title": "Run me",
                    "instruction": "Do the task",
                    "task_type": "one_shot",
                    "deliver": True,
                },
                ctx,
            )
        )

        with patch("app.autonomy.runner.get_task_runner") as mocked_runner:
            mocked_runner.return_value.execute = AsyncMock()
            result = json.loads(await tools_module._run_task_now({"task_id": created["task_id"]}, ctx))

    assert result["queued"] is True
    assert result["task_id"] == created["task_id"]

    async with TestSessionLocal() as db:
        task = await db.get(Task, created["task_id"])
        assert task is not None
        assert task.status == "pending"


@pytest.mark.asyncio
async def test_list_tasks_and_get_task_return_owned_task_state():
    import app.agent.tools as tools_module

    ctx = _make_context()

    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        created = json.loads(
            await tools_module._create_task(
                {
                    "title": "World Cup Topic Watcher",
                    "instruction": "topic: World Cup\nthreshold: medium",
                    "task_type": "recurring",
                    "schedule": "every:8h",
                    "deliver": True,
                    "profile": "topic_watcher",
                    "active_hours_start": "07:00",
                    "active_hours_end": "21:00",
                    "active_hours_tz": "America/New_York",
                },
                ctx,
            )
        )
        listed = json.loads(await tools_module._list_tasks({"limit": 10}, ctx))
        fetched = json.loads(await tools_module._get_task({"task_id": created["task_id"]}, ctx))

    assert listed["count"] >= 1
    assert any(task["id"] == created["task_id"] for task in listed["tasks"])
    assert fetched["id"] == created["task_id"]
    assert fetched["schedule"] == "every:8h"
    assert fetched["active_hours_start"] == "07:00"
    assert fetched["active_hours_tz"] == "America/New_York"
