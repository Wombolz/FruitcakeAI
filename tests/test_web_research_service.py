from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.web_research import (
    CallbackSearchProvider,
    SearchProviderCapabilities,
    WebResearchService,
    WebSearchRequest,
)


@pytest.mark.asyncio
async def test_web_research_service_normalizes_primary_results():
    callback = AsyncMock(
        return_value=(
            [
                {
                    "title": "Primary result",
                    "url": "https://example.com/primary",
                    "snippet": "Useful evidence",
                    "published_at": "2026-10-05",
                }
            ],
            "",
        )
    )
    service = WebResearchService(
        [CallbackSearchProvider(name="primary", search_callback=callback)]
    )

    response = await service.search(WebSearchRequest(query="provider test", max_results=3))

    assert response.provider == "primary"
    assert response.fallback_from is None
    assert response.attempts == ("primary",)
    assert response.results[0].rank == 1
    assert response.results[0].excerpt == "Useful evidence"
    assert response.results[0].published_at == "2026-10-05"


@pytest.mark.asyncio
async def test_web_research_service_falls_back_without_leaking_provider_shape():
    primary = AsyncMock(return_value=([], "Primary quota exhausted"))
    fallback = AsyncMock(
        return_value=(
            [{"title": "Fallback result", "url": "https://example.com/fallback", "snippet": ""}],
            "",
        )
    )
    service = WebResearchService(
        [
            CallbackSearchProvider(name="primary", search_callback=primary),
            CallbackSearchProvider(name="fallback", search_callback=fallback),
        ]
    )

    response = await service.search(WebSearchRequest(query="fallback test"))

    assert response.provider == "fallback"
    assert response.fallback_from == "primary"
    assert response.attempts == ("primary", "fallback")
    assert response.results[0].provider == "fallback"


@pytest.mark.asyncio
async def test_web_research_service_skips_unavailable_or_incompatible_providers():
    unavailable = AsyncMock()
    incompatible = AsyncMock()
    healthy = AsyncMock(
        return_value=([{"title": "Healthy", "url": "https://example.com", "snippet": "ok"}], "")
    )
    service = WebResearchService(
        [
            CallbackSearchProvider(
                name="unavailable",
                search_callback=unavailable,
                available=lambda: False,
            ),
            CallbackSearchProvider(
                name="context_only",
                search_callback=incompatible,
                capabilities=SearchProviderCapabilities(
                    search_results=False,
                    llm_context=True,
                ),
            ),
            CallbackSearchProvider(name="healthy", search_callback=healthy),
        ]
    )

    response = await service.search(WebSearchRequest(query="capability test"))

    assert response.provider == "healthy"
    assert response.attempts == ("healthy",)
    unavailable.assert_not_awaited()
    incompatible.assert_not_awaited()
    healthy.assert_awaited_once()


@pytest.mark.asyncio
async def test_web_research_service_reports_no_available_provider():
    service = WebResearchService(
        [
            CallbackSearchProvider(
                name="disabled",
                search_callback=AsyncMock(),
                available=lambda: False,
            )
        ]
    )

    response = await service.search(WebSearchRequest(query="nothing"))

    assert response.provider == "unavailable"
    assert response.results == ()
    assert "No configured web search provider" in response.error
