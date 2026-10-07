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
        result = await search_places(
            query="Zaxby's", near="Statesboro, GA", limit=3, provider="nominatim"
        )

    assert "Place search results for: Zaxby's near Statesboro, GA" in result
    assert "147 Tormenta Way" in result
    assert "lat=32.4377, lon=-81.764" in result
    assert result.structured_content["provider"] == "nominatim"
    assert result.structured_content["places"][0]["latitude"] == 32.4377
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_places_normalizes_brave_results(monkeypatch):
    monkeypatch.setattr("app.json_api.settings.brave_search_api_key", "test-key")
    payload = {
        "results": [
            {
                "title": "The Daily Grind",
                "url": "https://example.com/daily-grind",
                "coordinates": [32.448, -81.783],
                "postal_address": {"displayAddress": "17 Main St, Statesboro, GA"},
                "categories": ["Coffee shop", "Cafe"],
                "contact": {"telephone": "+1 555-0100"},
                "rating": {"ratingValue": 4.6, "bestRating": 5, "reviewCount": 82},
                "distance": {"value": 1.2, "units": "mi"},
            }
        ]
    }

    with patch("app.json_api.fetch_json", new=AsyncMock(return_value=payload)) as mocked:
        result = await search_places(
            query="coffee", near="Statesboro GA United States", limit=3, provider="brave"
        )

    structured = result.structured_content
    assert structured["provider"] == "brave"
    assert structured["fallback_used"] is False
    assert structured["places"] == [
        {
            "name": "The Daily Grind",
            "address": "17 Main St, Statesboro, GA",
            "latitude": 32.448,
            "longitude": -81.783,
            "category": "Coffee shop",
            "categories": ["Coffee shop", "Cafe"],
            "url": "https://example.com/daily-grind",
            "phone": "+1 555-0100",
            "rating": 4.6,
            "rating_max": 5.0,
            "review_count": 82,
            "distance": 1.2,
            "distance_unit": "mi",
            "provider": "brave",
        }
    ]
    assert structured["citations"][0]["url"] == "https://example.com/daily-grind"
    assert "rating=4.6 (82 reviews)" in result
    request = mocked.await_args.kwargs
    assert request["url"].endswith("/local/place_search")
    assert request["params"]["location"] == "Statesboro GA United States"
    assert request["headers"]["X-Subscription-Token"] == "test-key"


@pytest.mark.asyncio
async def test_search_places_auto_falls_back_to_nominatim(monkeypatch):
    monkeypatch.setattr("app.json_api.settings.brave_search_api_key", "test-key")
    brave_error = JsonApiError("Brave unavailable")
    nominatim_places = [
        {
            "name": "Fallback Cafe",
            "address": "1 Main St",
            "latitude": 32.0,
            "longitude": -81.0,
            "provider": "nominatim",
        }
    ]

    with (
        patch("app.json_api._search_places_brave", new=AsyncMock(side_effect=brave_error)) as brave,
        patch("app.json_api._search_places_nominatim", new=AsyncMock(return_value=nominatim_places)) as nominatim,
    ):
        result = await search_places(query="cafe", near="Statesboro", provider="auto")

    assert result.structured_content["provider"] == "nominatim"
    assert result.structured_content["providers_attempted"] == ["brave", "nominatim"]
    assert result.structured_content["fallback_used"] is True
    brave.assert_awaited_once()
    nominatim.assert_awaited_once()


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
