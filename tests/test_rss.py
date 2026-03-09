import pytest

from datetime import datetime, timezone
from unittest.mock import patch
from unittest.mock import AsyncMock

from app.db.models import RSSItem, RSSSource
from app.db.session import Base
from app.mcp.servers.rss import _looks_like_placeholder_feed, _normalize_feed_url, call_tool, get_tools
from app.mcp.services.rss_sources import search_cached_items
from tests.conftest import TestSessionLocal


def test_normalize_feed_url_valid():
    url = "https://example.com/feed.xml?x=1"
    assert _normalize_feed_url(url) == url


def test_normalize_feed_url_trims_appended_non_url_text():
    raw = (
        "http://hosted2.ap.org/APDEFAULT/f4b9dc0d8f1a46c7afae5eb12df33fda/"
        "BRDHiHPE.xml?xpgw下次输入从Reuters和AP获取新闻提要的get_feed_items函数调用。"
    )
    assert _normalize_feed_url(raw) == (
        "http://hosted2.ap.org/APDEFAULT/f4b9dc0d8f1a46c7afae5eb12df33fda/"
        "BRDHiHPE.xml?xpgw"
    )


def test_normalize_feed_url_rejects_non_url():
    assert _normalize_feed_url("not a url") == ""


def test_rss_models_registered_in_metadata():
    table_names = set(Base.metadata.tables.keys())
    assert "rss_sources" in table_names
    assert "rss_source_candidates" in table_names
    assert "rss_items" in table_names


def test_rss_server_exposes_new_tool_schemas():
    names = {tool["name"] for tool in get_tools()}
    assert "get_feed_items" in names
    assert "search_feeds" in names
    assert "list_rss_sources" in names
    assert "add_rss_source" in names
    assert "discover_rss_sources" in names
    assert "search_my_feeds" in names


def test_search_my_feeds_schema_requires_non_empty_query():
    tool = next(t for t in get_tools() if t["name"] == "search_my_feeds")
    query_schema = tool["inputSchema"]["properties"]["query"]
    assert "minLength" not in query_schema
    assert "most recent cached headlines" in tool["description"].lower()


@pytest.mark.asyncio
async def test_list_rss_sources_requires_user_context():
    result = await call_tool("list_rss_sources", {}, user_context=None)
    assert "requires an authenticated user context" in result


@pytest.mark.asyncio
async def test_get_feed_items_rejects_placeholder_feed_url():
    result = await call_tool(
        "get_feed_items",
        {"url": "https://news.example.com/rss"},
        user_context={"user_id": 1},
    )
    assert "Placeholder/demo feed URL detected" in result
    assert "search_my_feeds" in result


@pytest.mark.asyncio
async def test_search_feeds_ignores_placeholder_urls():
    result = await call_tool(
        "search_feeds",
        {"urls": ["https://news.example.com/rss"], "query": "market"},
        user_context=None,
    )
    assert "No valid feed URLs provided." in result


@pytest.mark.asyncio
async def test_search_feeds_invalid_urls_falls_back_to_search_my_feeds():
    with patch(
        "app.mcp.servers.rss._search_my_feeds",
        new=AsyncMock(return_value="FALLBACK_OK"),
    ) as fallback:
        result = await call_tool(
            "search_feeds",
            {
                "urls": ["https://news.example.com/rss", "https://example-rss-feed.com/feed"],
                "query": "market",
                "max_items_per_feed": 4,
            },
            user_context={"user_id": 1},
        )

    assert result == "FALLBACK_OK"
    fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_feeds_still_requires_query():
    result = await call_tool(
        "search_feeds",
        {"urls": ["https://news.example.com/rss"], "query": "   "},
        user_context={"user_id": 1},
    )
    assert result == "No search query provided."


def test_placeholder_detection_blocks_synthetic_feed_domains():
    assert _looks_like_placeholder_feed("https://example-rss-feed.com/feed")
    assert _looks_like_placeholder_feed("https://another-example-rss-feed.net/rss.xml")
    assert _looks_like_placeholder_feed("https://yet-another-source.org/newsfeed.atom")


@pytest.mark.asyncio
async def test_search_my_feeds_rejects_empty_query():
    with patch("app.mcp.servers.rss.AsyncSessionLocal", new=TestSessionLocal):
        result = await call_tool(
            "search_my_feeds",
            {"query": "   ", "max_results": 5},
            user_context={"user_id": 1},
        )
    assert (
        "most recent cached headlines" in result.lower()
        or "no active rss sources" in result.lower()
        or "no recent cached headlines found" in result.lower()
    )


@pytest.mark.asyncio
async def test_search_cached_items_temporal_query_returns_latest_items():
    async with TestSessionLocal() as db:
        source = RSSSource(
            user_id=None,
            name="Example",
            url="https://example.com/feed.xml",
            url_canonical="https://example.com/feed.xml",
            category="news",
            active=True,
            trust_level="seed",
            update_interval_minutes=60,
        )
        db.add(source)
        await db.flush()

        db.add(
            RSSItem(
                source_id=source.id,
                item_uid="abc123",
                title="Breaking market move",
                link="https://example.com/story",
                summary="Stocks moved on new policy updates.",
                published_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

        rows = await search_cached_items(
            db,
            user_id=999,
            query="today",
            max_results=5,
            category="news",
            days_back=7,
        )

    assert len(rows) >= 1
    assert rows[0]["title"] == "Breaking market move"
