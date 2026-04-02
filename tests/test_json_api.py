from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.json_api import JsonApiError, extract_json_fields, extract_json_path, fetch_json, search_places


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


def test_extract_json_path_supports_nested_dicts_and_lists():
    payload = {
        "passes": [
            {"start_utc": "2026-04-01T09:30:00+00:00", "max_elevation_deg": 67.0},
            {"start_utc": "2026-04-01T11:10:00+00:00", "max_elevation_deg": 42.0},
        ],
        "meta": {"symbol": "ISS"},
    }

    assert extract_json_path(payload, "meta.symbol") == "ISS"
    assert extract_json_path(payload, "passes[0].start_utc") == "2026-04-01T09:30:00+00:00"
    assert extract_json_path(payload, "passes.1.max_elevation_deg") == 42.0


def test_extract_json_fields_requires_all_selectors():
    payload = {"passes": [{"start_utc": "2026-04-01T09:30:00+00:00"}]}

    result = extract_json_fields(
        payload,
        {
            "first_pass": "passes[0].start_utc",
            "first_pass_list": "passes.0.start_utc",
        },
    )

    assert result == {
        "first_pass": "2026-04-01T09:30:00+00:00",
        "first_pass_list": "2026-04-01T09:30:00+00:00",
    }


def test_extract_json_fields_rejects_missing_values():
    payload = {"passes": []}

    with pytest.raises(JsonApiError, match="missing"):
        extract_json_fields(payload, {"first_pass": "passes[0].start_utc"})
