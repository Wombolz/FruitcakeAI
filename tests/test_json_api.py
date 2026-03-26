from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.json_api import JsonApiError, fetch_json, search_places


@pytest.mark.asyncio
async def test_fetch_json_raises_for_invalid_json():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.side_effect = ValueError("bad json")

    client = AsyncMock()
    client.get.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False

    with patch("app.json_api.httpx.AsyncClient", return_value=client):
        with pytest.raises(JsonApiError, match="not valid JSON"):
            await fetch_json(url="https://example.com/api")


@pytest.mark.asyncio
async def test_search_places_formats_results():
    payload = [
        {
            "name": "Zaxby's",
            "display_name": "Zaxby's, 147 Tormenta Way, Statesboro, Georgia 30458, United States",
            "lat": "32.4377",
            "lon": "-81.7640",
        }
    ]

    with patch("app.json_api.fetch_json", new=AsyncMock(return_value=payload)) as mocked:
        result = await search_places(query="Zaxby's", near="Statesboro, GA", limit=3)

    assert "Place search results for: Zaxby's near Statesboro, GA" in result
    assert "147 Tormenta Way" in result
    assert "lat=32.4377, lon=-81.7640" in result
    mocked.assert_awaited_once()
