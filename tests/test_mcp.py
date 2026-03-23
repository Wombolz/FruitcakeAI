"""
FruitcakeAI v5 — MCP Registry tests

Covers:
- _to_litellm_schema() converts MCP tool format to LiteLLM format
- _extract_text() handles strings, MCP content blocks, and plain dicts
- MCPRegistry.startup() with an internal_python module registers tools correctly
- MCPRegistry.knows_tool() returns True/False
- MCPRegistry.get_tools_for_agent() returns LiteLLM-format schemas
- MCPRegistry.call_tool() dispatches to internal module and handles unknown tools
- Registry startup with no config file is non-fatal

No Docker containers are started — docker_stdio servers are skipped via a
config that only includes a test internal_python server.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from app.mcp.registry import MCPRegistry, _extract_text, _to_litellm_schema
from app.mcp.client import MCPClient


# ── _to_litellm_schema ─────────────────────────────────────────────────────────

def test_to_litellm_schema_basic():
    mcp_tool = {
        "name": "my_tool",
        "description": "Does something useful",
        "inputSchema": {
            "type": "object",
            "properties": {"arg1": {"type": "string"}},
            "required": ["arg1"],
        },
    }
    result = _to_litellm_schema(mcp_tool)
    assert result["type"] == "function"
    assert result["function"]["name"] == "my_tool"
    assert result["function"]["description"] == "Does something useful"
    assert result["function"]["parameters"]["properties"]["arg1"]["type"] == "string"


def test_to_litellm_schema_missing_input_schema():
    """Tools without inputSchema get an empty-properties object schema."""
    mcp_tool = {"name": "bare_tool", "description": "minimal"}
    result = _to_litellm_schema(mcp_tool)
    assert result["function"]["parameters"] == {"type": "object", "properties": {}}


def test_to_litellm_schema_missing_description():
    """Tools without a description get an empty string."""
    mcp_tool = {"name": "no_desc", "inputSchema": {"type": "object", "properties": {}}}
    result = _to_litellm_schema(mcp_tool)
    assert result["function"]["description"] == ""


# ── _extract_text ──────────────────────────────────────────────────────────────

def test_extract_text_plain_string():
    assert _extract_text("hello") == "hello"


def test_extract_text_mcp_content_block():
    """Standard MCP result format: {"content": [{"type": "text", "text": "..."}]}"""
    result = {"content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]}
    assert _extract_text(result) == "line one\nline two"


def test_extract_text_direct_string_content():
    assert _extract_text({"content": "direct string"}) == "direct string"


def test_extract_text_non_dict():
    assert _extract_text(42) == "42"


# ── MCPRegistry internals ──────────────────────────────────────────────────────

def _make_fake_module(tool_names: list[str], *, result_prefix: str = "result_of") -> types.ModuleType:
    """Build a minimal fake internal_python MCP module."""
    module = types.ModuleType("fake_mcp_module")

    def get_tools():
        return [
            {
                "name": name,
                "description": f"Fake tool {name}",
                "inputSchema": {"type": "object", "properties": {}},
            }
            for name in tool_names
        ]

    async def call_tool(tool_name, arguments, user_context=None):
        return f"{result_prefix}_{tool_name}"

    module.get_tools = get_tools
    module.call_tool = call_tool
    return module


@pytest.fixture
def fake_config(tmp_path: Path) -> Path:
    """Write a minimal mcp_config.yaml with one internal_python server."""
    cfg = {
        "mcp_servers": {
            "test_server": {
                "type": "internal_python",
                "module": "fake.module",
                "enabled": True,
            }
        }
    }
    config_file = tmp_path / "mcp_config.yaml"
    config_file.write_text(yaml.dump(cfg))
    return config_file


@pytest.mark.asyncio
async def test_registry_startup_loads_internal_tools(fake_config: Path):
    """startup() with an internal_python server registers its tools."""
    fake_module = _make_fake_module(["tool_a", "tool_b"])

    registry = MCPRegistry()
    with patch("importlib.import_module", return_value=fake_module):
        await registry.startup(config_path=fake_config)

    assert registry._is_ready
    assert registry.knows_tool("tool_a")
    assert registry.knows_tool("tool_b")
    assert not registry.knows_tool("nonexistent")


@pytest.mark.asyncio
async def test_registry_tools_in_litellm_format(fake_config: Path):
    """All registered tools must be in LiteLLM function-calling format."""
    fake_module = _make_fake_module(["tool_x"])

    registry = MCPRegistry()
    with patch("importlib.import_module", return_value=fake_module):
        await registry.startup(config_path=fake_config)

    for schema in registry.get_tools_for_agent():
        assert schema["type"] == "function"
        fn = schema["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn


@pytest.mark.asyncio
async def test_registry_call_tool_dispatches_to_module(fake_config: Path):
    fake_module = _make_fake_module(["greet"])

    registry = MCPRegistry()
    with patch("importlib.import_module", return_value=fake_module):
        await registry.startup(config_path=fake_config)

    result = await registry.call_tool("greet", {})
    assert result == "result_of_greet"


@pytest.mark.asyncio
async def test_registry_call_unknown_tool_returns_error(fake_config: Path):
    registry = MCPRegistry()
    with patch("importlib.import_module", return_value=_make_fake_module([])):
        await registry.startup(config_path=fake_config)

    result = await registry.call_tool("does_not_exist", {})
    assert "Unknown MCP tool" in result


@pytest.mark.asyncio
async def test_registry_startup_missing_config(tmp_path: Path):
    """No config file → registry is still ready, just has no tools."""
    registry = MCPRegistry()
    await registry.startup(config_path=tmp_path / "nonexistent.yaml")
    assert registry._is_ready
    assert registry.get_tools_for_agent() == []


@pytest.mark.asyncio
async def test_registry_disabled_server_skipped(tmp_path: Path):
    cfg = {
        "mcp_servers": {
            "disabled_server": {
                "type": "internal_python",
                "module": "fake.module",
                "enabled": False,
            }
        }
    }
    config_file = tmp_path / "mcp_config.yaml"
    config_file.write_text(yaml.dump(cfg))

    registry = MCPRegistry()
    # import_module should never be called for a disabled server
    with patch("importlib.import_module") as mock_import:
        await registry.startup(config_path=config_file)
        mock_import.assert_not_called()

    assert registry.get_tools_for_agent() == []


@pytest.mark.asyncio
async def test_registry_get_status(fake_config: Path):
    fake_module = _make_fake_module(["status_tool"])
    registry = MCPRegistry()
    with patch("importlib.import_module", return_value=fake_module):
        await registry.startup(config_path=fake_config)

    status = registry.get_status()
    assert status["ready"] is True
    assert status["tool_count"] == 1
    tool_names = [t["name"] for t in status["tools"]]
    assert "status_tool" in tool_names


@pytest.mark.asyncio
async def test_stdio_reader_waits_for_matching_response_id():
    client = MCPClient(server_name="test", command="docker", args=["run", "fake"])
    client._read = AsyncMock(side_effect=[
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"message": "working"}},
        {"jsonrpc": "2.0", "id": 999, "result": {"ignored": True}},
        {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}},
    ])

    msg = await client._read_response(request_id=2, timeout=1.0)
    assert msg is not None
    assert msg.get("id") == 2
    assert msg["result"]["ok"] is True


@pytest.mark.asyncio
async def test_stdio_call_retries_once_after_timeout():
    client = MCPClient(server_name="test", command="docker", args=["run", "fake"], timeout=1)
    client._connected = True
    client._process = MagicMock()
    client._write = AsyncMock()
    client._read_response = AsyncMock(
        side_effect=[
            asyncio.TimeoutError(),
            {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}},
        ]
    )
    client._reconnect_for_retry = AsyncMock(return_value=True)

    result = await client._call_stdio("search", {"query": "x"})
    assert result["success"] is True
    assert result["result"]["ok"] is True
    client._reconnect_for_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_registry_duplicate_tool_name_first_wins(tmp_path: Path):
    cfg = {
        "mcp_servers": {
            "server_a": {"type": "internal_python", "module": "fake.a", "enabled": True},
            "server_b": {"type": "internal_python", "module": "fake.b", "enabled": True},
        }
    }
    config_file = tmp_path / "mcp_config.yaml"
    config_file.write_text(yaml.dump(cfg))

    mod_a = _make_fake_module(["dup_tool"], result_prefix="a")
    mod_b = _make_fake_module(["dup_tool"], result_prefix="b")
    registry = MCPRegistry()

    with patch("importlib.import_module", side_effect=[mod_a, mod_b]):
        await registry.startup(config_path=config_file)

    status = registry.get_status()
    assert len(status["duplicate_tools"]) == 1
    conflict = status["duplicate_tools"][0]
    assert conflict["tool"] == "dup_tool"
    assert conflict["existing_server"] == "server_a"
    assert conflict["ignored_server"] == "server_b"

    result = await registry.call_tool("dup_tool", {})
    assert result == "a_dup_tool"


@pytest.mark.asyncio
async def test_registry_get_diagnostics_includes_servers(fake_config: Path):
    fake_module = _make_fake_module(["diag_tool"])
    registry = MCPRegistry()
    with patch("importlib.import_module", return_value=fake_module):
        await registry.startup(config_path=fake_config)

    diagnostics = registry.get_diagnostics()
    assert diagnostics["ready"] is True
    assert diagnostics["tool_count"] == 1
    assert diagnostics["servers"]
    assert diagnostics["servers"][0]["server"] == "test_server"


@pytest.mark.asyncio
async def test_registry_loads_filesystem_server_from_config(tmp_path: Path):
    cfg = {
        "mcp_servers": {
            "filesystem": {
                "type": "internal_python",
                "module": "app.mcp.servers.filesystem",
                "enabled": True,
            }
        }
    }
    config_file = tmp_path / "mcp_config.yaml"
    config_file.write_text(yaml.dump(cfg))

    registry = MCPRegistry()
    await registry.startup(config_path=config_file)

    tools = [schema["function"]["name"] for schema in registry.get_tools_for_agent()]
    assert "list_directory" in tools
    assert "find_files" in tools
    assert "read_file" in tools
    assert "write_file" in tools
