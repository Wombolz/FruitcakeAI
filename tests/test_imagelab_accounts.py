"""Account-bound discovery, model calls and MCP Apps must never cross users."""
from __future__ import annotations

import asyncio
import json
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import uvicorn
import yaml
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from app.agent.context import UserContext
from app.config import Settings, settings
from app.mcp.client import MCPClient
from app.mcp.imagelab_accounts import IMAGELAB_SERVER, context_user_id, parse_user_tokens
from app.mcp.registry import MCPAppApprovalRequired, MCPRegistry

URI = "ui://imagelab/dashboard.html"


def tool(name, account, *, app=False):
    return {
        "name": name, "description": account,
        "inputSchema": {"type": "object", "properties": {}},
        "_meta": {"ui": {"resourceUri": URI, "visibility": ["app"] if app else ["model", "app"]}},
        "annotations": {"readOnlyHint": False},
    }


class AccountPeer:
    instances = []

    def __init__(self, *, headers, on_tools_changed, server_name, **kwargs):
        self.headers = headers
        self.callback = on_tools_changed
        self.server_name = server_name
        self.transport = kwargs["transport"]
        self.account = headers.get("Authorization", "Shared").removeprefix("Bearer ")
        self.calls = []
        self.disconnections = 0
        self.revoked = self.account == "invalid"
        self.tools = [tool("generate", self.account), tool("app_generate", self.account, app=True)]
        if self.account != "Shared":
            self.tools.append(tool(f"private_{self.account}", self.account))
        self.instances.append(self)

    async def connect(self):
        if self.revoked:
            return False
        self.callback(self.tools)
        return True

    def get_tools(self):
        return self.tools

    def get_status(self):
        return {"connected": not self.revoked, "last_error": "401 Unauthorized" if self.revoked else None}

    async def call_tool(self, name, arguments):
        await asyncio.sleep(0)
        self.calls.append((name, arguments))
        if self.revoked:
            return {"success": False, "error": "401 Unauthorized"}
        return {"success": True, "result": {"content": [{"type": "text", "text": self.account}]}}

    async def read_resource(self, uri):
        if self.revoked:
            return {"success": False, "error": "401 Unauthorized"}
        return {"success": True, "result": {"contents": [{
            "uri": uri, "mimeType": "text/html;profile=mcp-app", "text": self.account,
        }]}}

    async def disconnect(self):
        self.disconnections += 1
        self.callback([])


@pytest.fixture
async def accounts(tmp_path, monkeypatch):
    AccountPeer.instances = []
    monkeypatch.setattr(settings, "imagelab_user_tokens_json", SecretStr('{"1":"alice","2":"bob","3":"invalid"}'))
    config = tmp_path / "mcp.yaml"
    config.write_text(yaml.safe_dump({"mcp_servers": {IMAGELAB_SERVER: {
        "type": "streamable_http", "url": "http://127.0.0.1:47835/mcp",
        "headers": {"aUtHoRiZaTiOn": "Bearer deployment-secret", "X-Client": "Fruitcake"},
        "headers_from_env": {"AUTHORIZATION": "IGNORED_MISSING_AUTH_VARIABLE"},
        "trust_boundary": {"first_party": True},
    }}}, sort_keys=False))
    registry = MCPRegistry()
    with patch("app.mcp.registry.MCPClient", AccountPeer):
        await registry.startup(config)
        yield registry
        await registry.shutdown()


def names(registry, user=None):
    return {entry["function"]["name"] for entry in registry.get_tools_for_agent(user)}


def test_valid_configuration_is_secret_and_default_empty(monkeypatch):
    monkeypatch.delenv("IMAGELAB_USER_TOKENS_JSON", raising=False)
    assert parse_user_tokens(Settings(_env_file=None).imagelab_user_tokens_json) == {}
    tokens = parse_user_tokens(SecretStr('{"12":"test-token_+/=="}'))
    assert tokens[12].get_secret_value() == "test-token_+/=="
    assert "test-token" not in repr(tokens)
    configured = Settings(_env_file=None, imagelab_user_tokens_json='{"12":"secret-marker"}')
    assert "secret-marker" not in repr(configured)


def test_dotenv_preserves_single_quoted_json(tmp_path, monkeypatch):
    monkeypatch.delenv("IMAGELAB_USER_TOKENS_JSON", raising=False)
    env = tmp_path / "tokens.env"
    env.write_text("IMAGELAB_USER_TOKENS_JSON='{\"1\":\"fixture-token\"}'\n")
    configured = Settings(_env_file=env)
    assert parse_user_tokens(configured.imagelab_user_tokens_json)[1].get_secret_value() == "fixture-token"


@pytest.mark.parametrize("raw", [
    "", "invalid-secret-marker", "[]", "null", '{"1":"a","1":"b"}',
    '{"0":"x"}', '{"-1":"x"}', '{"01":"x"}', '{"abc":"x"}',
    '{"9223372036854775808":"x"}', '{"1":42}', '{"1":null}',
    '{"1":""}', '{"1":"Bearer token"}', '{"1":"secret\\nmarker"}',
    json.dumps({"1": "x" * 8193}), " " * 262_145,
    json.dumps({str(i): "token" for i in range(1, 258)}),
])
def test_invalid_configuration_rejected_without_secret_echo(raw):
    with pytest.raises(ValueError) as error:
        parse_user_tokens(SecretStr(raw))
    assert str(error.value).startswith("IMAGELAB_USER_TOKENS_JSON must map")
    assert "secret-marker" not in str(error.value)


@pytest.mark.parametrize("context, expected", [
    (SimpleNamespace(user_id=1), 1), (SimpleNamespace(id=2), 2),
    ({"user_id": 3}, 3), ({"id": 1}, None), ({"user_id": "1"}, None),
    ({"user_id": True}, None), ({"user_id": -1}, None), (None, None),
])
def test_only_authenticated_integer_id_selects_credentials(context, expected):
    assert context_user_id(context) == expected


@pytest.mark.asyncio
async def test_concurrent_users_are_isolated_and_arguments_cannot_select_account(accounts):
    users = [SimpleNamespace(user_id=1), SimpleNamespace(id=2), SimpleNamespace(user_id=99), None]
    results = await asyncio.gather(*(
        accounts.call_tool("generate", {"user_id": 2, "account": "bob"}, user) for user in users
    ))
    assert list(map(str, results)) == ["alice", "bob", "Shared", "Shared"]
    shared, alice, bob, invalid = AccountPeer.instances
    assert shared.headers == {"X-Client": "Fruitcake"}
    assert alice.headers["Authorization"] == "Bearer alice"
    assert bob.headers["Authorization"] == "Bearer bob"
    assert all(peer.transport == "streamable_http" for peer in AccountPeer.instances)
    assert invalid.calls == []
    assert "deployment-secret" not in repr(accounts._raw_config)
    assert "Bearer alice" not in repr(accounts._account_views[1]._server_configs)


@pytest.mark.asyncio
async def test_model_and_app_catalogs_are_account_local(accounts):
    assert names(accounts) == {"generate"}
    assert names(accounts, {"user_id": 1}) == {"generate", "private_alice"}
    assert names(accounts, {"user_id": 2}) == {"generate", "private_bob"}
    assert names(accounts, {"user_id": 3}) == set()
    assert accounts.knows_tool("private_alice", {"user_id": 1})
    assert not accounts.knows_tool("private_alice", {"user_id": 2})
    assert not accounts.knows_tool("app_generate", {"user_id": 1})
    assert "Unknown MCP tool" in await accounts.call_tool("private_alice", {}, {"user_id": 2})


@pytest.mark.asyncio
async def test_user_tool_surface_and_dispatch_include_private_tools(accounts):
    from app.agent.tools import _call_tool, get_tools_for_user

    user = UserContext(user_id=1, username="alice", role="parent")
    with patch("app.mcp.registry.get_mcp_registry", return_value=accounts):
        assert "private_alice" in {tool["function"]["name"] for tool in get_tools_for_user(user)}
        assert "private_bob" not in {tool["function"]["name"] for tool in get_tools_for_user(user)}
        assert await _call_tool("private_alice", {}, user) == "alice"


@pytest.mark.asyncio
async def test_dashboard_reads_and_approved_mutations_use_same_account(accounts):
    alice = SimpleNamespace(id=1)
    assert (await accounts.read_mcp_app_resource(IMAGELAB_SERVER, URI, alice))["html"] == "alice"
    assert (await accounts.read_mcp_app_resource(IMAGELAB_SERVER, URI))["html"] == "Shared"
    arguments = dict(server_name=IMAGELAB_SERVER, resource_uri=URI, tool_name="app_generate", arguments={})
    with pytest.raises(MCPAppApprovalRequired):
        await accounts.call_mcp_app_tool(**arguments, user_context=alice)
    result = await accounts.call_mcp_app_tool(**arguments, user_context=alice, approved=True)
    assert result["result"]["content"][0]["text"] == "alice"
    assert AccountPeer.instances[0].calls == []


@pytest.mark.asyncio
async def test_invalid_or_revoked_token_never_falls_back_to_shared(accounts):
    shared, alice, _, invalid = AccountPeer.instances
    assert "Unknown MCP tool" in await accounts.call_tool("generate", {}, {"user_id": 3})
    with pytest.raises(LookupError):
        await accounts.read_mcp_app_resource(IMAGELAB_SERVER, URI, {"user_id": 3})
    alice.revoked = True
    assert "401" in await accounts.call_tool("generate", {}, {"user_id": 1})
    with pytest.raises(RuntimeError, match="401"):
        await accounts.read_mcp_app_resource(IMAGELAB_SERVER, URI, {"user_id": 1})
    with pytest.raises(RuntimeError, match="401"):
        await accounts.call_mcp_app_tool(
            server_name=IMAGELAB_SERVER, resource_uri=URI, tool_name="app_generate",
            arguments={}, user_context={"user_id": 1}, approved=True,
        )
    assert shared.calls == invalid.calls == []


@pytest.mark.asyncio
async def test_live_catalog_changes_do_not_cross_accounts(accounts):
    shared, alice, bob, _ = AccountPeer.instances
    alice.callback([tool("alice_new", "alice")])
    assert names(accounts, {"user_id": 1}) == {"alice_new"}
    assert names(accounts) == {"generate"}
    assert names(accounts, {"user_id": 2}) == {"generate", "private_bob"}
    shared.callback([tool("shared_new", "Shared")])
    assert names(accounts) == {"shared_new"}
    assert names(accounts, {"user_id": 1}) == {"alice_new"}
    accounts._set_server_tools("other", "http", [{"name": "ordinary", "inputSchema": {"type": "object"}}])
    assert all("ordinary" in names(accounts, {"user_id": uid}) for uid in (1, 2, 3, 99))
    accounts._set_server_tools("other", "http", [])
    assert all("ordinary" not in names(accounts, {"user_id": uid}) for uid in (1, 2, 3, 99))


@pytest.mark.asyncio
async def test_shutdown_only_disconnects_each_owned_client_once(accounts):
    other = SimpleNamespace(disconnect=AsyncMock())
    accounts._clients["other"] = other
    for view in accounts._account_views.values():
        view._clients["other"] = other
    await accounts.shutdown()
    other.disconnect.assert_awaited_once()
    assert all(peer.disconnections == 1 for peer in AccountPeer.instances)
    assert accounts._account_views == {}
    assert not accounts._is_ready


@pytest.mark.asyncio
async def test_malformed_configuration_fails_before_any_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "imagelab_user_tokens_json", SecretStr('{"1":""}'))
    with patch("app.mcp.registry.MCPClient") as client:
        with pytest.raises(ValueError, match="IMAGELAB_USER_TOKENS_JSON"):
            await MCPRegistry().startup(tmp_path / "absent.yaml")
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_account_tokens_require_http_unless_server_disabled(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(settings, "imagelab_user_tokens_json", SecretStr('{"1":"fixture-token"}'))
    config = tmp_path / "mcp.yaml"
    config.write_text(yaml.safe_dump({"mcp_servers": {IMAGELAB_SERVER: {
        "type": "stdio", "command": "unused", "enabled": enabled,
    }}}))
    registry = MCPRegistry()
    with patch("app.mcp.registry.MCPClient") as client:
        if enabled:
            with pytest.raises(ValueError, match="require an HTTP MCP transport"):
                await registry.startup(config)
        else:
            await registry.startup(config)
            assert names(registry, {"user_id": 1}) == set()
        client.assert_not_called()
    await registry.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_reflected_bearer_credentials_redacted_in_results_resources_and_errors(is_error):
    token = "private-credential-marker"
    client = MCPClient("image", headers={"Authorization": f"Bearer {token}"})
    client.is_connected = lambda: True
    payload = {"content": [{"type": "text", "text": f"Bearer {token}; bare {token}"}], "isError": is_error}
    client._legacy = SimpleNamespace(request=AsyncMock(return_value=payload))
    assert token not in repr(await client.call_tool("generate", {}))
    assert token not in repr(await client.read_resource(URI))
    client._publish_tools([tool("generate", token)])
    assert token not in repr(client.get_tools())
    client._legacy.request.side_effect = RuntimeError(f"denied {token}")
    assert token not in repr(await client.call_tool("generate", {}))
    assert token not in repr(client.get_status())


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "streamable_http"])
async def test_real_http_headers_isolate_discovery_calls_resources_and_revocation(tmp_path, monkeypatch, transport):
    """Exercise actual MCPClient/HTTP sessions against a local authenticated peer."""
    from mcp.server import MCPServer

    peers = {}
    requests = []
    revoked = set()

    def make_peer(account):
        server = MCPServer(f"imagelab-{account}")

        async def identity() -> str:
            return account

        server.add_tool(identity, name=f"identity_{account}")
        return server.streamable_http_app(json_response=True)

    if transport == "streamable_http":
        peers = {account: make_peer(account) for account in ("Shared", "alice", "bob")}

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for peer in peers.values():
                await stack.enter_async_context(peer.router.lifespan_context(peer))
            yield

    lifecycle = Starlette(lifespan=lifespan)

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await lifecycle(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization")
        account = {None: "Shared", b"Bearer fixture-token-a": "alice", b"Bearer fixture-token-b": "bob"}.get(auth)
        requests.append((account, auth))
        if account is None or account in revoked:
            return await JSONResponse({}, status_code=401)(scope, receive, send)
        if transport == "streamable_http":
            return await peers[account](scope, receive, send)
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        data = json.loads(body)
        if data["method"] == "initialize":
            result = {"serverInfo": {"name": account}}
        elif data["method"] == "tools/list":
            result = {"tools": [tool(f"identity_{account}", account)]}
        elif data["method"] == "resources/read":
            result = {"contents": [{"uri": URI, "mimeType": "text/html;profile=mcp-app", "text": account}]}
        else:
            result = {"content": [{"type": "text", "text": account}]}
        return await JSONResponse({"jsonrpc": "2.0", "id": data["id"], "result": result})(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    http_server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    serving = asyncio.create_task(http_server.serve(sockets=[sock]))
    monkeypatch.setattr(settings, "imagelab_user_tokens_json", SecretStr('{"1":"fixture-token-a","2":"fixture-token-b"}'))
    config = tmp_path / "network-mcp.yaml"
    config.write_text(yaml.safe_dump({"mcp_servers": {IMAGELAB_SERVER: {
        "type": transport, "url": f"http://127.0.0.1:{port}/mcp", "timeout": 3,
    }}}))
    registry = MCPRegistry()
    try:
        async with asyncio.timeout(5):
            while not http_server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        await registry.startup(config)
        assert names(registry) == {"identity_Shared"}
        assert names(registry, {"user_id": 1}) == {"identity_alice"}
        assert names(registry, {"user_id": 2}) == {"identity_bob"}
        results = await asyncio.gather(*(
            registry.call_tool(f"identity_{account}", {}, {"user_id": uid})
            for uid, account in ((1, "alice"), (2, "bob"), (99, "Shared"))
        ))
        assert list(map(str, results)) == ["alice", "bob", "Shared"]
        if transport == "http":
            assert (await registry.read_mcp_app_resource(IMAGELAB_SERVER, URI, {"user_id": 1}))["html"] == "alice"
            assert (await registry.read_mcp_app_resource(IMAGELAB_SERVER, URI))["html"] == "Shared"
        revoked.add("alice")
        requests.clear()
        assert "failed" in await registry.call_tool("identity_alice", {}, {"user_id": 1})
        assert requests and all(auth == b"Bearer fixture-token-a" for _, auth in requests)
        assert await registry.call_tool("identity_bob", {}, {"user_id": 2}) == "bob"
        assert await registry.call_tool("identity_Shared", {}, {"user_id": 99}) == "Shared"
    finally:
        await registry.shutdown()
        http_server.should_exit = True
        await asyncio.wait_for(serving, 5)
        sock.close()
