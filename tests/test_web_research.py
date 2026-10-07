from unittest.mock import AsyncMock, MagicMock, patch

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

    assert "Brave result" in result["content"][0]["text"]
    assert result["structuredContent"]["provider"] == "brave"
    assert result["structuredContent"]["citations"] == [{
        "title": "Brave result",
        "url": "https://example.org/brave",
        "source": "brave",
    }]
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

    assert "Fallback result" in result["content"][0]["text"]
    assert result["structuredContent"]["provider"] == "duckduckgo"
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

    assert "No-key result" in result["content"][0]["text"]
    assert result["structuredContent"]["provider"] == "duckduckgo"
    brave.assert_not_awaited()
    ddg.assert_awaited_once()


def test_brave_locale_maps_existing_region_format():
    assert web_research_v2._brave_locale("us-en") == ("us", "en")
    assert web_research_v2._brave_locale("gb_en") == ("gb", "en")


def test_web_context_is_only_advertised_when_brave_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "brave_context_enabled", True)
    monkeypatch.setattr(settings, "brave_search_api_key", "")
    assert "web_context" not in {tool["name"] for tool in web_research_v2.get_tools()}

    monkeypatch.setattr(settings, "brave_search_api_key", "test-key")
    assert "web_context" in {tool["name"] for tool in web_research_v2.get_tools()}


def test_format_brave_context_preserves_source_boundaries_and_dates():
    formatted, source_count, snippet_count = web_research_v2._format_brave_context(
        query="What changed?",
        requested_tokens=8192,
        payload={
            "grounding": {
                "generic": [
                    {
                        "title": "Primary source",
                        "url": "https://example.org/report",
                        "snippets": ["First supported fact.", "Second supported fact."],
                    }
                ]
            },
            "sources": {
                "https://example.org/report": {
                    "title": "Primary source",
                    "age": ["October 5, 2026", "2026-10-05", "today", "2026-10-05T12:00:00Z"],
                }
            },
        },
    )

    assert source_count == 1
    assert snippet_count == 2
    assert "[Source 1] Primary source" in formatted
    assert "URL: https://example.org/report" in formatted
    assert "Published/updated: 2026-10-05T12:00:00Z" in formatted
    assert "First supported fact." in formatted


@pytest.mark.asyncio
async def test_web_context_calls_brave_with_bounded_provider_budget(monkeypatch):
    monkeypatch.setattr(settings, "brave_context_enabled", True)
    monkeypatch.setattr(settings, "brave_search_api_key", "test-key")
    monkeypatch.setattr(settings, "brave_context_default_tokens", 8192)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "grounding": {
            "generic": [
                {"title": "Result", "url": "https://example.org", "snippets": ["Evidence"]}
            ]
        },
        "sources": {"https://example.org": {"title": "Result", "age": []}},
    }
    client = AsyncMock()
    client.post.return_value = response
    context_manager = MagicMock()
    context_manager.__aenter__ = AsyncMock(return_value=client)
    context_manager.__aexit__ = AsyncMock(return_value=None)

    with patch.object(web_research_v2.httpx, "AsyncClient", return_value=context_manager):
        result = await web_research_v2._web_context(
            {"query": "provider context", "depth": "deep", "max_sources": 30}
        )

    assert "Provider: Brave LLM Context" in result["content"][0]["text"]
    assert result["structuredContent"]["source_count"] == 1
    assert result["structuredContent"]["citations"][0]["url"] == "https://example.org"
    request = client.post.await_args
    assert request.args[0] == web_research_v2._BRAVE_CONTEXT_URL
    assert request.kwargs["json"]["maximum_number_of_tokens"] == 16_384
    assert request.kwargs["json"]["maximum_number_of_urls"] == 30
