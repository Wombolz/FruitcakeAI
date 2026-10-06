"""Provider selection and fallback for web research."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import structlog

from app.web_research.models import (
    SearchProviderCapabilities,
    SearchProviderResponse,
    WebSearchRequest,
    WebSearchResponse,
)


log = structlog.get_logger(__name__)


class SearchProvider(Protocol):
    name: str
    capabilities: SearchProviderCapabilities

    @property
    def is_available(self) -> bool: ...

    async def search(self, request: WebSearchRequest) -> SearchProviderResponse: ...


class WebResearchService:
    """Execute a capability request against an ordered provider chain."""

    def __init__(self, providers: Sequence[SearchProvider]) -> None:
        self._providers = tuple(providers)

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        attempts: list[str] = []
        first_provider: str | None = None
        last_error = ""

        for provider in self._providers:
            if not provider.is_available or not provider.capabilities.search_results:
                continue
            attempts.append(provider.name)
            first_provider = first_provider or provider.name
            try:
                response = await provider.search(request)
            except Exception as exc:
                last_error = f"Web search provider '{provider.name}' failed."
                log.warning(
                    "web_research.provider_error",
                    provider=provider.name,
                    error_type=type(exc).__name__,
                )
                continue

            last_error = response.error or last_error
            if response.results:
                return WebSearchResponse(
                    provider=response.provider,
                    results=response.results,
                    fallback_from=(
                        first_provider
                        if first_provider and first_provider != response.provider
                        else None
                    ),
                    attempts=tuple(attempts),
                    usage=response.usage,
                )
            if not response.retryable:
                break

        return WebSearchResponse(
            provider=attempts[-1] if attempts else "unavailable",
            error=last_error or "No configured web search provider is available.",
            fallback_from=(first_provider if len(attempts) > 1 else None),
            attempts=tuple(attempts),
        )
