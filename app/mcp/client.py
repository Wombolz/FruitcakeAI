"""Fruitcake MCP facade: official SDK transports and legacy companion HTTP."""
from __future__ import annotations

import asyncio
import os
from collections import deque
from contextlib import AsyncExitStack
from typing import Any, Callable

import httpx2
from mcp import Client
from mcp.client import advertise
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import ToolsListChanged
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from mcp.shared.exceptions import MCPError

from app.mcp.legacy_http import LegacyHTTPClient

MCP_APPS_EXTENSION_ID = "io.modelcontextprotocol/ui"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"


class MCPClient:
    """SDK contexts belong to one task; callers may use any asyncio task.

    Failed calls are never replayed: a server may already have executed a
    mutation. Only a later independent call may establish a fresh connection.
    """
    def __init__(
        self, server_name: str, server_url: str | None = None,
        command: str | None = None, args: list[str] | None = None,
        timeout: float = 30, *, transport: str | None = None,
        env: dict[str, str] | None = None, cwd: str | None = None,
        headers: dict[str, str] | None = None,
        on_tools_changed: Callable[[list[dict[str, Any]]], None] | None = None,
    ):
        self.server_name, self.server_url = server_name, server_url
        self.command, self.args, self.timeout = command, args or [], timeout
        self.transport_type = transport or ("stdio" if command else "http")
        self.env, self.cwd, self.headers = env or {}, cwd, headers or {}
        self._on_tools_changed = on_tools_changed
        self._connected = False
        self._connection_state = "disconnected"
        self._last_error: str | None = None
        self._tools: list[dict[str, Any]] = []
        self._server_info: dict[str, Any] = {}
        self._sdk: Client | None = None
        self._legacy: LegacyHTTPClient | None = None
        self._owner: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._refresh_pending = False
        self._lifecycle_lock = asyncio.Lock()
        self._stderr_ring: deque[str] = deque(maxlen=200)

    def _error_text(self, exc: BaseException) -> str:
        text = str(exc) or type(exc).__name__
        for value in [*self.headers.values(), *self.env.values()]:
            if value:
                text = text.replace(value, "[REDACTED]")
        return text[:500]

    def _publish_tools(self, tools: list[dict[str, Any]]) -> None:
        self._tools = tools
        if self._on_tools_changed is not None:
            self._on_tools_changed(list(tools))

    async def _message_handler(self, message: Any) -> None:
        if isinstance(message, Exception):
            self._last_error = self._error_text(message)
            self._connected = False
            self._stopping = True
            self._wake.set()
        elif getattr(message, "method", None) == "notifications/tools/list_changed":
            # Fetch outside the receive callback so responses can be delivered.
            self._refresh_pending = True
            self._wake.set()

    async def _discover_tools(self) -> None:
        if self._legacy is not None:
            result = await self._legacy.request("tools/list", {})
            self._publish_tools(result.get("tools", []))
            return
        assert self._sdk is not None
        tools: list[dict[str, Any]] = []
        cursor = None
        seen: set[str] = set()
        while True:
            page = await self._sdk.list_tools(cursor=cursor, cache_mode="bypass")
            tools.extend(tool.model_dump(by_alias=True, exclude_none=True) for tool in page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
            if cursor in seen:
                raise RuntimeError("MCP tools/list repeated a pagination cursor")
            seen.add(cursor)
        self._publish_tools(tools)

    async def _watch_changes(self, subscription: Any) -> None:
        try:
            async for event in subscription:
                if isinstance(event, ToolsListChanged):
                    self._refresh_pending = True
                    self._wake.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._message_handler(exc)

    async def _capture_stderr(self, reader: asyncio.StreamReader) -> None:
        pending = b""
        discarding = False
        while data := await reader.read(4096):
            parts = data.split(b"\n")
            for index, part in enumerate(parts):
                if not discarding:
                    pending += part
                    if len(pending) > 4096:
                        pending = b""
                        discarding = True
                if index < len(parts) - 1:
                    text = "[oversized stderr line omitted]" if discarding else pending.decode(errors="replace")
                    self._stderr_ring.append(self._error_text(RuntimeError(text)))
                    pending, discarding = b"", False
        if pending and not discarding:
            self._stderr_ring.append(self._error_text(RuntimeError(pending.decode(errors="replace"))))

    async def _run(self) -> None:
        """Enter and exit every SDK/AnyIO context in this same task."""
        try:
            async with AsyncExitStack() as stack:
                if self.transport_type == "http":
                    self._legacy = LegacyHTTPClient(self.server_url, self.timeout, self.headers)
                    stack.push_async_callback(self._legacy.disconnect)
                    self._server_info = await self._legacy.connect()
                else:
                    if self.transport_type == "stdio":
                        # The SDK needs a real fd. Capture a bounded diagnostic
                        # tail rather than allowing an unbounded stderr file.
                        read_fd, write_fd = os.pipe()
                        read_file = stack.enter_context(os.fdopen(read_fd, "rb"))
                        write_file = stack.enter_context(os.fdopen(write_fd, "w"))
                        reader = asyncio.StreamReader()
                        pipe, _ = await asyncio.get_running_loop().connect_read_pipe(
                            lambda: asyncio.StreamReaderProtocol(reader), read_file,
                        )
                        stack.callback(pipe.close)
                        stderr_task = asyncio.create_task(self._capture_stderr(reader))
                        stack.push_async_callback(self._cancel_task, stderr_task)
                        transport = stdio_client(StdioServerParameters(
                            command=self.command, args=self.args, env=self.env, cwd=self.cwd,
                        ), errlog=write_file)
                    elif self.transport_type == "streamable_http":
                        http = await stack.enter_async_context(httpx2.AsyncClient(
                            headers=self.headers, timeout=httpx2.Timeout(self.timeout),
                        ))
                        transport = streamable_http_client(self.server_url, http_client=http)
                    else:
                        raise ValueError(f"Unsupported MCP transport: {self.transport_type}")
                    self._sdk = await stack.enter_async_context(Client(
                        transport, read_timeout_seconds=self.timeout,
                        message_handler=self._message_handler, cache=None,
                        extensions=[advertise(
                            MCP_APPS_EXTENSION_ID,
                            {"mimeTypes": [MCP_APP_MIME_TYPE]},
                        )],
                    ))
                    if self._sdk.server_info is not None:
                        self._server_info = self._sdk.server_info.model_dump(by_alias=True)
                    if self._sdk.protocol_version in MODERN_PROTOCOL_VERSIONS:
                        try:
                            subscription = await stack.enter_async_context(self._sdk.listen(tools_list_changed=True))
                        except MCPError as exc:
                            if exc.code != -32601:  # subscriptions are optional
                                raise
                        else:
                            watcher = asyncio.create_task(self._watch_changes(subscription))
                            stack.push_async_callback(self._cancel_task, watcher)
                await self._discover_tools()
                self._connected = True
                self._connection_state = "connected"
                self._last_error = None
                self._ready.set_result(True)
                while not self._stopping:
                    await self._wake.wait()
                    self._wake.clear()
                    if self._refresh_pending and not self._stopping:
                        self._refresh_pending = False
                        await self._discover_tools()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = self._error_text(exc)
        finally:
            self._connected = False
            self._sdk = None
            self._legacy = None
            if self._last_error is None:
                self._publish_tools([])
            self._connection_state = "error" if self._last_error else "disconnected"
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(False)

    @staticmethod
    async def _cancel_task(task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _stop_owner(self) -> None:
        if self._owner is not None:
            self._stopping = True
            self._wake.set()
            self._owner.cancel()
            try:
                await asyncio.shield(self._owner)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            self._owner = None

    async def connect(self) -> bool:
        async with self._lifecycle_lock:
            if self.is_connected():
                return True
            await self._stop_owner()
            self._stopping = False
            self._wake.clear()
            self._refresh_pending = False
            self._last_error = None
            self._connection_state = "connecting"
            self._ready = asyncio.get_running_loop().create_future()
            self._owner = asyncio.create_task(self._run(), name=f"mcp:{self.server_name}")
            try:
                return await asyncio.wait_for(asyncio.shield(self._ready), self.timeout)
            except BaseException as exc:
                self._last_error = self._error_text(exc)
                self._owner.cancel()
                try:
                    await self._owner
                except asyncio.CancelledError:
                    pass
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return False

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.is_connected() and not await self.connect():
            return {"success": False, "error": self._last_error or "MCP server unavailable"}
        try:
            if self._legacy is not None:
                result = await self._legacy.request("tools/call", {"name": tool_name, "arguments": arguments})
            else:
                assert self._sdk is not None
                response = await self._sdk.call_tool(tool_name, arguments, read_timeout_seconds=self.timeout)
                result = response.model_dump(by_alias=True, exclude_none=True)
            if result.get("isError"):
                error = "\n".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
                return {"success": False, "result": result, "error": error or "MCP tool reported an error"}
            return {"success": True, "result": result}
        except asyncio.CancelledError:
            raise  # The SDK cancels the request; never resubmit it.
        except Exception as exc:
            self._last_error = self._error_text(exc)
            self._connected = False
            self._stopping = True
            self._wake.set()
            return {"success": False, "error": self._last_error}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read a resource from the connected server without exposing auth to a UI."""
        if not self.is_connected() and not await self.connect():
            return {"success": False, "error": self._last_error or "MCP server unavailable"}
        try:
            if self._legacy is not None:
                result = await self._legacy.request("resources/read", {"uri": uri})
            else:
                assert self._sdk is not None
                response = await self._sdk.read_resource(uri, cache_mode="bypass")
                result = response.model_dump(by_alias=True, exclude_none=True)
            return {"success": True, "result": result}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = self._error_text(exc)
            return {"success": False, "error": self._last_error}

    async def disconnect(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_owner()
            self._last_error = None
            self._publish_tools([])
            self._connection_state = "disconnected"

    def is_connected(self) -> bool:
        return self._connected and self._owner is not None and not self._owner.done()

    def get_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    def get_status(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name, "transport": self.transport_type,
            "connected": self.is_connected(), "connection_state": self._connection_state,
            "tools": [tool["name"] for tool in self._tools], "last_error": self._last_error,
            "stderr_tail": list(self._stderr_ring),
        }
