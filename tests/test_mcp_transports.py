from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import uvicorn
import yaml
from starlette.responses import JSONResponse

from app.mcp.client import MCPClient
from app.mcp.registry import MCPRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_sdk_server.py"


def stdio_client(**kwargs):
    return MCPClient("fixture", command=sys.executable, args=[str(FIXTURE.resolve())], timeout=5, **kwargs)


def content(result):
    return result["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_sdk_stdio_cross_task_lifecycle_and_tool_errors(tmp_path):
    client = stdio_client(env={"FIXTURE_VALUE": "configured"}, cwd=str(tmp_path))
    assert await asyncio.create_task(client.connect()), client.get_status()
    pid = None
    try:
        result = await asyncio.create_task(client.call_tool("identity", {}))
        assert result["success"], result
        identity = json.loads(content(result))
        pid = identity["pid"]
        assert identity["cwd"] == str(tmp_path)
        assert identity["env"] == "configured"
        responses = await asyncio.gather(*(client.call_tool("echo", {"value": str(i)}) for i in range(4)))
        assert [content(r) for r in responses] == [str(i) for i in range(4)]
        failure = await client.call_tool("fail", {})
        assert failure["success"] is False
        assert "Error executing tool fail" in failure["error"]
        assert client.is_connected()
        assert "fixture started" in "\n".join(client.get_status()["stderr_tail"])
    finally:
        await asyncio.create_task(client.disconnect())
    assert not client.is_connected()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_sdk_tool_timeout_never_replays_mutation_and_next_call_reconnects(tmp_path):
    client = stdio_client()
    assert await client.connect(), client.get_status()
    marker = tmp_path / "mutations.txt"
    try:
        client.timeout = 0.1
        result = await client.call_tool("slow_mutation", {"marker": str(marker)})
        assert result["success"] is False
        assert marker.read_text() == "executed\n"
        client.timeout = 5
        assert content(await client.call_tool("echo", {"value": "reconnected"})) == "reconnected"
        assert marker.read_text() == "executed\n"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_sdk_cancellation_does_not_replay_and_leaves_client_usable(tmp_path):
    client = stdio_client()
    assert await client.connect(), client.get_status()
    marker = tmp_path / "mutations.txt"
    try:
        call = asyncio.create_task(client.call_tool("slow_mutation", {"marker": str(marker)}))
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert content(await client.call_tool("echo", {"value": "alive"})) == "alive"
        assert marker.read_text() == "executed\n"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_sdk_registry_refreshes_removed_and_added_tools(tmp_path):
    config = tmp_path / "mcp.yaml"
    config.write_text(yaml.safe_dump({"mcp_servers": {"fixture": {
        "type": "stdio", "command": sys.executable, "args": [str(FIXTURE.resolve())], "timeout": 5,
    }}}))
    registry = MCPRegistry()
    await registry.startup(config)
    try:
        assert registry.knows_tool("echo"), registry.get_diagnostics()
        assert await registry.call_tool("change_tools", {}) == "changed"
        async with asyncio.timeout(5):
            while not registry.knows_tool("added"):
                await asyncio.sleep(0.01)
        assert not registry.knows_tool("echo")
        assert await registry.call_tool("added", {}) == "new tool"
    finally:
        await registry.shutdown()
    assert not registry.knows_tool("added")


@pytest.mark.asyncio
async def test_sdk_connect_failure_is_clean():
    client = MCPClient("missing", command="/no/such/mcp-executable", timeout=1)
    assert not await client.connect()
    assert client.get_status()["connection_state"] == "error"
    await client.disconnect()
    assert not client.is_connected()


@pytest.mark.asyncio
async def test_sdk_bundled_shell_server_legacy_protocol(tmp_path):
    client = MCPClient("shell", command=sys.executable, args=[
        "-m", "mcp_shell_server.server", "--allowed-paths", str(tmp_path),
    ], timeout=5)
    assert await client.connect(), client.get_status()
    try:
        result = await client.call_tool("shell_exec", {
            "command": "pwd", "_fruitcake_user_context": {"user_id": 42},
        })
        assert result["success"], result
        assert json.loads(content(result))["stdout"].strip().endswith("/42")
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_legacy_http_companion_contract_and_rpc_errors():
    requests = []
    def handler(request):
        data = json.loads(request.content)
        requests.append((str(request.url), data["method"]))
        if data["method"] == "initialize":
            result = {"serverInfo": {"name": "companion"}}
        elif data["method"] == "tools/list":
            result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": data["id"], "error": {"message": "refused"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": data["id"], "result": result})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch("app.mcp.legacy_http.httpx.AsyncClient", return_value=http):
        client = MCPClient("companion", server_url="http://companion/")
        assert await client.connect()
        assert (await client.call_tool("echo", {}))["success"] is False
        await client.disconnect()
    assert requests == [("http://companion/", method) for method in ["initialize", "tools/list", "tools/call"]]
    assert http.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("json_response", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
async def test_sdk_streamable_http_endpoint_auth_sessions_and_cleanup(json_response, legacy):
    spec = importlib.util.spec_from_file_location("mcp_fixture", FIXTURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.make_server()
    mcp_app = server.streamable_http_app(json_response=json_response)
    requests = []

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await mcp_app(scope, receive, send)
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        headers = dict(scope["headers"])
        method = json.loads(body).get("method") if body else scope["method"]
        requests.append((method, headers))
        if headers.get(b"authorization") != b"Bearer fixture-token":
            return await JSONResponse({}, status_code=401)(scope, receive, send)
        # Exercise a handshake-era, session-based server like Elgato.
        if legacy and method == "server/discover":
            return await JSONResponse({"jsonrpc": "2.0", "id": json.loads(body)["id"],
                                       "error": {"code": -32601, "message": "not supported"}})(scope, receive, send)
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body}
            return await receive()
        await mcp_app(scope, replay, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    http_server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    serving = asyncio.create_task(http_server.serve(sockets=[sock]))
    client = MCPClient("http", server_url=f"http://127.0.0.1:{port}/mcp", transport="streamable_http",
                       headers={"Authorization": "Bearer fixture-token"}, timeout=5)
    try:
        async with asyncio.timeout(5):
            while not http_server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        assert await client.connect(), client.get_status()
        assert content(await client.call_tool("echo", {"value": "over HTTP"})) == "over HTTP"
        if not legacy:
            assert (await client.call_tool("change_tools", {}))["success"]
            async with asyncio.timeout(5):
                while "added" not in [tool["name"] for tool in client.get_tools()]:
                    await asyncio.sleep(0.01)
        await asyncio.create_task(client.disconnect())
        tool_headers = next(headers for method, headers in requests if method == "tools/call")
        if legacy:
            assert tool_headers.get(b"mcp-session-id")
            assert any(method == "DELETE" for method, _ in requests)
    finally:
        await client.disconnect()
        http_server.should_exit = True
        await asyncio.wait_for(serving, 5)
        sock.close()


@pytest.mark.asyncio
async def test_legacy_sdk_pagination_and_list_change_notifications():
    client = MCPClient("legacy-peer", command=sys.executable,
                       args=[str(FIXTURE.with_name("mcp_legacy_peer.py").resolve())], timeout=5)
    assert await client.connect(), client.get_status()
    try:
        assert [tool["name"] for tool in client.get_tools()] == ["first", "second"]
        assert (await client.call_tool("first", {}))["success"]
        async with asyncio.timeout(5):
            while client.get_tools()[0]["name"] != "replacement":
                await asyncio.sleep(0.01)
        assert [tool["name"] for tool in client.get_tools()] == ["replacement", "second"]
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_cancelled_connection_reaps_starting_process(tmp_path):
    marker = tmp_path / "pid"
    client = MCPClient("hang", command=sys.executable, args=[
        "-c", "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",
        str(marker),
    ], timeout=5)
    connecting = asyncio.create_task(client.connect())
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        pid = int(marker.read_text())
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert not client.is_connected()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_stderr_redacts_secrets_split_across_reads():
    client = stdio_client(env={"TOKEN": "synthetic-secret-value"})
    reader = asyncio.StreamReader()
    task = asyncio.create_task(client._capture_stderr(reader))
    reader.feed_data(b"token=synthetic-secret-")
    await asyncio.sleep(0)
    reader.feed_data(b"value\n" + b"x" * 5000 + b"\n")
    reader.feed_eof()
    await task
    assert list(client._stderr_ring) == ["token=[REDACTED]", "[oversized stderr line omitted]"]


def test_registry_live_catalog_preserves_config_order_and_promotes_duplicates():
    registry = MCPRegistry()
    registry._server_configs = {"first": {}, "second": {}}
    tool = {"name": "shared", "inputSchema": {"type": "object"}}
    registry._set_server_tools("second", "stdio", [tool])
    registry._set_server_tools("first", "stdio", [tool])
    assert registry._tool_map["shared"] == ("first", "stdio")
    assert len(registry.get_status()["duplicate_tools"]) == 1
    registry._set_server_tools("first", "stdio", [])
    assert registry._tool_map["shared"] == ("second", "stdio")
    assert registry.get_status()["duplicate_tools"] == []


@pytest.mark.asyncio
async def test_registry_passes_http_credentials_from_environment(monkeypatch):
    monkeypatch.setenv("TEST_MCP_AUTH", "Bearer synthetic-credential")
    registry = MCPRegistry()
    client = AsyncMock()
    client.get_tools = lambda: []
    with patch("app.mcp.registry.MCPClient", return_value=client) as factory:
        await registry._init_external("remote", {
            "url": "https://example.invalid/mcp", "headers_from_env": {"Authorization": "TEST_MCP_AUTH"},
        }, "streamable_http")
    assert factory.call_args.kwargs["headers"] == {"Authorization": "Bearer synthetic-credential"}
    assert factory.call_args.kwargs["transport"] == "streamable_http"
    assert factory.call_args.kwargs["server_url"].endswith("/mcp")


def test_registry_missing_credentials_fail_closed(monkeypatch):
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Missing environment variable"):
        MCPRegistry._config_values({"env_from": {"API_TOKEN": "MISSING_MCP_TOKEN"}}, "env")
