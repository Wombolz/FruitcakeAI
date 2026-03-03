"""
FruitcakeAI v5 — Web Research MCP Server (internal_python)

Tools exposed to the agent:
  web_search  — DuckDuckGo HTML search (no API key required)
  fetch_page  — Fetch and clean a URL's text content

Designed to be called by the MCP registry:
  get_tools()                           → List[MCP tool schema dicts]
  call_tool(name, arguments, context)   → str result
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlparse

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
_MAX_PAGE_CHARS = 8000  # Truncate fetched pages to keep context manageable


# ── Tool schemas ──────────────────────────────────────────────────────────────

_WEB_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "web_search",
    "description": (
        "Search the web using DuckDuckGo. Use this to look up current events, "
        "factual information, product details, or anything that benefits from "
        "fresh web results. Returns a list of titles, URLs, and snippets."
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


# ── Public MCP interface ──────────────────────────────────────────────────────

def get_tools() -> List[Dict[str, Any]]:
    return [_WEB_SEARCH_SCHEMA, _FETCH_PAGE_SCHEMA]


async def call_tool(
    tool_name: str, arguments: Dict[str, Any], user_context: Any = None
) -> str:
    if tool_name == "web_search":
        return await _web_search(arguments)
    if tool_name == "fetch_page":
        return await _fetch_page(arguments)
    return f"Unknown tool: {tool_name}"


# ── DuckDuckGo search ─────────────────────────────────────────────────────────

async def _web_search(arguments: Dict[str, Any]) -> str:
    query = (arguments.get("query") or "").strip()
    if not query:
        return "No search query provided."

    max_results = min(int(arguments.get("max_results", 5)), 10)

    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(_SEARCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            response = await client.post(
                _DDG_SEARCH_URL,
                data={"q": query, "b": "", "kl": "us-en"},
            )
            response.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("DuckDuckGo request failed", error=str(e))
        return f"Web search failed: {e}"

    results = _parse_ddg_html(response.text, max_results)
    if not results:
        return f"No results found for: {query}"

    lines = [f"Web search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    URL: {r['url']}")
        lines.append(f"    {r['snippet']}")
        lines.append("")

    return "\n".join(lines)


def _parse_ddg_html(html: str, max_results: int) -> List[Dict[str, str]]:
    """Parse DuckDuckGo HTML results page."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _parse_ddg_regex(html, max_results)

    soup = BeautifulSoup(html, "html.parser")
    results = []

    for result_div in soup.select(".result")[:max_results * 2]:
        title_tag = result_div.select_one(".result__a")
        snippet_tag = result_div.select_one(".result__snippet")

        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        raw_href = title_tag.get("href", "")
        url = _clean_ddg_url(raw_href)
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        if url and title:
            results.append({"title": title, "url": url, "snippet": snippet})

        if len(results) >= max_results:
            break

    return results


def _parse_ddg_regex(html: str, max_results: int) -> List[Dict[str, str]]:
    """Fallback regex parser if BeautifulSoup is unavailable."""
    results = []
    # Match DuckDuckGo result links
    pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
        re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        url = _clean_ddg_url(m.group(1))
        title = m.group(2).strip()
        if url and title:
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

    # Strip leading // if present
    if href.startswith("//"):
        href = "https:" + href

    parsed = urlparse(href)
    if parsed.path == "/l/":
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])

    # If it's already a direct URL, return as-is
    if parsed.scheme in ("http", "https"):
        return href

    return None


# ── Page fetching ─────────────────────────────────────────────────────────────

async def _fetch_page(arguments: Dict[str, Any]) -> str:
    url = (arguments.get("url") or "").strip()
    if not url:
        return "No URL provided."

    # Reject non-HTTP(S) schemes for safety
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Unsupported URL scheme: {parsed.scheme}"

    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(_FETCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Page fetch failed", url=url, error=str(e))
        return f"Failed to fetch {url}: {e}"

    content_type = response.headers.get("content-type", "")
    if "text" not in content_type and "html" not in content_type:
        return f"Cannot read content type: {content_type}"

    text = _extract_text(response.text)
    if len(text) > _MAX_PAGE_CHARS:
        text = text[:_MAX_PAGE_CHARS] + f"\n\n[... content truncated at {_MAX_PAGE_CHARS} characters ...]"

    return f"Page content from {url}:\n\n{text}"


def _extract_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # Remove script/style blocks
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except ImportError:
        # Fallback: strip tags with regex
        text = re.sub(r"<[^>]+>", " ", html)

    # Collapse blank lines
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)
