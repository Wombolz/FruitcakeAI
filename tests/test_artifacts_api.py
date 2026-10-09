from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.artifacts import MCPAppToolCallRequest, call_mcp_app_tool, get_mcp_app_resource


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
