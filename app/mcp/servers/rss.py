"""
FruitcakeAI v5 — RSS MCP Server (internal_python)

Tools exposed to the agent:
  get_feed_items  — Fetch and return recent items from an RSS/Atom feed URL
  search_feeds    — Fetch multiple feeds concurrently and search for a keyword

Designed to be called by the MCP registry:
  get_tools()                           → List[MCP tool schema dicts]
  call_tool(name, arguments, context)   → str result
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

import structlog

log = structlog.get_logger(__name__)

_MAX_ITEMS = 20          # Hard cap per feed
_MAX_SUMMARY_CHARS = 400 # Truncate long summaries


# ── Tool schemas ──────────────────────────────────────────────────────────────

_GET_FEED_ITEMS_SCHEMA: Dict[str, Any] = {
    "name": "get_feed_items",
    "description": (
        "Fetch recent items from an RSS or Atom feed. Returns a list of titles, "
        "links, and summaries. Use this to get news or updates from a specific source."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The RSS or Atom feed URL",
            },
            "max_items": {
                "type": "integer",
                "description": f"Maximum number of items to return (default 10, max {_MAX_ITEMS})",
                "default": 10,
            },
        },
        "required": ["url"],
    },
}

_SEARCH_FEEDS_SCHEMA: Dict[str, Any] = {
    "name": "search_feeds",
    "description": (
        "Search for a keyword across multiple RSS/Atom feeds simultaneously. "
        "Fetches all feeds concurrently and returns matching items. Use this to "
        "find news about a topic across several sources."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of RSS or Atom feed URLs to search",
            },
            "query": {
                "type": "string",
                "description": "Keyword or phrase to search for in item titles and summaries",
            },
            "max_items_per_feed": {
                "type": "integer",
                "description": "Maximum results per feed (default 5)",
                "default": 5,
            },
        },
        "required": ["urls", "query"],
    },
}


# ── Public MCP interface ──────────────────────────────────────────────────────

def get_tools() -> List[Dict[str, Any]]:
    return [_GET_FEED_ITEMS_SCHEMA, _SEARCH_FEEDS_SCHEMA]


async def call_tool(
    tool_name: str, arguments: Dict[str, Any], user_context: Any = None
) -> str:
    if tool_name == "get_feed_items":
        return await _get_feed_items(arguments)
    if tool_name == "search_feeds":
        return await _search_feeds(arguments)
    return f"Unknown tool: {tool_name}"


# ── Feed fetching ─────────────────────────────────────────────────────────────

async def _fetch_feed(url: str) -> Any:
    """
    Parse an RSS/Atom feed URL using feedparser.
    feedparser.parse() is synchronous, so we run it in the default executor.
    """
    try:
        import feedparser
    except ImportError:
        raise RuntimeError("feedparser is not installed. Run: pip install feedparser")

    loop = asyncio.get_running_loop()
    feed = await loop.run_in_executor(None, feedparser.parse, url)
    return feed


# ── get_feed_items ────────────────────────────────────────────────────────────

async def _get_feed_items(arguments: Dict[str, Any]) -> str:
    url = (arguments.get("url") or "").strip()
    if not url:
        return "No feed URL provided."

    max_items = min(int(arguments.get("max_items", 10)), _MAX_ITEMS)

    try:
        feed = await _fetch_feed(url)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        log.warning("Feed fetch failed", url=url, error=str(e))
        return f"Failed to fetch feed: {e}"

    if feed.bozo and not feed.entries:
        return f"Could not parse feed at {url}: {feed.bozo_exception}"

    feed_title = feed.feed.get("title", url)
    entries = feed.entries[:max_items]

    if not entries:
        return f"No items found in feed: {feed_title}"

    lines = [f"Feed: {feed_title}\n"]
    for i, entry in enumerate(entries, 1):
        title = entry.get("title", "(no title)").strip()
        link = entry.get("link", "")
        published = entry.get("published", entry.get("updated", ""))
        summary = _get_summary(entry)

        lines.append(f"[{i}] {title}")
        if published:
            lines.append(f"    Published: {published}")
        if link:
            lines.append(f"    URL: {link}")
        if summary:
            lines.append(f"    {summary}")
        lines.append("")

    return "\n".join(lines)


# ── search_feeds ──────────────────────────────────────────────────────────────

async def _search_feeds(arguments: Dict[str, Any]) -> str:
    urls = arguments.get("urls") or []
    query = (arguments.get("query") or "").strip().lower()
    max_items_per_feed = min(int(arguments.get("max_items_per_feed", 5)), _MAX_ITEMS)

    if not urls:
        return "No feed URLs provided."
    if not query:
        return "No search query provided."

    # Fetch all feeds concurrently
    tasks = [_fetch_feed(url) for url in urls]
    try:
        feeds = await asyncio.gather(*tasks, return_exceptions=True)
    except RuntimeError as e:
        return str(e)

    all_matches: List[Dict[str, str]] = []

    for url, feed in zip(urls, feeds):
        if isinstance(feed, Exception):
            log.warning("Feed fetch failed during search", url=url, error=str(feed))
            continue

        feed_title = feed.feed.get("title", url)
        count = 0

        for entry in feed.entries:
            if count >= max_items_per_feed:
                break

            title = entry.get("title", "").strip()
            summary = _get_summary(entry)
            combined = (title + " " + summary).lower()

            if query in combined:
                all_matches.append({
                    "feed": feed_title,
                    "title": title,
                    "url": entry.get("link", ""),
                    "published": entry.get("published", entry.get("updated", "")),
                    "summary": summary,
                })
                count += 1

    if not all_matches:
        return f"No results found for '{query}' across {len(urls)} feed(s)."

    lines = [f"Search results for '{query}' across {len(urls)} feed(s):\n"]
    for i, m in enumerate(all_matches, 1):
        lines.append(f"[{i}] {m['title']}")
        lines.append(f"    Feed: {m['feed']}")
        if m["published"]:
            lines.append(f"    Published: {m['published']}")
        if m["url"]:
            lines.append(f"    URL: {m['url']}")
        if m["summary"]:
            lines.append(f"    {m['summary']}")
        lines.append("")

    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_summary(entry: Any) -> str:
    """Extract and clean the summary/description from a feed entry."""
    raw = entry.get("summary", entry.get("description", ""))
    if not raw:
        return ""
    text = _strip_html(raw).strip()
    if len(text) > _MAX_SUMMARY_CHARS:
        text = text[:_MAX_SUMMARY_CHARS] + "..."
    return text


def _strip_html(html: str) -> str:
    """Remove HTML tags from a string."""
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser").get_text(separator=" ")
    except ImportError:
        return re.sub(r"<[^>]+>", " ", html)
