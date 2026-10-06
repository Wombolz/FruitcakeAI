"""Provider-neutral web research capabilities."""

from app.web_research.models import (
    SearchProviderCapabilities,
    SearchProviderResponse,
    WebSearchRequest,
    WebSearchResponse,
    WebSearchResult,
)
from app.web_research.providers import CallbackSearchProvider
from app.web_research.service import WebResearchService

__all__ = [
    "CallbackSearchProvider",
    "SearchProviderCapabilities",
    "SearchProviderResponse",
    "WebResearchService",
    "WebSearchRequest",
    "WebSearchResponse",
    "WebSearchResult",
]
