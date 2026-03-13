"""
FruitcakeAI v5 — Web Research MCP Server (internal_python) — v2

Tools exposed to the agent:
  web_search  — DuckDuckGo HTML search (no API key required)
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
_FETCH_TIMEOUT = 15
_SEARCH_TIMEOUT = 15
_MAX_PAGE_CHARS = 8000

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
        "Search the web using DuckDuckGo. Use this to look up current events, "
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
                "description": "DuckDuckGo region code, e.g. us-en",
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
    return [_WEB_SEARCH_SCHEMA, _FETCH_PAGE_SCHEMA]


async def call_tool(
    tool_name: str, arguments: Dict[str, Any], user_context: Any = None
) -> str:
    if tool_name == "web_search":
        return await _web_search(arguments, user_context=user_context)
    if tool_name == "fetch_page":
        return await _fetch_page(arguments, user_context=user_context)
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

    cache_key = f"search::{query}::{max_results}::{region}"
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        log.info("web_search cache_hit", query=query, max_results=max_results, region=region)
        return cached

    waited = await _SEARCH_LIMITER.acquire()
    if waited > 0:
        log.info("web_search rate_limited_wait", query=query, waited_seconds=round(waited, 3))

    started = time.perf_counter()

    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(_SEARCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            response = await client.post(
                _DDG_SEARCH_URL,
                data={"q": query, "b": "", "kl": region},
            )
            response.raise_for_status()
    except httpx.TimeoutException:
        log.warning("web_search timeout", query=query, region=region)
        return f"Web search timed out for: {query}"
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        log.warning("web_search http_status_error", query=query, region=region, status=status)
        return f"Web search failed with HTTP {status} for: {query}"
    except httpx.RequestError as e:
        log.warning("web_search request_error", query=query, region=region, error=str(e))
        return f"Web search failed due to a network error: {e}"
    except Exception as e:
        log.exception("web_search unexpected_error", query=query, region=region, error=str(e))
        return f"Web search failed unexpectedly: {e}"

    results = _parse_ddg_html(response.text, max_results)
    if not results:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        log.info("web_search no_results", query=query, region=region, elapsed_ms=elapsed_ms)
        return f"No results found for: {query}"

    formatted = _format_search_results(query=query, results=results)

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    log.info(
        "web_search success",
        query=query,
        region=region,
        result_count=len(results),
        elapsed_ms=elapsed_ms,
    )

    _SEARCH_CACHE.set(cache_key, formatted)
    return formatted


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

    was_truncated = len(text) > _MAX_PAGE_CHARS
    if was_truncated:
        text = (
            text[:_MAX_PAGE_CHARS]
            + f"\n\n[... content truncated at {_MAX_PAGE_CHARS} characters ...]"
        )

    result = f"Page content from {url}:\n\n{text}"
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
