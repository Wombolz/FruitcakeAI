"""Adapters for provider-specific web research implementations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.web_research.models import (
    SearchProviderCapabilities,
    SearchProviderResponse,
    WebSearchRequest,
    WebSearchResult,
)


LegacySearchCallback = Callable[
    [str, int, str],
    Awaitable[tuple[list[dict[str, Any]], str]],
]


class CallbackSearchProvider:
    """Normalize an existing provider callback behind the shared interface."""

    def __init__(
        self,
        *,
        name: str,
        search_callback: LegacySearchCallback,
        available: Callable[[], bool] | None = None,
        capabilities: SearchProviderCapabilities | None = None,
    ) -> None:
        self.name = name
        self._search_callback = search_callback
        self._available = available or (lambda: True)
        self.capabilities = capabilities or SearchProviderCapabilities()

    @property
    def is_available(self) -> bool:
        return bool(self._available())

    async def search(self, request: WebSearchRequest) -> SearchProviderResponse:
        if not self.is_available:
            return SearchProviderResponse(
                provider=self.name,
                error=f"Web search provider '{self.name}' is not configured.",
                retryable=False,
            )
        if not self.capabilities.search_results:
            return SearchProviderResponse(
                provider=self.name,
                error=f"Web search provider '{self.name}' does not support search results.",
                retryable=False,
            )

        raw_results, error = await self._search_callback(
            request.query,
            request.max_results,
            request.region,
        )
        normalized: list[WebSearchResult] = []
        seen_urls: set[str] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title or not url or url in seen_urls:
                continue
            seen_urls.add(url)
            normalized.append(
                WebSearchResult(
                    title=title,
                    url=url,
                    excerpt=str(item.get("excerpt") or item.get("snippet") or "").strip(),
                    provider=self.name,
                    rank=len(normalized) + 1,
                    published_at=(str(item.get("published_at") or "").strip() or None),
                    metadata=(
                        dict(item.get("metadata") or {})
                        if isinstance(item.get("metadata"), dict)
                        else {}
                    ),
                )
            )
            if len(normalized) >= request.max_results:
                break

        return SearchProviderResponse(
            provider=self.name,
            results=tuple(normalized),
            error=str(error or ""),
        )
