"""
FruitcakeAI v5 — Web Research MCP Server (internal_python) — v2

Tools exposed to the agent:
  web_search  — Brave Search API with a temporary DuckDuckGo fallback
  fetch_page  — Fetch and clean a URL's text content

Designed to be called by the MCP registry:
  get_tools()                           → List[MCP tool schema dicts]
  call_tool(name, arguments, context)   → str result
"""

from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import re
import socket
import time
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import structlog

from app.config import settings
from app.web_research import CallbackSearchProvider, WebResearchService, WebSearchRequest

log = structlog.get_logger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_DDG_SEARCH_URL = "https://html.duckduckgo.com/html/"
_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"
_FETCH_TIMEOUT = 15
_SEARCH_TIMEOUT = 15

# Rate limiting aligned with the reference DuckDuckGo MCP server
_SEARCHES_PER_MINUTE = 30
_FETCHES_PER_MINUTE = 20

# Small in-memory caches to reduce repeated work during agent loops
_SEARCH_CACHE_TTL_SECONDS = 180
_PAGE_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 128


# ── Tool schemas ──────────────────────────────────────────────────────────────

_WEB_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "web_search",
    "description": (
        "Search the web. Use this to look up current events, "
        "factual information, product details, or anything that benefits from "
        "fresh web results. Returns titles, URLs, and snippets."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5, max 10)",
                "default": 5,
            },
            "region": {
                "type": "string",
                "description": "Search region code, e.g. us-en",
                "default": "us-en",
            },
        },
        "required": ["query"],
    },
}

_FETCH_PAGE_SCHEMA: Dict[str, Any] = {
    "name": "fetch_page",
    "description": (
        "Fetch the text content of a web page. Use this to read the full content "
        "of a URL returned by web_search or provided by the user. Returns cleaned "
        "text with HTML stripped."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch",
            },
        },
        "required": ["url"],
    },
}

_WEB_CONTEXT_SCHEMA: Dict[str, Any] = {
    "name": "web_context",
    "description": (
        "Retrieve citation-rich, pre-extracted web evidence for a research question. "
        "Use this instead of repeated web_search and fetch_page calls when the question "
        "needs synthesis across several current web sources."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The focused research question, up to 600 characters",
            },
            "depth": {
                "type": "string",
                "enum": ["standard", "deep"],
                "description": "Standard uses about 8K provider tokens; deep uses about 16K",
                "default": "standard",
            },
            "max_sources": {
                "type": "integer",
                "description": "Maximum source URLs to include (1-50)",
                "default": 20,
            },
            "region": {
                "type": "string",
                "description": "Search region code, e.g. us-en",
                "default": "us-en",
            },
            "freshness": {
                "type": "string",
                "description": "Optional freshness filter: pd, pw, pm, py, or a date range",
            },
            "threshold": {
                "type": "string",
                "enum": ["strict", "balanced", "lenient"],
                "description": "Optional relevance threshold",
            },
        },
        "required": ["query"],
    },
}


# ── Small helpers ─────────────────────────────────────────────────────────────

class SlidingWindowRateLimiter:
    """Simple async sliding-window limiter."""

    def __init__(self, max_calls: int, period_seconds: int = 60):
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self.calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """
        Wait until a slot is available.
        Returns the number of seconds waited.
        """
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()

                while self.calls and (now - self.calls[0]) > self.period_seconds:
                    self.calls.popleft()

                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return waited

                sleep_for = self.period_seconds - (now - self.calls[0])
            if sleep_for > 0:
                waited += sleep_for
                await asyncio.sleep(sleep_for)


class TTLCache:
    """Tiny in-memory TTL cache with bounded size."""

    def __init__(self, ttl_seconds: int, max_entries: int = 128):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def get(self, key: str) -> Optional[str]:
        item = self._data.get(key)
        if item is None:
            return None

        expires_at, value = item
        if time.monotonic() > expires_at:
            self._data.pop(key, None)
            return None

        # refresh LRU position
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: str) -> None:
        if key in self._data:
            self._data.pop(key, None)

        self._data[key] = (time.monotonic() + self.ttl_seconds, value)
        self._data.move_to_end(key)

        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)


_SEARCH_LIMITER = SlidingWindowRateLimiter(_SEARCHES_PER_MINUTE, 60)
_FETCH_LIMITER = SlidingWindowRateLimiter(_FETCHES_PER_MINUTE, 60)

_SEARCH_CACHE = TTLCache(_SEARCH_CACHE_TTL_SECONDS, _CACHE_MAX_ENTRIES)
_PAGE_CACHE = TTLCache(_PAGE_CACHE_TTL_SECONDS, _CACHE_MAX_ENTRIES)


# ── Public MCP interface ──────────────────────────────────────────────────────

def get_tools() -> List[Dict[str, Any]]:
    tools = [_WEB_SEARCH_SCHEMA, _FETCH_PAGE_SCHEMA]
    if settings.brave_context_enabled and settings.brave_search_api_key.strip():
        tools.append(_WEB_CONTEXT_SCHEMA)
    return tools


async def call_tool(
    tool_name: str, arguments: Dict[str, Any], user_context: Any = None
) -> Any:
    if tool_name == "web_search":
        return await _web_search(arguments, user_context=user_context)
    if tool_name == "fetch_page":
        return await _fetch_page(arguments, user_context=user_context)
    if tool_name == "web_context":
        return await _web_context(arguments, user_context=user_context)
    return f"Unknown tool: {tool_name}"


# ── DuckDuckGo search ─────────────────────────────────────────────────────────

async def _web_search(arguments: Dict[str, Any], user_context: Any = None) -> str:
    query = (arguments.get("query") or "").strip()
    if not query:
        return "No search query provided."

    try:
        max_results = int(arguments.get("max_results", 5))
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(max_results, 10))

    region = (arguments.get("region") or "us-en").strip() or "us-en"

    service = _build_web_research_service()
    provider_chain = ",".join(service.provider_names) or "unavailable"
    cache_key = f"search::{provider_chain}::{query}::{max_results}::{region}"
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        log.info("web_search cache_hit", query=query, max_results=max_results, region=region)
        return cached

    waited = await _SEARCH_LIMITER.acquire()
    if waited > 0:
        log.info("web_search rate_limited_wait", query=query, waited_seconds=round(waited, 3))

    started = time.perf_counter()

    response = await service.search(
        WebSearchRequest(query=query, max_results=max_results, region=region)
    )
    provider = response.provider
    if response.fallback_from:
        log.warning(
            "web_search provider_fallback",
            primary=response.fallback_from,
            fallback=response.provider,
            attempts=list(response.attempts),
        )

    if not response.results:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        log.info("web_search no_results", query=query, region=region, provider=provider, elapsed_ms=elapsed_ms)
        return response.error or f"No results found for: {query}"

    results = [
        {
            "title": result.title,
            "url": result.url,
            "snippet": result.excerpt,
        }
        for result in response.results
    ]
    formatted = _format_search_results(query=query, results=results)

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    log.info(
        "web_search success",
        query=query,
        region=region,
        provider=provider,
        result_count=len(results),
        elapsed_ms=elapsed_ms,
    )

    _SEARCH_CACHE.set(cache_key, formatted)
    return formatted


def _build_web_research_service() -> WebResearchService:
    """Resolve an ordered provider chain without changing the MCP tool contract."""
    brave = CallbackSearchProvider(
        name="brave",
        search_callback=_search_brave,
        available=lambda: bool(settings.brave_search_api_key.strip()),
    )
    duckduckgo = CallbackSearchProvider(
        name="duckduckgo",
        search_callback=_search_duckduckgo,
    )
    preference = str(settings.web_search_provider or "auto").strip().casefold()
    if preference == "duckduckgo":
        return WebResearchService([duckduckgo])
    if preference == "brave":
        providers = [brave]
        if settings.brave_search_fallback_to_ddg:
            providers.append(duckduckgo)
        return WebResearchService(providers)
    providers = [brave, duckduckgo] if settings.brave_search_api_key.strip() else [duckduckgo]
    if not settings.brave_search_fallback_to_ddg and providers and providers[0] is brave:
        providers = [brave]
    return WebResearchService(providers)


def _brave_locale(region: str) -> tuple[str, str]:
    parts = [part.lower() for part in str(region or "").replace("_", "-").split("-") if part]
    country = parts[0] if parts and len(parts[0]) == 2 else "us"
    language = parts[1] if len(parts) > 1 and len(parts[1]) == 2 else "en"
    return country, language


async def _web_context(arguments: Dict[str, Any], user_context: Any = None) -> Any:
    del user_context
    if not settings.brave_context_enabled or not settings.brave_search_api_key.strip():
        return "Web context is not configured."
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "No web context query provided."
    query = query[:600]
    depth = str(arguments.get("depth") or "standard").strip().casefold()
    default_tokens = max(1_024, min(int(settings.brave_context_default_tokens), 32_768))
    maximum_tokens = 16_384 if depth == "deep" else default_tokens
    try:
        max_sources = max(1, min(int(arguments.get("max_sources", 20)), 50))
    except (TypeError, ValueError):
        max_sources = 20
    region = str(arguments.get("region") or "us-en").strip() or "us-en"
    country, language = _brave_locale(region)
    payload: Dict[str, Any] = {
        "q": query,
        "country": country.upper(),
        "search_lang": language,
        "count": max_sources,
        "maximum_number_of_urls": max_sources,
        "maximum_number_of_tokens": maximum_tokens,
        "enable_source_metadata": True,
    }
    freshness = str(arguments.get("freshness") or "").strip()
    if freshness:
        payload["freshness"] = freshness
    threshold = str(arguments.get("threshold") or "").strip().casefold()
    if threshold in {"strict", "balanced", "lenient"}:
        payload["context_threshold_mode"] = threshold

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "X-Subscription-Token": settings.brave_search_api_key.strip(),
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(max(5, int(settings.brave_context_timeout_seconds))),
        ) as client:
            response = await client.post(_BRAVE_CONTEXT_URL, json=payload)
            response.raise_for_status()
            response_payload = response.json()
            usage_headers = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
                if str(key).lower().startswith("x-ratelimit")
            }
    except httpx.TimeoutException:
        log.warning("web_context brave_timeout", query=query, depth=depth)
        return f"Web context timed out for: {query}"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        log.warning("web_context brave_http_error", query=query, depth=depth, status=status)
        return f"Web context failed with HTTP {status} for: {query}"
    except (httpx.RequestError, ValueError) as exc:
        log.warning("web_context brave_request_error", query=query, depth=depth, error_type=type(exc).__name__)
        return f"Web context failed due to a provider error for: {query}"

    formatted, source_count, snippet_count = _format_brave_context(
        query=query,
        payload=response_payload,
        requested_tokens=maximum_tokens,
    )
    log.info(
        "web_context success",
        query=query,
        depth=depth,
        requested_tokens=maximum_tokens,
        source_count=source_count,
        snippet_count=snippet_count,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    citations = _structured_brave_sources(response_payload)
    return {
        "content": [{"type": "text", "text": formatted}],
        "structuredContent": {
            "provider": "brave",
            "capability": "llm_context",
            "query": query,
            "requested_tokens": maximum_tokens,
            "source_count": source_count,
            "snippet_count": snippet_count,
            "coverage_note": "Extracted passages may be partial.",
            "sources": citations,
            "citations": citations,
            "usage": usage_headers,
        },
    }


def _structured_brave_sources(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    grounding = payload.get("grounding") or {}
    generic = (grounding.get("generic") or []) if isinstance(grounding, dict) else []
    source_metadata = payload.get("sources") or {}
    citations: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in generic if isinstance(generic, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        metadata = source_metadata.get(url) if isinstance(source_metadata, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        title = _clean_text_inline(str(item.get("title") or metadata.get("title") or "Untitled source"))
        citation: Dict[str, Any] = {"url": url, "title": title, "source": "brave"}
        age = metadata.get("age") or []
        if isinstance(age, list):
            published_at = next(
                (str(age[index]).strip() for index in (3, 1, 0) if len(age) > index and str(age[index]).strip()),
                "",
            )
            if published_at:
                citation["published_at"] = published_at
        citations.append(citation)
    return citations


def _format_brave_context(
    *,
    query: str,
    payload: Any,
    requested_tokens: int,
) -> tuple[str, int, int]:
    if not isinstance(payload, dict):
        return f"No web context found for: {query}", 0, 0
    grounding = payload.get("grounding") or {}
    generic = (grounding.get("generic") or []) if isinstance(grounding, dict) else []
    sources = payload.get("sources") or {}
    if not isinstance(generic, list) or not generic:
        return f"No web context found for: {query}", 0, 0

    lines = [
        f"Web context for: {query}",
        "Provider: Brave LLM Context",
        f"Requested provider budget: {requested_tokens} tokens",
        "Use the source URLs below for citations. Extracted passages may be partial.",
        "",
    ]
    source_count = 0
    snippet_count = 0
    seen_urls: set[str] = set()
    for item in generic:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        source_count += 1
        source_meta = sources.get(url) if isinstance(sources, dict) else {}
        source_meta = source_meta if isinstance(source_meta, dict) else {}
        title = _clean_text_inline(str(item.get("title") or source_meta.get("title") or "Untitled source"))
        age = source_meta.get("age") or []
        published_at = ""
        if isinstance(age, list):
            published_at = next(
                (str(age[index]).strip() for index in (3, 1, 0) if len(age) > index and str(age[index]).strip()),
                "",
            )
        lines.extend([f"[Source {source_count}] {title}", f"URL: {url}"])
        if published_at:
            lines.append(f"Published/updated: {published_at}")
        snippets = item.get("snippets") or []
        if not isinstance(snippets, list):
            snippets = [snippets]
        for snippet in snippets:
            text = str(snippet or "").strip()
            if not text:
                continue
            snippet_count += 1
            lines.append(f"Passage {snippet_count}: {text}")
        lines.append("")
    if not source_count:
        return f"No web context found for: {query}", 0, 0
    return "\n".join(lines).rstrip(), source_count, snippet_count


async def _search_brave(
    query: str,
    max_results: int,
    region: str,
) -> tuple[List[Dict[str, str]], str]:
    country, language = _brave_locale(region)
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": settings.brave_search_api_key.strip(),
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(_SEARCH_TIMEOUT)) as client:
            response = await client.get(
                _BRAVE_SEARCH_URL,
                params={
                    "q": query,
                    "count": max_results,
                    "country": country,
                    "search_lang": language,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException:
        log.warning("web_search brave_timeout", query=query, region=region)
        return [], f"Web search timed out for: {query}"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        retry_after = exc.response.headers.get("X-RateLimit-Reset") if exc.response is not None else None
        log.warning("web_search brave_http_error", query=query, region=region, status=status, retry_after=retry_after)
        return [], f"Web search failed with HTTP {status} for: {query}"
    except (httpx.RequestError, ValueError) as exc:
        log.warning("web_search brave_request_error", query=query, region=region, error=str(exc))
        return [], f"Web search failed due to a provider error for: {query}"

    raw_results = ((payload.get("web") or {}).get("results") or []) if isinstance(payload, dict) else []
    results: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = _clean_text_inline(str(item.get("title") or ""))
        url = str(item.get("url") or "").strip()
        snippet = _clean_text_inline(str(item.get("description") or ""))
        if not title or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results, "" if results else f"No results found for: {query}"


async def _search_duckduckgo(
    query: str,
    max_results: int,
    region: str,
) -> tuple[List[Dict[str, str]], str]:
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(_SEARCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            response = await client.post(_DDG_SEARCH_URL, data={"q": query, "b": "", "kl": region})
            response.raise_for_status()
    except httpx.TimeoutException:
        log.warning("web_search ddg_timeout", query=query, region=region)
        return [], f"Web search timed out for: {query}"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        log.warning("web_search ddg_http_error", query=query, region=region, status=status)
        return [], f"Web search failed with HTTP {status} for: {query}"
    except httpx.RequestError as exc:
        log.warning("web_search ddg_request_error", query=query, region=region, error=str(exc))
        return [], f"Web search failed due to a network error: {exc}"
    return _parse_ddg_html(response.text, max_results), ""


def _parse_ddg_html(html: str, max_results: int) -> List[Dict[str, str]]:
    """Parse DuckDuckGo HTML results page."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _parse_ddg_regex(html, max_results)

    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    # DuckDuckGo HTML commonly uses .result containers
    for result_div in soup.select(".result")[: max_results * 3]:
        title_tag = result_div.select_one(".result__a")
        snippet_tag = result_div.select_one(".result__snippet")

        if not title_tag:
            continue

        title = _clean_text_inline(title_tag.get_text(" ", strip=True))
        raw_href = title_tag.get("href", "")
        url = _clean_ddg_url(raw_href)
        snippet = _clean_text_inline(snippet_tag.get_text(" ", strip=True) if snippet_tag else "")

        if not url or not title:
            continue
        if url in seen_urls:
            continue

        seen_urls.add(url)
        results.append({"title": title, "url": url, "snippet": snippet})

        if len(results) >= max_results:
            break

    return results


def _parse_ddg_regex(html: str, max_results: int) -> List[Dict[str, str]]:
    """Fallback regex parser if BeautifulSoup is unavailable."""
    results: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in pattern.finditer(html):
        raw_url = match.group(1)
        raw_title = match.group(2)

        url = _clean_ddg_url(raw_url)
        title = _clean_text_inline(_strip_tags(raw_title))

        if not url or not title or url in seen_urls:
            continue

        seen_urls.add(url)
        results.append({"title": title, "url": url, "snippet": ""})

        if len(results) >= max_results:
            break

    return results


def _clean_ddg_url(href: str) -> Optional[str]:
    """
    DuckDuckGo wraps result URLs in redirect links like:
      //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&...
    Extract the actual destination URL.
    """
    if not href:
        return None

    href = html_lib.unescape(href.strip())

    if href.startswith("//"):
        href = "https:" + href

    parsed = urlparse(href)
    if parsed.path == "/l/":
        qs = parse_qs(parsed.query)
        uddg_values = qs.get("uddg")
        if uddg_values:
            return unquote(uddg_values[0])

    if parsed.scheme in ("http", "https"):
        return href

    return None


def _format_search_results(query: str, results: List[Dict[str, str]]) -> str:
    lines = [
        f"Web search results for: {query}",
        f"Returned {len(results)} result(s).",
        "",
    ]

    for i, result in enumerate(results, 1):
        title = result.get("title", "").strip() or "(untitled)"
        url = result.get("url", "").strip()
        snippet = _clean_text_inline(result.get("snippet", ""))

        lines.append(f"[{i}] {title}")
        lines.append(f"URL: {url}")
        if snippet:
            lines.append(f"Snippet: {snippet}")
        lines.append("")

    return "\n".join(lines).rstrip()


# ── Page fetching ─────────────────────────────────────────────────────────────

async def _fetch_page(arguments: Dict[str, Any], user_context: Any = None) -> str:
    url = (arguments.get("url") or "").strip()
    if not url:
        return "No URL provided."

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Unsupported URL scheme: {parsed.scheme or '(missing scheme)'}"

    blocked_reason = await _blocked_target_reason(parsed)
    if blocked_reason is not None:
        log.warning("fetch_page blocked_target", url=url, reason=blocked_reason)
        return f"Blocked URL target: {blocked_reason}"

    cache_key = f"page::{url}"
    cached = _PAGE_CACHE.get(cache_key)
    if cached is not None:
        log.info("fetch_page cache_hit", url=url)
        return cached

    waited = await _FETCH_LIMITER.acquire()
    if waited > 0:
        log.info("fetch_page rate_limited_wait", url=url, waited_seconds=round(waited, 3))

    started = time.perf_counter()

    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(_FETCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.TimeoutException:
        log.warning("fetch_page timeout", url=url)
        return f"Timed out while fetching: {url}"
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        log.warning("fetch_page http_status_error", url=url, status=status)
        return f"Failed to fetch {url}: HTTP {status}"
    except httpx.RequestError as e:
        log.warning("fetch_page request_error", url=url, error=str(e))
        return f"Failed to fetch {url}: network error: {e}"
    except Exception as e:
        log.exception("fetch_page unexpected_error", url=url, error=str(e))
        return f"Failed to fetch {url}: unexpected error: {e}"

    content_type = response.headers.get("content-type", "")
    normalized_content_type = content_type.lower()

    if "text" not in normalized_content_type and "html" not in normalized_content_type:
        log.info("fetch_page unsupported_content_type", url=url, content_type=content_type)
        return f"Cannot read content type: {content_type}"

    text = _extract_text(response.text)
    if not text.strip():
        log.info("fetch_page empty_extracted_text", url=url, content_type=content_type)
        return f"No readable text content found at: {url}"

    max_page_chars = max(1_000, int(settings.web_fetch_max_chars))
    was_truncated = len(text) > max_page_chars
    if was_truncated:
        text = (
            text[:max_page_chars]
            + f"\n\n[... content truncated at {max_page_chars} characters ...]"
        )

    title = _extract_title(response.text)
    title_line = f"Title: {title}\n" if title else ""
    result = f"{title_line}Page content from {url}:\n\n{text}"
    _PAGE_CACHE.set(cache_key, result)

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    log.info(
        "fetch_page success",
        url=url,
        content_type=content_type,
        truncated=was_truncated,
        chars=len(text),
        elapsed_ms=elapsed_ms,
    )

    return result


def _is_private_or_local_ip(ip_text: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def _blocked_target_reason(parsed_url) -> Optional[str]:
    host = (parsed_url.hostname or "").strip().lower()
    if not host:
        return "missing host"

    if host in {"localhost", "127.0.0.1", "::1"}:
        return "localhost"
    if host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return "local/internal domain"

    if _is_private_or_local_ip(host):
        return "private/local IP address"

    port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return None
    except Exception:
        return None

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_text = sockaddr[0]
        if _is_private_or_local_ip(ip_text):
            return f"resolved to private/local IP ({ip_text})"
    return None


def _extract_title(html: str) -> str:
    """Best-effort <title> extraction for source attribution in evidence rows."""
    title = ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("title")
        if tag:
            title = tag.get_text()
    except ImportError:
        match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        if match:
            title = html_lib.unescape(match.group(1))
    return _clean_text_inline(title)[:200]


def _extract_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        for tag in soup([
            "script",
            "style",
            "nav",
            "footer",
            "header",
            "noscript",
            "svg",
            "iframe",
            "form",
            "aside",
        ]):
            tag.decompose()

        text = soup.get_text(separator="\n")
    except ImportError:
        text = _strip_tags(html)

    lines = [_clean_text_inline(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<script.*?>.*?</script>", " ", value)
    value = re.sub(r"(?is)<style.*?>.*?</style>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return html_lib.unescape(value)


def _clean_text_inline(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
