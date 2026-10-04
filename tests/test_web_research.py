from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.mcp.servers import web_research_v2


@pytest.mark.asyncio
async def test_web_search_prefers_brave_when_key_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "brave_search_api_key", "test-key")
    brave = AsyncMock(return_value=([{
        "title": "Brave result",
        "url": "https://example.org/brave",
        "snippet": "Current information",
    }], ""))
    ddg = AsyncMock(return_value=([], "should not run"))

    with (
        patch.object(web_research_v2, "_search_brave", brave),
        patch.object(web_research_v2, "_search_duckduckgo", ddg),
    ):
        result = await web_research_v2._web_search({"query": "provider test brave"})

    assert "Brave result" in result
    brave.assert_awaited_once()
    ddg.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_search_falls_back_when_brave_fails(monkeypatch):
    monkeypatch.setattr(settings, "brave_search_api_key", "test-key")
    monkeypatch.setattr(settings, "brave_search_fallback_to_ddg", True)
    brave = AsyncMock(return_value=([], "Web search failed with HTTP 429"))
    ddg = AsyncMock(return_value=([{
        "title": "Fallback result",
        "url": "https://example.org/fallback",
        "snippet": "Fallback information",
    }], ""))

    with (
        patch.object(web_research_v2, "_search_brave", brave),
        patch.object(web_research_v2, "_search_duckduckgo", ddg),
    ):
        result = await web_research_v2._web_search({"query": "provider test fallback"})

    assert "Fallback result" in result
    brave.assert_awaited_once()
    ddg.assert_awaited_once()


@pytest.mark.asyncio
async def test_web_search_uses_duckduckgo_without_brave_key(monkeypatch):
    monkeypatch.setattr(settings, "brave_search_api_key", "")
    brave = AsyncMock(return_value=([], "should not run"))
    ddg = AsyncMock(return_value=([{
        "title": "No-key result",
        "url": "https://example.org/no-key",
        "snippet": "No-key information",
    }], ""))

    with (
        patch.object(web_research_v2, "_search_brave", brave),
        patch.object(web_research_v2, "_search_duckduckgo", ddg),
    ):
        result = await web_research_v2._web_search({"query": "provider test no key"})

    assert "No-key result" in result
    brave.assert_not_awaited()
    ddg.assert_awaited_once()


def test_brave_locale_maps_existing_region_format():
    assert web_research_v2._brave_locale("us-en") == ("us", "en")
    assert web_research_v2._brave_locale("gb_en") == ("gb", "en")
