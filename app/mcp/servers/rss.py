"""
FruitcakeAI v5 — RSS MCP Server (internal_python)

Tools exposed to the agent:
  get_feed_items                — Fetch and return recent items from an RSS/Atom feed URL
  search_feeds                  — Fetch multiple feeds concurrently and search for a keyword
  list_rss_sources              — List effective (global + user override) feed sources
  add_rss_source                — Add or update a user RSS source
  remove_rss_source             — Remove a user-owned source
  discover_rss_sources          — Discover candidate feeds from a site URL (queued pending)
  list_rss_source_candidates    — List candidate feeds pending/approved/rejected
  approve_rss_source_candidate  — Approve candidate and create active source
  reject_rss_source_candidate   — Reject candidate with reason
  refresh_rss_cache             — Refresh active RSS cache and return stats only
  list_recent_feed_items        — List recent articles with configurable window/source filters
  search_my_feeds               — Search user active feed catalog
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlsplit

import structlog

from app.db.session import AsyncSessionLocal
from app.mcp.services import rss_sources

log = structlog.get_logger(__name__)

_MAX_ITEMS = 20          # Hard cap per feed
_MAX_SUMMARY_CHARS = 400 # Truncate long summaries
_URL_SAFE_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~:/?#[]@!$&'()*+,;=%"
)
_PLACEHOLDER_HOST_SUFFIXES = (
    ".example.com",
    ".example.org",
    ".example.net",
)
_PLACEHOLDER_HOSTS = {
    "example.com",
    "example.org",
    "example.net",
    "news.example.com",
}
_PLACEHOLDER_HOST_SUBSTRINGS = (
    "example",
    "placeholder",
    "dummy",
    "fake-feed",
    "test-feed",
    "yet-another-source",
    "another-example",
)


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
            "url": {"type": "string", "description": "The RSS or Atom feed URL"},
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

_LIST_RSS_SOURCES_SCHEMA: Dict[str, Any] = {
    "name": "list_rss_sources",
    "description": "List active and inactive RSS sources available to the current user.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "active_only": {"type": "boolean", "default": False},
            "category": {"type": "string"},
        },
    },
}

_ADD_RSS_SOURCE_SCHEMA: Dict[str, Any] = {
    "name": "add_rss_source",
    "description": "Add or update a user RSS feed source.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "url": {"type": "string"},
            "category": {"type": "string", "default": "news"},
            "update_interval_minutes": {"type": "integer", "default": 60},
            "active": {"type": "boolean", "default": True},
        },
        "required": ["name", "url"],
    },
}

_REMOVE_RSS_SOURCE_SCHEMA: Dict[str, Any] = {
    "name": "remove_rss_source",
    "description": "Remove a user-owned RSS feed source.",
    "inputSchema": {
        "type": "object",
        "properties": {"source_id": {"type": "integer"}},
        "required": ["source_id"],
    },
}

_DISCOVER_RSS_SCHEMA: Dict[str, Any] = {
    "name": "discover_rss_sources",
    "description": "Discover RSS/Atom feeds from a site URL and queue candidates for approval.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "seed_url": {"type": "string"},
            "max_candidates": {"type": "integer", "default": 10},
        },
        "required": ["seed_url"],
    },
}

_LIST_RSS_CANDIDATES_SCHEMA: Dict[str, Any] = {
    "name": "list_rss_source_candidates",
    "description": "List RSS source candidates discovered for this user.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "pending|approved|rejected"},
        },
    },
}

_APPROVE_RSS_CANDIDATE_SCHEMA: Dict[str, Any] = {
    "name": "approve_rss_source_candidate",
    "description": "Approve a pending RSS candidate and create an active source.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "integer"},
            "name": {"type": "string"},
            "category": {"type": "string", "default": "news"},
        },
        "required": ["candidate_id"],
    },
}

_REJECT_RSS_CANDIDATE_SCHEMA: Dict[str, Any] = {
    "name": "reject_rss_source_candidate",
    "description": "Reject a pending RSS candidate.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "integer"},
            "reason": {"type": "string", "default": "Rejected by user"},
        },
        "required": ["candidate_id"],
    },
}

_SEARCH_MY_FEEDS_SCHEMA: Dict[str, Any] = {
    "name": "search_my_feeds",
    "description": (
        "Search across active RSS sources in the user's curated catalog. "
        "If query is omitted/empty, returns most recent cached headlines."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional query text; empty means return most recent headlines.",
            },
            "max_results": {"type": "integer", "default": 10},
            "category": {"type": "string"},
            "refresh": {"type": "boolean", "default": False},
            "days_back": {"type": "integer", "default": 7},
        },
    },
}

_LIST_RECENT_FEED_ITEMS_SCHEMA: Dict[str, Any] = {
    "name": "list_recent_feed_items",
    "description": (
        "List recent RSS articles with title, published time, source, summary, and full URL. "
        "Use this for latest/since-last-refresh article lists. "
        "Prefer search_my_feeds for keyword/topic search."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "max_results": {"type": "integer", "default": 5},
            "window": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["hours", "days", "weeks", "since_last_refresh", "all"],
                        "default": "all",
                    },
                    "value": {"type": "integer", "description": "Required for hours/days/weeks."},
                },
            },
            "sources": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["all", "category", "include", "exclude"],
                        "default": "all",
                    },
                    "category": {"type": "string"},
                    "include_source_ids": {"type": "array", "items": {"type": "integer"}},
                    "exclude_source_ids": {"type": "array", "items": {"type": "integer"}},
                },
            },
            "refresh": {"type": "boolean", "default": False},
            "mark_cursor": {"type": "boolean", "default": True},
        },
    },
}

_REFRESH_RSS_CACHE_SCHEMA: Dict[str, Any] = {
    "name": "refresh_rss_cache",
    "description": (
        "Refresh active RSS sources for the current user and return compact maintenance stats only. "
        "Use this for maintenance tasks that should avoid article content output."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "max_items_per_source": {"type": "integer", "default": 20},
        },
    },
}


# ── Public MCP interface ──────────────────────────────────────────────────────

def get_tools() -> List[Dict[str, Any]]:
    return [
        _GET_FEED_ITEMS_SCHEMA,
        _SEARCH_FEEDS_SCHEMA,
        _LIST_RSS_SOURCES_SCHEMA,
        _ADD_RSS_SOURCE_SCHEMA,
        _REMOVE_RSS_SOURCE_SCHEMA,
        _DISCOVER_RSS_SCHEMA,
        _LIST_RSS_CANDIDATES_SCHEMA,
        _APPROVE_RSS_CANDIDATE_SCHEMA,
        _REJECT_RSS_CANDIDATE_SCHEMA,
        _REFRESH_RSS_CACHE_SCHEMA,
        _LIST_RECENT_FEED_ITEMS_SCHEMA,
        _SEARCH_MY_FEEDS_SCHEMA,
    ]


async def call_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    user_context: Any = None,
) -> str:
    if tool_name == "get_feed_items":
        return await _get_feed_items(arguments)
    if tool_name == "search_feeds":
        return await _search_feeds(arguments, user_context)
    if tool_name == "list_rss_sources":
        return await _list_rss_sources(arguments, user_context)
    if tool_name == "add_rss_source":
        return await _add_rss_source(arguments, user_context)
    if tool_name == "remove_rss_source":
        return await _remove_rss_source(arguments, user_context)
    if tool_name == "discover_rss_sources":
        return await _discover_rss_sources(arguments, user_context)
    if tool_name == "list_rss_source_candidates":
        return await _list_rss_source_candidates(arguments, user_context)
    if tool_name == "approve_rss_source_candidate":
        return await _approve_rss_source_candidate(arguments, user_context)
    if tool_name == "reject_rss_source_candidate":
        return await _reject_rss_source_candidate(arguments, user_context)
    if tool_name == "refresh_rss_cache":
        return await _refresh_rss_cache(arguments, user_context)
    if tool_name == "list_recent_feed_items":
        return await _list_recent_feed_items(arguments, user_context)
    if tool_name == "search_my_feeds":
        return await _search_my_feeds(arguments, user_context)
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
    raw_url = (arguments.get("url") or "").strip()
    url = _normalize_feed_url(raw_url)
    if not url:
        return f"Invalid feed URL provided: {raw_url or '(empty)'}"
    if _looks_like_placeholder_feed(url):
        return (
            "Placeholder/demo feed URL detected. Use a real feed URL from "
            "list_rss_sources or run search_my_feeds instead."
        )

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

async def _search_feeds(arguments: Dict[str, Any], user_context: Any = None) -> str:
    raw_urls = arguments.get("urls") or []
    urls = [_normalize_feed_url(u) for u in raw_urls]
    urls = [u for u in urls if u]
    urls = [u for u in urls if not _looks_like_placeholder_feed(u)]
    query = (arguments.get("query") or "").strip().lower()
    max_items_per_feed = min(int(arguments.get("max_items_per_feed", 5)), _MAX_ITEMS)

    if not query:
        return "No search query provided."
    if not urls:
        # Auto-recover to curated feed search when URL inputs are unusable.
        if _get_user_id(user_context):
            return await _search_my_feeds(
                {
                    "query": query,
                    "max_results": max_items_per_feed * 2,
                    "refresh": False,
                },
                user_context,
            )
        return "No valid feed URLs provided. Provide real feed URLs or use search_my_feeds with a query."

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
                all_matches.append(
                    {
                        "feed": feed_title,
                        "title": title,
                        "url": entry.get("link", ""),
                        "published": entry.get("published", entry.get("updated", "")),
                        "summary": summary,
                    }
                )
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


# ── source catalog tools ──────────────────────────────────────────────────────

async def _list_rss_sources(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "list_rss_sources requires an authenticated user context."

    active_only = bool(arguments.get("active_only", False))
    category = (arguments.get("category") or "").strip() or None

    async with AsyncSessionLocal() as db:
        sources = await rss_sources.list_effective_sources(
            db, user_id=user_id, active_only=active_only, category=category
        )

    if not sources:
        return "No RSS sources configured."

    lines = [f"RSS sources ({len(sources)}):\n"]
    for i, src in enumerate(sources, 1):
        lines.append(f"[{i}] {src['name']}")
        lines.append(f"    Scope: {src['scope']}")
        lines.append(f"    Category: {src['category']} | Active: {src['active']}")
        lines.append(f"    URL: {src['url']}")
        lines.append("")
    return "\n".join(lines)


async def _add_rss_source(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "add_rss_source requires an authenticated user context."

    name = (arguments.get("name") or "").strip()
    url = (arguments.get("url") or "").strip()
    if not name or not url:
        return "Both name and url are required."

    category = (arguments.get("category") or "news").strip() or "news"
    update_interval = int(arguments.get("update_interval_minutes", 60))
    active = bool(arguments.get("active", True))

    try:
        async with AsyncSessionLocal() as db:
            row = await rss_sources.add_source(
                db,
                user_id=user_id,
                name=name,
                url=url,
                category=category,
                update_interval_minutes=update_interval,
                active=active,
            )
            await db.commit()
    except ValueError as e:
        return str(e)

    return f"Saved RSS source '{row.name}' (id={row.id}, active={row.active})."


async def _remove_rss_source(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "remove_rss_source requires an authenticated user context."

    source_id = int(arguments.get("source_id", 0))
    if source_id <= 0:
        return "source_id is required."

    async with AsyncSessionLocal() as db:
        ok = await rss_sources.remove_source(db, user_id=user_id, source_id=source_id)
        if ok:
            await db.commit()
            return f"Removed source {source_id}."
        return "Source not found or not owned by the user."


async def _discover_rss_sources(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "discover_rss_sources requires an authenticated user context."

    seed_url = (arguments.get("seed_url") or "").strip()
    max_candidates = max(1, min(int(arguments.get("max_candidates", 10)), 25))
    if not seed_url:
        return "seed_url is required."

    try:
        async with AsyncSessionLocal() as db:
            queued = await rss_sources.queue_discovered_candidates(
                db,
                user_id=user_id,
                seed_url=seed_url,
                max_candidates=max_candidates,
            )
            await db.commit()
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Discovery failed: {e}"

    if not queued:
        return "No new feed candidates discovered."

    lines = [f"Queued {len(queued)} candidate feed(s) for review:\n"]
    for i, c in enumerate(queued, 1):
        lines.append(f"[{i}] id={c.id} {c.url}")
    return "\n".join(lines)


async def _list_rss_source_candidates(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "list_rss_source_candidates requires an authenticated user context."

    status = (arguments.get("status") or "").strip() or None

    async with AsyncSessionLocal() as db:
        rows = await rss_sources.list_candidates(db, user_id=user_id, status=status)

    if not rows:
        return "No RSS source candidates found."

    lines = [f"RSS source candidates ({len(rows)}):\n"]
    for i, row in enumerate(rows, 1):
        lines.append(f"[{i}] id={row.id} [{row.status}] {row.url}")
        if row.title_hint:
            lines.append(f"    Title hint: {row.title_hint}")
        if row.reason:
            lines.append(f"    Reason: {row.reason}")
    return "\n".join(lines)


async def _approve_rss_source_candidate(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "approve_rss_source_candidate requires an authenticated user context."

    candidate_id = int(arguments.get("candidate_id", 0))
    if candidate_id <= 0:
        return "candidate_id is required."

    name = (arguments.get("name") or "").strip() or None
    category = (arguments.get("category") or "news").strip() or "news"

    try:
        async with AsyncSessionLocal() as db:
            source = await rss_sources.approve_candidate(
                db,
                user_id=user_id,
                candidate_id=candidate_id,
                reviewer_id=user_id,
                name=name,
                category=category,
            )
            await db.commit()
    except ValueError as e:
        return str(e)

    return f"Approved candidate {candidate_id}; source '{source.name}' created (id={source.id})."


async def _reject_rss_source_candidate(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "reject_rss_source_candidate requires an authenticated user context."

    candidate_id = int(arguments.get("candidate_id", 0))
    if candidate_id <= 0:
        return "candidate_id is required."

    reason = (arguments.get("reason") or "Rejected by user").strip() or "Rejected by user"

    try:
        async with AsyncSessionLocal() as db:
            row = await rss_sources.reject_candidate(
                db,
                user_id=user_id,
                candidate_id=candidate_id,
                reviewer_id=user_id,
                reason=reason,
            )
            await db.commit()
    except ValueError as e:
        return str(e)

    return f"Rejected candidate {row.id}."


async def _search_my_feeds(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "search_my_feeds requires an authenticated user context."

    query = (arguments.get("query") or "").strip()
    using_recent_fallback = False
    max_results = max(1, min(int(arguments.get("max_results", 10)), 25))
    category = (arguments.get("category") or "").strip() or None
    refresh = bool(arguments.get("refresh", False))
    days_back = int(arguments.get("days_back", 7))

    if not query:
        # Treat empty query as "most recent" to avoid wasteful retries and
        # make headline-style requests work without strict phrasing.
        query = "latest"
        using_recent_fallback = True

    async with AsyncSessionLocal() as db:
        cached = await rss_sources.search_cached_items(
            db,
            user_id=user_id,
            query=query,
            max_results=max_results,
            category=category,
            days_back=days_back,
        )
        if cached and not refresh:
            prefix = "No query provided; showing most recent cached headlines.\n\n" if using_recent_fallback else ""
            return prefix + _format_cached_results(query, cached, used_cache_only=True)

        refreshed = await rss_sources.refresh_active_sources_cache(
            db,
            user_id=user_id,
            category=category,
            max_items_per_source=20,
        )
        await db.commit()

        cached = await rss_sources.search_cached_items(
            db,
            user_id=user_id,
            query=query,
            max_results=max_results,
            category=category,
            days_back=days_back,
        )
        if not cached:
            if refreshed["sources"] == 0:
                return "No active RSS sources available. Add or approve sources first."
            if using_recent_fallback:
                return (
                    f"No recent cached headlines found after refreshing "
                    f"{refreshed['sources']} source(s)."
                )
            return f"No cached results found for '{query}' after refreshing {refreshed['sources']} source(s)."
        prefix = "No query provided; showing most recent cached headlines.\n\n" if using_recent_fallback else ""
        return prefix + _format_cached_results(
            query, cached, used_cache_only=False, refreshed_sources=refreshed["sources"]
        )


async def _refresh_rss_cache(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "refresh_rss_cache requires an authenticated user context."

    category = (arguments.get("category") or "").strip() or None
    try:
        max_items_per_source = max(1, min(int(arguments.get("max_items_per_source", 20)), 50))
    except Exception:
        max_items_per_source = 20

    async with AsyncSessionLocal() as db:
        refreshed = await rss_sources.refresh_active_sources_cache(
            db,
            user_id=user_id,
            category=category,
            max_items_per_source=max_items_per_source,
        )
        await db.commit()

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return (
        "RSS_REFRESH_OK\n"
        f"sources_refreshed: {int(refreshed.get('sources') or 0)}\n"
        f"items_seen: {int(refreshed.get('items') or 0)}\n"
        f"timestamp_utc: {now}"
    )


async def _list_recent_feed_items(arguments: Dict[str, Any], user_context: Any) -> str:
    user_id = _get_user_id(user_context)
    if not user_id:
        return "list_recent_feed_items requires an authenticated user context."

    max_results = max(1, min(int(arguments.get("max_results", 5)), 50))
    refresh = bool(arguments.get("refresh", False))
    mark_cursor = bool(arguments.get("mark_cursor", True))

    raw_window = arguments.get("window")
    window = raw_window or {}
    window_mode = str(window.get("mode", "all")).strip().lower() or "all"
    window_value_raw = window.get("value")
    window_value = int(window_value_raw) if window_value_raw is not None else None

    sources = arguments.get("sources") or {}
    source_mode = str(sources.get("mode", "all")).strip().lower() or "all"
    source_category = (sources.get("category") or "").strip() or None
    include_source_ids = [int(x) for x in (sources.get("include_source_ids") or []) if int(x) > 0]
    exclude_source_ids = [int(x) for x in (sources.get("exclude_source_ids") or []) if int(x) > 0]

    if window_mode in {"hours", "days", "weeks"} and (window_value is None or window_value <= 0):
        return "window.value must be a positive integer when window.mode is hours/days/weeks."
    if window_mode not in {"hours", "days", "weeks", "since_last_refresh", "all"}:
        return "window.mode must be one of: hours, days, weeks, since_last_refresh, all."
    if source_mode not in {"all", "category", "include", "exclude"}:
        return "sources.mode must be one of: all, category, include, exclude."

    async with AsyncSessionLocal() as db:
        if refresh:
            refreshed = await rss_sources.refresh_active_sources_cache(
                db,
                user_id=user_id,
                category=source_category if source_mode == "category" else None,
                max_items_per_source=20,
            )
            await db.commit()
        else:
            refreshed = None

        cursor_at = None
        effective_mode = window_mode
        effective_value = window_value
        if window_mode == "since_last_refresh":
            cursor_at = await rss_sources.get_recent_list_cursor(db, user_id=user_id)
            if cursor_at is None:
                # First-run fallback for predictable output.
                effective_mode = "days"
                effective_value = 7

        rows = await rss_sources.list_recent_items(
            db,
            user_id=user_id,
            max_results=max_results,
            window_mode=effective_mode,
            window_value=effective_value,
            source_mode=source_mode,
            source_category=source_category,
            include_source_ids=include_source_ids,
            exclude_source_ids=exclude_source_ids,
            since_cursor_at=cursor_at,
        )

        if not rows:
            if window_mode == "since_last_refresh":
                return "No new items since last refresh."
            return "No recent feed items found for the selected filters."

        if mark_cursor:
            newest = max((_recent_item_timestamp(r) for r in rows), default=None)
            if newest is not None:
                await rss_sources.set_recent_list_cursor(db, user_id=user_id, cursor_at=newest)
                await db.commit()

    return _format_recent_items_results(
        rows,
        mode=window_mode,
        refreshed_sources=(refreshed or {}).get("sources"),
    )


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


def _normalize_feed_url(raw: str) -> str:
    """
    Best-effort URL cleanup to handle contaminated tool arguments.
    Extracts a URL prefix and drops trailing non-URL text.
    """
    value = (raw or "").strip()
    if not value:
        return ""

    m = re.search(r"https?://", value)
    if m:
        value = value[m.start() :]

    end = 0
    for ch in value:
        if ch in _URL_SAFE_CHARS:
            end += 1
            continue
        break
    candidate = value[:end].rstrip(".,);]")
    if not candidate:
        return ""

    try:
        parsed = urlsplit(candidate)
    except Exception:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""

    return candidate


def _get_user_id(user_context: Any) -> int | None:
    if user_context is None:
        return None
    if isinstance(user_context, dict):
        value = user_context.get("user_id")
    else:
        value = getattr(user_context, "user_id", None)
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _looks_like_placeholder_feed(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in _PLACEHOLDER_HOSTS:
        return True
    if any(part in host for part in _PLACEHOLDER_HOST_SUBSTRINGS):
        return True
    return any(host.endswith(suffix) for suffix in _PLACEHOLDER_HOST_SUFFIXES)


def _format_cached_results(
    query: str,
    rows: List[Dict[str, str]],
    *,
    used_cache_only: bool,
    refreshed_sources: int = 0,
) -> str:
    mode = "cache-only" if used_cache_only else f"cache after refreshing {refreshed_sources} source(s)"
    lines = [f"Cached feed results for '{query}' ({mode}):\n"]
    for i, row in enumerate(rows, 1):
        lines.append(f"[{i}] {row.get('title') or '(no title)'}")
        feed = row.get("feed") or ""
        if feed:
            lines.append(f"    Feed: {feed}")
        published = row.get("published") or ""
        if published:
            lines.append(f"    Published: {published}")
        url = row.get("url") or ""
        if url:
            lines.append(f"    URL: {url}")
        summary = row.get("summary") or ""
        if summary:
            lines.append(f"    {summary}")
        lines.append("")
    return "\n".join(lines)


def _recent_item_timestamp(row: Dict[str, str]) -> datetime | None:
    text = (row.get("published") or "").strip() or (row.get("fetched_at") or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _format_recent_items_results(
    rows: List[Dict[str, str]],
    *,
    mode: str,
    refreshed_sources: int | None = None,
) -> str:
    if mode == "since_last_refresh":
        title = f"Recent feed items since last refresh ({len(rows)}):"
    else:
        title = f"Recent feed items ({len(rows)}):"
    if refreshed_sources is not None:
        title += f" [refreshed {refreshed_sources} source(s)]"

    lines = [title, ""]
    for i, row in enumerate(rows, 1):
        lines.append(f"[{i}] {row.get('title') or '(no title)'}")
        feed = row.get("feed") or ""
        if feed:
            lines.append(f"    Source: {feed}")
        published = row.get("published") or ""
        if published:
            lines.append(f"    Published: {published}")
        summary = row.get("summary") or ""
        if summary:
            lines.append(f"    Summary: {summary}")
        url = row.get("url") or ""
        if url:
            lines.append(f"    URL: {url}")
        lines.append("")
    return "\n".join(lines)
