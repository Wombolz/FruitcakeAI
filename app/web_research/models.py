"""Normalized provider-neutral web research models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchProviderCapabilities:
    search_results: bool = True
    llm_context: bool = False
    image_search: bool = False
    news_search: bool = False
    usage_metadata: bool = False


@dataclass(frozen=True)
class WebSearchRequest:
    query: str
    max_results: int = 5
    region: str = "us-en"


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    excerpt: str = ""
    provider: str = ""
    rank: int = 0
    published_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchProviderResponse:
    provider: str
    results: tuple[WebSearchResult, ...] = ()
    error: str = ""
    retryable: bool = True
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WebSearchResponse:
    provider: str
    results: tuple[WebSearchResult, ...] = ()
    error: str = ""
    fallback_from: str | None = None
    attempts: tuple[str, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
