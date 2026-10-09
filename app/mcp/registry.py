"""
FruitcakeAI v5 — MCP Server Registry
Auto-discovery from config/mcp_config.yaml.

Supported server types:
  internal_python  — Python modules that run in-process (calendar, web, rss)
  docker_stdio     — Docker containers invoked via stdio (python_refactoring, playwright, etc.)
  stdio            — Local subprocesses managed by the official MCP SDK
  streamable_http  — Standard MCP HTTP endpoints managed by the SDK
  http             — Legacy POST-only companion-app compatibility

Tool schemas are converted from MCP format → LiteLLM function-calling format at startup.
Adding a new server requires only a config entry — no code changes.
"""

from __future__ import annotations

import importlib
import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from app.agent.runtime.models import ToolOutputText
from app.artifacts import artifact_registry
from app.mcp.client import MCP_APP_MIME_TYPE, MCPClient

log = structlog.get_logger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "mcp_config.yaml"
_MCP_APP_RESOURCE_MAX_BYTES = 512_000
_MCP_APP_TOOL_ARGUMENT_MAX_BYTES = 20_000
_MCP_APP_TOOL_RESULT_MAX_BYTES = 96_000


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


def _find_invalid_schema_field(value: Any, path: str = "parameters") -> str | None:
    """Return the first malformed JSON Schema field that providers reject."""
    if not isinstance(value, dict):
        return f"{path} must be an object"
    properties = value.get("properties")
    if properties is not None and not isinstance(properties, dict):
        return f"{path}.properties must be an object"
    for key, child in value.items():
        child_path = f"{path}.{key}"
        if isinstance(child, dict):
            invalid = _find_invalid_schema_field(child, child_path)
            if invalid:
                return invalid
        elif isinstance(child, list):
            for index, item in enumerate(child):
                if isinstance(item, dict):
                    invalid = _find_invalid_schema_field(item, f"{child_path}[{index}]")
                    if invalid:
                        return invalid
    return None


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


def _extract_structured_content(result: Any) -> Dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent") or result.get("structured_content")
    return dict(structured) if isinstance(structured, dict) else None


def _tool_ui_metadata(tool: Dict[str, Any]) -> Dict[str, Any] | None:
    metadata = tool.get("_meta")
    if not isinstance(metadata, dict):
        return None
    ui = metadata.get("ui")
    if isinstance(ui, dict):
        return dict(ui)
    # Compatibility with the pre-2026 flat MCP Apps metadata spelling.
    resource_uri = metadata.get("ui/resourceUri")
    visibility = metadata.get("ui/visibility")
    if resource_uri is None and visibility is None:
        return None
    return {"resourceUri": resource_uri, "visibility": visibility}


def _tool_is_model_visible(tool: Dict[str, Any]) -> bool:
    ui = _tool_ui_metadata(tool)
    if not ui or "visibility" not in ui:
        return True
    visibility = ui.get("visibility")
    return isinstance(visibility, list) and "model" in visibility


def _bounded_json_value(value: Any, max_bytes: int) -> Any:
    """Return JSON-safe data when it fits; otherwise return a compact marker."""
    try:
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return {"omitted": True, "reason": "not_json_serializable"}
    if len(encoded) <= max_bytes:
        return value
    return {"omitted": True, "reason": "size_limit", "size_bytes": len(encoded)}


def _serialize_user_context(user_context: Any) -> Dict[str, Any]:
    if user_context is None:
        return {}
    if isinstance(user_context, dict):
        source = user_context
    else:
        source = {
            "user_id": getattr(user_context, "user_id", None),
            "username": getattr(user_context, "username", None),
            "role": getattr(user_context, "role", None),
            "persona": getattr(user_context, "persona", None),
            "timezone": getattr(user_context, "timezone", None),
        }
    return {k: v for k, v in source.items() if v is not None}


class MCPRegistry:
    """
    Singleton registry for all MCP tools available to the agent.

    Lifecycle:
      await registry.startup()   — call in FastAPI lifespan
      await registry.shutdown()  — call in FastAPI lifespan teardown
    """

    def __init__(self):
        # SDK transports and legacy HTTP: persistent clients
        self._clients: Dict[str, MCPClient] = {}
        # internal_python: imported modules
        self._modules: Dict[str, Any] = {}
        # server_name -> raw config
        self._server_configs: Dict[str, Dict[str, Any]] = {}
        # tool_name → (server_name, server_type)
        self._tool_map: Dict[str, Tuple[str, str]] = {}
        # App-only tools are discoverable for diagnostics but never offered to
        # or callable by the model. Interactive app calls are a later slice.
        self._app_tool_map: Dict[str, Tuple[str, str]] = {}
        self._app_tool_definitions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._tool_definitions: Dict[str, Dict[str, Any]] = {}
        self._ui_resources: Dict[str, set[str]] = {}
        # All registered tools in LiteLLM format
        self._litellm_schemas: List[Dict[str, Any]] = []
        # Raw YAML config (used for status reporting)
        self._raw_config: Dict[str, Any] = {}
        # Duplicate tool name conflicts (deterministic first-wins policy)
        self._duplicate_tools: List[Dict[str, Any]] = []
        self._invalid_tools: List[Dict[str, Any]] = []
        self._is_ready = False
        self._server_tools: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}

    def _register_tool(self, tool: Dict[str, Any], server_name: str, server_type: str) -> None:
        """
        Register one tool by name using deterministic first-wins behavior.
        Duplicate names are retained in diagnostics and never silently override.
        """
        name = tool["name"]
        ui = _tool_ui_metadata(tool)
        resource_uri = ui.get("resourceUri") if ui else None
        if isinstance(resource_uri, str) and resource_uri.startswith("ui://"):
            self._ui_resources.setdefault(server_name, set()).add(resource_uri)
        if ui is not None and not _tool_is_model_visible(tool):
            if name not in self._app_tool_map:
                self._app_tool_map[name] = (server_name, server_type)
            self._app_tool_definitions[(server_name, name)] = dict(tool)
            return
        schema = _to_litellm_schema(tool)
        invalid_reason = _find_invalid_schema_field(schema["function"]["parameters"])
        if invalid_reason:
            diagnostic = {
                "tool": name,
                "server": server_name,
                "type": server_type,
                "reason": invalid_reason,
            }
            self._invalid_tools.append(diagnostic)
            log.error("Invalid MCP tool schema (tool quarantined)", **diagnostic)
            return
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
        self._tool_definitions[name] = dict(tool)
        self._litellm_schemas.append(schema)

    def _set_server_tools(self, name: str, kind: str, tools: List[Dict[str, Any]]) -> None:
        # Rebuild atomically in config order so live changes preserve first-wins
        # collision handling and removed tools disappear from future turns.
        self._server_tools[name] = (kind, list(tools))
        self._tool_map.clear()
        self._app_tool_map.clear()
        self._app_tool_definitions.clear()
        self._tool_definitions.clear()
        self._ui_resources.clear()
        self._litellm_schemas.clear()
        self._duplicate_tools.clear()
        self._invalid_tools.clear()
        order = dict.fromkeys([*self._server_configs, *self._server_tools])
        for server in order:
            server_kind, entries = self._server_tools.get(server, ("", []))
            for tool in entries:
                self._register_tool(tool, server, server_kind)

    @staticmethod
    def _config_values(config: Dict[str, Any], key: str) -> Dict[str, str]:
        values = dict(config.get(key, {}) or {})
        for name, variable in (config.get("env_from" if key == "env" else f"{key}_from_env", {}) or {}).items():
            if variable not in os.environ:
                raise ValueError(f"Missing environment variable for MCP {key}: {variable}")
            values[name] = os.environ[variable]
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
            raise ValueError(f"MCP {key} must contain string keys and values")
        return values

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
            self._server_configs[server_name] = dict(config)
            if not config.get("enabled", True):
                log.info("MCP server disabled (skipping)", server=server_name)
                continue

            server_type = config.get("type", "docker_stdio")
            if server_type == "internal_python":
                await self._init_internal(server_name, config)
            elif server_type == "docker_stdio":
                await self._init_docker(server_name, config)
            elif server_type in {"stdio", "streamable_http", "http"}:
                await self._init_external(server_name, config, server_type)
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
            self._set_server_tools(server_name, "internal_python", tools)
            log.info(
                "Internal MCP server loaded",
                server=server_name,
                tools=[t["name"] for t in tools],
            )
        except Exception as e:
            log.error("Failed to load internal MCP server", server=server_name, error=str(e))

    async def _init_docker(self, server_name: str, config: Dict[str, Any]) -> None:
        image = config.get("image")
        if not image:
            log.error("No image specified for docker_stdio server", server=server_name)
            return
        await self._init_external(server_name, {
            **config,
            "command": "docker",
            "args": ["run", "-i", "--rm", *list(config.get("docker_run_args", []) or []),
                     image, *list(config.get("server_args", []) or [])],
        }, "docker_stdio")

    async def _init_http(self, server_name: str, config: Dict[str, Any]) -> None:
        await self._init_external(server_name, config, "http")

    async def _init_external(self, server_name: str, config: Dict[str, Any], kind: str) -> None:
        client = None
        try:
            transport = "stdio" if kind == "docker_stdio" else kind
            if not config.get("command" if transport == "stdio" else "url"):
                raise ValueError("MCP server requires command for stdio or url for HTTP")
            client = MCPClient(
                server_name=server_name,
                command=config.get("command"), args=list(config.get("args", []) or []),
                server_url=config.get("url"), transport=transport,
                timeout=config.get("timeout", 60 if transport == "stdio" else 30),
                cwd=config.get("cwd"), env=self._config_values(config, "env"),
                headers=self._config_values(config, "headers"),
                on_tools_changed=lambda tools: self._set_server_tools(server_name, kind, tools),
            )
            self._clients[server_name] = client
            if await client.connect():
                self._set_server_tools(server_name, kind, client.get_tools())
                log.info("MCP server connected", server=server_name, transport=transport)
            else:
                log.warning("MCP server unavailable", server=server_name,
                            error=client.get_status().get("last_error"))
        except Exception as exc:
            if client is not None:
                await client.disconnect()
            log.warning("MCP server configuration failed", server=server_name, error_type=type(exc).__name__)

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
                return ToolOutputText(
                    _extract_text(result),
                    structured_content=_extract_structured_content(result),
                )
            except Exception as e:
                log.error("Internal MCP tool failed", tool=tool_name, error=str(e))
                return f"Tool {tool_name} failed: {e}"

        if server_type in {"docker_stdio", "stdio", "streamable_http", "http"}:
            client = self._clients.get(server_name)
            if not client:
                return f"MCP server '{server_name}' is not available"
            effective_args = dict(arguments)
            config = self._server_configs.get(server_name, {})
            if config.get("pass_user_context"):
                effective_args["_fruitcake_user_context"] = _serialize_user_context(user_context)
            raw = await client.call_tool(tool_name, effective_args)
            if raw["success"]:
                structured_content = _extract_structured_content(raw["result"])
                app_artifact = self._build_mcp_app_artifact(
                    tool_name=tool_name,
                    server_name=server_name,
                    arguments=arguments,
                    result=raw["result"],
                )
                if app_artifact is not None:
                    structured_content = dict(structured_content or {})
                    artifacts = structured_content.get("artifacts")
                    if not isinstance(artifacts, list):
                        artifacts = []
                    structured_content["artifacts"] = [*artifacts, app_artifact]
                return ToolOutputText(
                    _extract_text(raw["result"]),
                    structured_content=structured_content,
                )
            return f"Tool {tool_name} failed: {raw.get('error', 'unknown error')}"

        return f"Unsupported server type for tool: {tool_name}"

    def _build_mcp_app_artifact(
        self,
        *,
        tool_name: str,
        server_name: str,
        arguments: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        tool = self._tool_definitions.get(tool_name, {})
        ui = _tool_ui_metadata(tool)
        resource_uri = ui.get("resourceUri") if ui else None
        if not isinstance(resource_uri, str) or not resource_uri.startswith("ui://"):
            return None
        fallback = _extract_text(result).strip() or f"{tool_name} completed."
        candidate = {
            "type": "core.mcp_app",
            "schema_version": 1,
            "title": str(tool.get("title") or tool_name.replace("_", " ").title())[:200],
            "summary": str(tool.get("description") or "Interactive MCP App result")[:600],
            "payload": {
                "tool_input": _bounded_json_value(arguments, 20_000),
                "tool_result": _bounded_json_value(result, 36_000),
            },
            "resources": [{
                "uri": resource_uri,
                "media_type": MCP_APP_MIME_TYPE,
                "title": "MCP App interface",
                "role": "ui",
            }],
            "provenance": {
                "provider": "mcp",
                "server": server_name,
                "tool": tool_name,
            },
            "presentation": {
                "preferred": "inline",
                "expandable": True,
                "renderer": "mcp_app",
                "ui_resource": resource_uri,
            },
            "fallback": {
                "media_type": "text/plain",
                "content": fallback[:24_000],
            },
        }
        try:
            return artifact_registry.validate(candidate).model_dump(mode="json", exclude_none=True)
        except ValueError as exc:
            log.warning(
                "MCP App artifact rejected",
                server=server_name,
                tool=tool_name,
                error=str(exc),
            )
            return None

    async def read_mcp_app_resource(self, server_name: str, uri: str) -> Dict[str, Any]:
        """Resolve one linked MCP App UI resource through its owning server."""
        if not uri.startswith("ui://"):
            raise ValueError("MCP App resources must use the ui:// scheme")
        if uri not in self._ui_resources.get(server_name, set()):
            raise LookupError("MCP App resource is not linked by a registered tool")
        client = self._clients.get(server_name)
        if client is None:
            raise LookupError(f"MCP server '{server_name}' is not available")
        raw = await client.read_resource(uri)
        if not raw.get("success"):
            raise RuntimeError(raw.get("error") or "MCP resource read failed")
        result = raw.get("result")
        contents = result.get("contents") if isinstance(result, dict) else None
        if not isinstance(contents, list):
            raise ValueError("MCP resource response did not include contents")
        for item in contents:
            if not isinstance(item, dict) or item.get("uri") != uri:
                continue
            media_type = item.get("mimeType") or item.get("mime_type")
            if media_type != MCP_APP_MIME_TYPE:
                raise ValueError("MCP App resource has an unsupported media type")
            if isinstance(item.get("text"), str):
                data = item["text"].encode("utf-8")
            elif isinstance(item.get("blob"), str):
                try:
                    data = base64.b64decode(item["blob"], validate=True)
                except ValueError as exc:
                    raise ValueError("MCP App resource blob is not valid base64") from exc
            else:
                raise ValueError("MCP App resource must contain text or a blob")
            if len(data) > _MCP_APP_RESOURCE_MAX_BYTES:
                raise ValueError("MCP App resource exceeds the size limit")
            try:
                html = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("MCP App resource must be UTF-8 HTML") from exc
            return {
                "server": server_name,
                "uri": uri,
                "mime_type": media_type,
                "html": html,
            }
        raise LookupError("MCP App resource was not returned by the server")

    async def call_mcp_app_tool(
        self,
        *,
        server_name: str,
        resource_uri: str,
        tool_name: str,
        arguments: Dict[str, Any],
        user_context: Any = None,
    ) -> Dict[str, Any]:
        """Call one same-server app-only tool after enforcing read-only policy."""
        if not resource_uri.startswith("ui://"):
            raise ValueError("MCP App resources must use the ui:// scheme")
        if resource_uri not in self._ui_resources.get(server_name, set()):
            raise LookupError("MCP App resource is not linked by a registered tool")

        definition = self._app_tool_definitions.get((server_name, tool_name))
        if definition is None:
            raise LookupError("MCP App tool is not declared by this server")
        ui = _tool_ui_metadata(definition) or {}
        if ui.get("resourceUri") != resource_uri:
            raise PermissionError("MCP App tool is not linked to this UI resource")
        annotations = definition.get("annotations")
        if not isinstance(annotations, dict) or annotations.get("readOnlyHint") is not True:
            raise PermissionError("MCP App tool is not declared read-only")
        config = self._server_configs.get(server_name, {})
        trust_boundary = config.get("trust_boundary")
        if not isinstance(trust_boundary, dict) or trust_boundary.get("first_party") is not True:
            raise PermissionError("MCP App tool requires approval for a non-first-party server")

        encoded_arguments = json.dumps(
            arguments,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_arguments) > _MCP_APP_TOOL_ARGUMENT_MAX_BYTES:
            raise ValueError("MCP App tool arguments exceed the size limit")
        schema = definition.get("inputSchema") or {"type": "object", "properties": {}}
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(arguments)
        except SchemaError as exc:
            raise ValueError("MCP App tool has an invalid input schema") from exc
        except ValidationError as exc:
            raise ValueError(f"MCP App tool arguments are invalid: {exc.message}") from exc

        client = self._clients.get(server_name)
        if client is None:
            raise LookupError(f"MCP server '{server_name}' is not available")
        effective_args = dict(arguments)
        if config.get("pass_user_context"):
            effective_args["_fruitcake_user_context"] = _serialize_user_context(user_context)
        raw = await client.call_tool(tool_name, effective_args)
        if not raw.get("success"):
            raise RuntimeError(raw.get("error") or "MCP App tool call failed")
        result = raw.get("result")
        encoded_result = json.dumps(
            result,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_result) > _MCP_APP_TOOL_RESULT_MAX_BYTES:
            raise ValueError("MCP App tool result exceeds the size limit")
        return {
            "server": server_name,
            "resource_uri": resource_uri,
            "tool": tool_name,
            "result": result,
        }

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
                "classification": self._server_configs.get(server_name, {}).get("classification", ""),
                "shipping_default": bool(self._server_configs.get(server_name, {}).get("shipping_default", False)),
                "available": True,
                "connected": client.is_connected() if client else True,
                "mcp_app": _tool_ui_metadata(self._tool_definitions.get(name, {})) is not None,
            })

        disabled = []
        for server_name, config in self._raw_config.get("mcp_servers", {}).items():
            if not config.get("enabled", True):
                disabled.append({
                    "server": server_name,
                    "type": config.get("type", "unknown"),
                    "classification": config.get("classification", ""),
                    "shipping_default": bool(config.get("shipping_default", False)),
                    "available": False,
                    "reason": "disabled in mcp_config.yaml",
                })

        return {
            "ready": self._is_ready,
            "tool_count": len(tools),
            "tools": tools,
            "disabled_servers": disabled,
            "duplicate_tools": list(self._duplicate_tools),
            "invalid_tools": list(self._invalid_tools),
            "app_only_tools": sorted(self._app_tool_map),
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
                "classification": config.get("classification", ""),
                "shipping_default": bool(config.get("shipping_default", False)),
                "trust_boundary": config.get("trust_boundary", {}),
                "declared_tools": config.get("tools", []),
            }

            if not enabled:
                entry["status"] = "disabled"
                servers.append(entry)
                continue

            if server_type in {"docker_stdio", "stdio", "streamable_http", "http"}:
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
            "invalid_tools": list(self._invalid_tools),
            "servers": servers,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Disconnect all external servers and clear their tool catalogs."""
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
