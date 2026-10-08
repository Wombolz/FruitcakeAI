from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.artifacts import get_mcp_app_resource


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
