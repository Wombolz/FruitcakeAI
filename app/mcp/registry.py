"""
FruitcakeAI v5 — MCP Server Registry
Auto-discovery from config/mcp_config.yaml.

Supports two server types:
  internal_python  — Python modules that run in-process (calendar, web, rss)
  docker_stdio     — Docker containers invoked via stdio (python_refactoring, playwright, etc.)

Tool schemas are converted from MCP format → LiteLLM function-calling format at startup.
Adding a new server requires only a config entry — no code changes.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
import yaml

from app.mcp.client import MCPClient

log = structlog.get_logger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "mcp_config.yaml"


def _to_litellm_schema(tool: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an MCP tool schema to LiteLLM function-calling format.

    MCP:     {"name": ..., "description": ..., "inputSchema": {...}}
    LiteLLM: {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    """
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        },
    }


def _extract_text(result: Any) -> str:
    """
    Flatten an MCP tool result to a plain string for the LLM.
    MCP results are often {"content": [{"type": "text", "text": "..."}]}.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content", result)
        if isinstance(content, list):
            return "\n".join(
                item.get("text", str(item)) if isinstance(item, dict) else str(item)
                for item in content
            )
        return str(content)
    return str(result)


class MCPRegistry:
    """
    Singleton registry for all MCP tools available to the agent.

    Lifecycle:
      await registry.startup()   — call in FastAPI lifespan
      await registry.shutdown()  — call in FastAPI lifespan teardown
    """

    def __init__(self):
        # docker_stdio: persistent subprocess clients
        self._clients: Dict[str, MCPClient] = {}
        # internal_python: imported modules
        self._modules: Dict[str, Any] = {}
        # tool_name → (server_name, server_type)
        self._tool_map: Dict[str, Tuple[str, str]] = {}
        # All registered tools in LiteLLM format
        self._litellm_schemas: List[Dict[str, Any]] = []
        # Raw YAML config (used for status reporting)
        self._raw_config: Dict[str, Any] = {}
        # Duplicate tool name conflicts (deterministic first-wins policy)
        self._duplicate_tools: List[Dict[str, Any]] = []
        self._is_ready = False

    def _register_tool(self, tool: Dict[str, Any], server_name: str, server_type: str) -> None:
        """
        Register one tool by name using deterministic first-wins behavior.
        Duplicate names are retained in diagnostics and never silently override.
        """
        name = tool["name"]
        if name in self._tool_map:
            existing_server, existing_type = self._tool_map[name]
            conflict = {
                "tool": name,
                "existing_server": existing_server,
                "existing_type": existing_type,
                "ignored_server": server_name,
                "ignored_type": server_type,
                "policy": "first_wins",
            }
            self._duplicate_tools.append(conflict)
            log.error("Duplicate MCP tool name (ignored by first-wins policy)", **conflict)
            return

        self._tool_map[name] = (server_name, server_type)
        self._litellm_schemas.append(_to_litellm_schema(tool))

    async def startup(self, config_path: Optional[Path] = None) -> None:
        """Load config/mcp_config.yaml and initialize all enabled servers."""
        path = config_path or _CONFIG_PATH
        if not path.exists():
            log.warning("MCP config not found — no MCP tools will be available", path=str(path))
            self._is_ready = True
            return

        with open(path) as f:
            self._raw_config = yaml.safe_load(f) or {}

        servers = self._raw_config.get("mcp_servers", {})
        for server_name, config in servers.items():
            if not config.get("enabled", True):
                log.info("MCP server disabled (skipping)", server=server_name)
                continue

            server_type = config.get("type", "docker_stdio")
            if server_type == "internal_python":
                await self._init_internal(server_name, config)
            elif server_type == "docker_stdio":
                await self._init_docker(server_name, config)
            else:
                log.warning("Unknown MCP server type", server=server_name, type=server_type)

        self._is_ready = True
        log.info(
            "MCP registry ready",
            tool_count=len(self._litellm_schemas),
            tools=[s["function"]["name"] for s in self._litellm_schemas],
        )

    async def _init_internal(self, server_name: str, config: Dict[str, Any]) -> None:
        """Import an internal Python MCP server module and register its tools."""
        module_path = config.get("module")
        if not module_path:
            log.error("No module path for internal_python server", server=server_name)
            return
        try:
            module = importlib.import_module(module_path)
            tools = module.get_tools()  # expected: List[MCP tool schema dicts]
            self._modules[server_name] = module
            for tool in tools:
                self._register_tool(tool, server_name, "internal_python")
            log.info(
                "Internal MCP server loaded",
                server=server_name,
                tools=[t["name"] for t in tools],
            )
        except Exception as e:
            log.error("Failed to load internal MCP server", server=server_name, error=str(e))

    async def _init_docker(self, server_name: str, config: Dict[str, Any]) -> None:
        """
        Connect to a Docker stdio MCP server.
        Failures are non-fatal — the server is simply omitted from the tool list.
        """
        image = config.get("image")
        if not image:
            log.error("No image specified for docker_stdio server", server=server_name)
            return

        client = MCPClient(
            server_name=server_name,
            command="docker",
            args=["run", "-i", "--rm", image],
            timeout=config.get("timeout", 60),
        )
        ok = await client.connect()
        if ok:
            self._clients[server_name] = client
            for tool in client.get_tools():
                self._register_tool(tool, server_name, "docker_stdio")
            log.info(
                "Docker MCP server connected",
                server=server_name,
                image=image,
                tools=[t["name"] for t in client.get_tools()],
            )
        else:
            log.warning(
                "Docker MCP server unavailable (Docker may not be running or image not pulled)",
                server=server_name,
                image=image,
                hint=f"docker pull {image}",
            )

    # ── Tool access ───────────────────────────────────────────────────────────

    def get_tools_for_agent(self) -> List[Dict[str, Any]]:
        """Return all registered MCP tools in LiteLLM function-calling schema format."""
        return list(self._litellm_schemas)

    def knows_tool(self, tool_name: str) -> bool:
        """Return True if this tool is registered in the MCP registry."""
        return tool_name in self._tool_map

    # ── Tool execution ────────────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        user_context: Any = None,
    ) -> str:
        """
        Dispatch a tool call to the appropriate server and return a plain string result.
        The string is appended to conversation history as a tool-role message.
        """
        if tool_name not in self._tool_map:
            return f"Unknown MCP tool: {tool_name}"

        server_name, server_type = self._tool_map[tool_name]

        if server_type == "internal_python":
            module = self._modules.get(server_name)
            if not module:
                return f"Internal module {server_name} is not loaded"
            try:
                result = await module.call_tool(tool_name, arguments, user_context)
                return str(result)
            except Exception as e:
                log.error("Internal MCP tool failed", tool=tool_name, error=str(e))
                return f"Tool {tool_name} failed: {e}"

        if server_type == "docker_stdio":
            client = self._clients.get(server_name)
            if not client or not client.is_connected():
                return f"MCP server '{server_name}' is not available"
            raw = await client.call_tool(tool_name, arguments)
            if raw["success"]:
                return _extract_text(raw["result"])
            return f"Tool {tool_name} failed: {raw.get('error', 'unknown error')}"

        return f"Unsupported server type for tool: {tool_name}"

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """
        Return registry status for GET /admin/tools.
        Includes enabled tools and disabled servers from config.
        """
        tools = []
        for schema in self._litellm_schemas:
            fn = schema["function"]
            name = fn["name"]
            server_name, server_type = self._tool_map.get(name, ("unknown", "unknown"))
            client = self._clients.get(server_name)
            tools.append({
                "name": name,
                "description": fn.get("description", ""),
                "server": server_name,
                "type": server_type,
                "available": True,
                "connected": client.is_connected() if client else True,
            })

        disabled = []
        for server_name, config in self._raw_config.get("mcp_servers", {}).items():
            if not config.get("enabled", True):
                disabled.append({
                    "server": server_name,
                    "type": config.get("type", "unknown"),
                    "available": False,
                    "reason": "disabled in mcp_config.yaml",
                })

        return {
            "ready": self._is_ready,
            "tool_count": len(tools),
            "tools": tools,
            "disabled_servers": disabled,
            "duplicate_tools": list(self._duplicate_tools),
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        """Expanded MCP diagnostics for targeted admin troubleshooting."""
        servers: List[Dict[str, Any]] = []
        configured = self._raw_config.get("mcp_servers", {})
        for server_name, config in configured.items():
            enabled = config.get("enabled", True)
            server_type = config.get("type", "unknown")
            entry: Dict[str, Any] = {
                "server": server_name,
                "type": server_type,
                "enabled": enabled,
                "declared_tools": config.get("tools", []),
            }

            if not enabled:
                entry["status"] = "disabled"
                servers.append(entry)
                continue

            if server_type == "docker_stdio":
                client = self._clients.get(server_name)
                if client is None:
                    entry["status"] = "not_connected"
                else:
                    status = client.get_status()
                    entry["status"] = "connected" if status.get("connected") else "error"
                    entry["connection_state"] = status.get("connection_state")
                    entry["last_error"] = status.get("last_error")
                    entry["stderr_tail"] = status.get("stderr_tail", [])
                    entry["registered_tools"] = status.get("tools", [])
            elif server_type == "internal_python":
                loaded = server_name in self._modules
                entry["status"] = "loaded" if loaded else "error"
                entry["registered_tools"] = [
                    tool_name
                    for tool_name, (owner, _) in self._tool_map.items()
                    if owner == server_name
                ]
            else:
                entry["status"] = "unknown_type"

            servers.append(entry)

        return {
            "ready": self._is_ready,
            "tool_count": len(self._litellm_schemas),
            "duplicate_tools": list(self._duplicate_tools),
            "servers": servers,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Disconnect all Docker stdio servers."""
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()
        log.info("MCP registry shut down")


# ── Singleton ─────────────────────────────────────────────────────────────────

_registry: Optional[MCPRegistry] = None


def get_mcp_registry() -> MCPRegistry:
    global _registry
    if _registry is None:
        _registry = MCPRegistry()
    return _registry
