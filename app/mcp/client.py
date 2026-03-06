"""
FruitcakeAI v5 — MCP Client
Ported from v4 (production-proven). Supports SSE (HTTP) and stdio (subprocess)
transports using the JSON-RPC 2.0 MCP wire protocol.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx
import structlog

log = structlog.get_logger(__name__)


class MCPClient:
    """
    Client for a single MCP server.

    Supports two transports:
    - stdio: spawns a subprocess (e.g. `docker run -i --rm <image>`)
    - sse:   connects to an HTTP MCP server
    """

    def __init__(
        self,
        server_name: str,
        server_url: Optional[str] = None,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        timeout: int = 30,
    ):
        self.server_name = server_name
        self.server_url = server_url.rstrip("/") if server_url else None
        self.command = command
        self.args = args or []
        self.timeout = timeout
        self.transport_type = "stdio" if command else "sse"

        self._client: Optional[httpx.AsyncClient] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._connected = False
        self._tools: List[Dict[str, Any]] = []
        self._server_info: Dict[str, Any] = {}
        self._last_error: Optional[str] = None
        self._request_id = 0

    # ── Connection ────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def connect(self) -> bool:
        if self.transport_type == "stdio":
            return await self._connect_stdio()
        return await self._connect_sse()

    async def _connect_sse(self) -> bool:
        try:
            self._client = httpx.AsyncClient(
                base_url=self.server_url,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
            response = await self._client.post(
                "/initialize",
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "clientInfo": {"name": "FruitcakeAI", "version": "5.0"},
                    },
                },
            )
            if response.status_code == 200:
                result = response.json()
                self._server_info = result.get("result", result)
                self._connected = True
                await self._discover_tools()
                log.info("MCP server connected", server=self.server_name, transport="sse")
                return True
            self._last_error = f"HTTP {response.status_code}"
            return False
        except Exception as e:
            self._last_error = str(e)
            log.warning("MCP server connect failed", server=self.server_name, error=str(e))
            return False

    async def _connect_stdio(self) -> bool:
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            request_id = self._next_id()
            await self._write({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "FruitcakeAI", "version": "5.0"},
                },
            })
            response = await self._read_response(request_id=request_id, timeout=float(self.timeout))
            if response and "result" in response:
                self._server_info = response["result"]
                # Required MCP protocol handshake
                await self._write({
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                })
                self._connected = True
                await self._discover_tools()
                log.info("MCP server connected", server=self.server_name, transport="stdio")
                return True
            self._last_error = "Invalid initialize response"
            return False
        except Exception as e:
            self._last_error = str(e)
            log.warning("MCP server connect failed", server=self.server_name, error=str(e))
            return False

    # ── stdio I/O ─────────────────────────────────────────────────────────────

    async def _write(self, message: Dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("No stdio process")
        self._process.stdin.write((json.dumps(message) + "\n").encode())
        await self._process.stdin.drain()

    async def _read(self) -> Optional[Dict[str, Any]]:
        if not self._process or not self._process.stdout:
            raise RuntimeError("No stdio process")
        line = await self._process.stdout.readline()
        if not line:
            return None

        first = line.decode(errors="replace").strip()
        if not first:
            return None

        # MCP stdio commonly uses Content-Length framing.
        if first.lower().startswith("content-length:"):
            try:
                length = int(first.split(":", 1)[1].strip())
            except Exception:
                return None

            # Consume remaining headers until blank line.
            while True:
                header_line = await self._process.stdout.readline()
                if not header_line:
                    return None
                if header_line in (b"\n", b"\r\n"):
                    break

            try:
                payload = await self._process.stdout.readexactly(length)
                return json.loads(payload.decode(errors="replace"))
            except Exception:
                return None

        # Line-delimited JSON (legacy/simple servers).
        try:
            return json.loads(first)
        except json.JSONDecodeError:
            pass

        # Some servers pretty-print JSON over multiple lines.
        buffer = first
        for _ in range(64):
            next_line = await self._process.stdout.readline()
            if not next_line:
                break
            buffer += "\n" + next_line.decode(errors="replace").rstrip("\r\n")
            try:
                return json.loads(buffer)
            except json.JSONDecodeError:
                continue
        return None

    async def _read_response(
        self,
        *,
        request_id: Optional[int],
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Read stdio frames until the matching JSON-RPC response id arrives.

        Notifications/out-of-band frames are ignored.
        """
        deadline: Optional[float] = None
        if timeout is not None:
            deadline = asyncio.get_running_loop().time() + timeout

        while True:
            remaining: Optional[float] = None
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError

            msg = await asyncio.wait_for(self._read(), timeout=remaining)
            if msg is None:
                return None
            if request_id is not None and msg.get("id") != request_id:
                continue
            return msg

    # ── Tool discovery ────────────────────────────────────────────────────────

    async def _discover_tools(self) -> None:
        try:
            if self.transport_type == "stdio":
                request_id = self._next_id()
                await self._write({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/list",
                    "params": {},
                })
                response = await self._read_response(request_id=request_id, timeout=float(self.timeout))
                if response and "result" in response:
                    self._tools = response["result"].get("tools", [])
            else:
                if not self._client:
                    return
                response = await self._client.post(
                    "/tools/list",
                    json={
                        "jsonrpc": "2.0",
                        "id": self._next_id(),
                        "method": "tools/list",
                        "params": {},
                    },
                )
                if response.status_code == 200:
                    result = response.json()
                    self._tools = result.get("result", result).get("tools", [])

            log.info(
                "MCP tools discovered",
                server=self.server_name,
                tools=[t.get("name") for t in self._tools],
            )
        except Exception as e:
            log.warning("MCP tool discovery failed", server=self.server_name, error=str(e))

    # ── Tool execution ────────────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a tool. Returns {"success": bool, "result": ..., "error": ...}."""
        if self.transport_type == "stdio":
            return await self._call_stdio(tool_name, arguments)
        return await self._call_sse(tool_name, arguments)

    async def _call_stdio(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            if not self._connected or not self._process:
                raise RuntimeError(f"Not connected to {self.server_name}")
            request_id = self._next_id()
            await self._write({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            response = await self._read_response(request_id=request_id, timeout=float(self.timeout))
            if response and "result" in response:
                return {"success": True, "result": response["result"]}
            if response and "error" in response:
                return {"success": False, "error": response["error"].get("message", "Unknown error")}
            return {"success": False, "error": "No response from server"}
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timed out waiting for server response ({self.timeout}s)"}
        except Exception as e:
            log.error("MCP stdio tool call failed", server=self.server_name, tool=tool_name, error=str(e))
            return {"success": False, "error": str(e)}

    async def _call_sse(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            if not self._connected or not self._client:
                raise RuntimeError(f"Not connected to {self.server_name}")
            response = await self._client.post(
                "/tools/call",
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
            )
            if response.status_code == 200:
                result = response.json()
                return {"success": True, "result": result.get("result", result)}
            return {"success": False, "error": f"HTTP {response.status_code}"}
        except Exception as e:
            log.error("MCP SSE tool call failed", server=self.server_name, tool=tool_name, error=str(e))
            return {"success": False, "error": str(e)}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def disconnect(self) -> None:
        self._connected = False
        self._tools = []
        if self.transport_type == "stdio" and self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                try:
                    self._process.kill()
                    await self._process.wait()
                except Exception:
                    pass
            finally:
                self._process = None
        elif self._client:
            await self._client.aclose()
            self._client = None
        log.info("MCP server disconnected", server=self.server_name)

    # ── Status ────────────────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        return self._connected

    def get_tools(self) -> List[Dict[str, Any]]:
        return list(self._tools)

    def get_status(self) -> Dict[str, Any]:
        return {
            "server_name": self.server_name,
            "transport": self.transport_type,
            "connected": self._connected,
            "tools": [t.get("name") for t in self._tools],
            "last_error": self._last_error,
        }
