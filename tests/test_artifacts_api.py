from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.artifacts import (
    MCPAppToolApprovalDecision,
    MCPAppToolCallRequest,
    call_mcp_app_tool,
    get_mcp_app_resource,
    resolve_mcp_app_tool_approval,
)
from app.db.models import AuditLog
from app.mcp.registry import MCPAppApprovalRequired


@pytest.mark.asyncio
async def test_mcp_app_resource_endpoint_uses_authenticated_bridge():
    registry = MagicMock()
    registry.read_mcp_app_resource = AsyncMock(return_value={
        "server": "weather",
        "uri": "ui://weather/dashboard.html",
        "mime_type": "text/html;profile=mcp-app",
        "html": "<html></html>",
    })

    with patch("app.api.artifacts.get_mcp_registry", return_value=registry):
        response = await get_mcp_app_resource(
            server="weather",
            uri="ui://weather/dashboard.html",
            current_user=MagicMock(id=1),
        )

    assert response["html"] == "<html></html>"
    registry.read_mcp_app_resource.assert_awaited_once_with(
        "weather",
        "ui://weather/dashboard.html",
    )


@pytest.mark.asyncio
async def test_mcp_app_resource_endpoint_maps_unlinked_resource_to_404():
    registry = MagicMock()
    registry.read_mcp_app_resource = AsyncMock(
        side_effect=LookupError("MCP App resource is not linked by a registered tool")
    )

    with patch("app.api.artifacts.get_mcp_registry", return_value=registry):
        with pytest.raises(HTTPException) as exc_info:
            await get_mcp_app_resource(
                server="weather",
                uri="ui://weather/private.html",
                current_user=MagicMock(id=1),
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_mcp_app_tool_endpoint_audits_read_only_call():
    registry = MagicMock()
    registry.call_mcp_app_tool = AsyncMock(return_value={
        "server": "weather",
        "resource_uri": "ui://weather/dashboard.html",
        "tool": "refresh_dashboard",
        "result": {"structuredContent": {"temperature": 72}},
    })
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    user = MagicMock(id=7)

    with patch("app.api.artifacts.get_mcp_registry", return_value=registry):
        response = await call_mcp_app_tool(
            request=MCPAppToolCallRequest(
                server="weather",
                resource_uri="ui://weather/dashboard.html",
                tool="refresh_dashboard",
                arguments={"city": "Statesboro"},
            ),
            current_user=user,
            db=db,
        )

    assert response["result"]["structuredContent"]["temperature"] == 72
    registry.call_mcp_app_tool.assert_awaited_once()
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_app_tool_endpoint_maps_mutation_to_403():
    registry = MagicMock()
    registry.call_mcp_app_tool = AsyncMock(
        side_effect=PermissionError("MCP App tool is not declared read-only")
    )
    db = MagicMock()

    with patch("app.api.artifacts.get_mcp_registry", return_value=registry):
        with pytest.raises(HTTPException) as exc_info:
            await call_mcp_app_tool(
                request=MCPAppToolCallRequest(
                    server="weather",
                    resource_uri="ui://weather/dashboard.html",
                    tool="delete_station",
                    arguments={},
                ),
                current_user=MagicMock(id=1),
                db=db,
            )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_mcp_app_tool_endpoint_returns_durable_mutation_approval():
    registry = MagicMock()
    registry.call_mcp_app_tool = AsyncMock(
        side_effect=MCPAppApprovalRequired(
            "fieldkit_app_run_discovery",
            {
                "title": "Run Discovery",
                "description": "Scan the local network for AV devices.",
                "annotations": {"destructiveHint": False},
            },
        )
    )
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def refresh(row):
        row.id = 42

    db.refresh = AsyncMock(side_effect=refresh)
    with patch("app.api.artifacts.get_mcp_registry", return_value=registry):
        response = await call_mcp_app_tool(
            request=MCPAppToolCallRequest(
                server="fieldkit",
                resource_uri="ui://fieldkit/dashboard.html",
                tool="fieldkit_app_run_discovery",
                arguments={"save": False},
            ),
            current_user=MagicMock(id=7),
            db=db,
        )

    assert response["state"] == "waiting_approval"
    assert response["approval"]["id"] == 42
    assert response["approval"]["arguments"] == {"save": False}
    pending = db.add.call_args.args[0]
    assert isinstance(pending, AuditLog)
    assert pending.result_summary == "MCP App mutation awaiting approval"


@pytest.mark.asyncio
async def test_mcp_app_mutation_approval_executes_persisted_exact_intent():
    row = MagicMock(
        id=42,
        user_id=7,
        tool="mcp_app:fieldkit:fieldkit_app_run_discovery",
        arguments=(
            '{"kind":"mcp_app_mutation","server":"fieldkit",'
            '"resource_uri":"ui://fieldkit/dashboard.html",'
            '"tool":"fieldkit_app_run_discovery","arguments":{"save":false}}'
        ),
        result_summary="MCP App mutation awaiting approval",
    )
    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=scalar)
    db.commit = AsyncMock()
    registry = MagicMock()
    registry.call_mcp_app_tool = AsyncMock(return_value={
        "server": "fieldkit",
        "resource_uri": "ui://fieldkit/dashboard.html",
        "tool": "fieldkit_app_run_discovery",
        "result": {"structuredContent": {"started": True}},
    })

    with patch("app.api.artifacts.get_mcp_registry", return_value=registry):
        response = await resolve_mcp_app_tool_approval(
            approval_id=42,
            decision=MCPAppToolApprovalDecision(approved=True),
            current_user=MagicMock(id=7),
            db=db,
        )

    assert response["state"] == "completed"
    assert response["result"]["structuredContent"]["started"] is True
    registry.call_mcp_app_tool.assert_awaited_once_with(
        server_name="fieldkit",
        resource_uri="ui://fieldkit/dashboard.html",
        tool_name="fieldkit_app_run_discovery",
        arguments={"save": False},
        user_context=ANY,
        approved=True,
    )
    assert row.result_summary == "MCP App mutation approved and completed"


@pytest.mark.asyncio
async def test_mcp_app_mutation_denial_never_executes_tool():
    row = MagicMock(
        id=43,
        user_id=7,
        tool="mcp_app:fieldkit:fieldkit_app_run_discovery",
        arguments='{"kind":"mcp_app_mutation"}',
        result_summary="MCP App mutation awaiting approval",
    )
    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=scalar)
    db.commit = AsyncMock()
    registry = MagicMock()

    with patch("app.api.artifacts.get_mcp_registry", return_value=registry):
        response = await resolve_mcp_app_tool_approval(
            approval_id=43,
            decision=MCPAppToolApprovalDecision(approved=False),
            current_user=MagicMock(id=7),
            db=db,
        )

    assert response == {"state": "denied", "approval_id": 43}
    assert row.result_summary == "MCP App mutation denied"
    registry.call_mcp_app_tool.assert_not_called()
