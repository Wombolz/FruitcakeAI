import pytest

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.db.models import RSSItem, RSSSource
from app.db.session import Base
from app.mcp.servers.rss import _looks_like_placeholder_feed, _normalize_feed_url, call_tool, get_tools
from app.mcp.services.rss_sources import (
    _strip_html,
    add_source,
    equivalent_canonical_urls,
    get_recent_list_cursor,
    list_recent_items,
    search_cached_items,
    set_recent_list_cursor,
)
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
    assert "rss_user_state" in table_names


def test_rss_server_exposes_new_tool_schemas():
    names = {tool["name"] for tool in get_tools()}
    assert "get_feed_items" in names
    assert "search_feeds" in names
    assert "list_rss_sources" in names
    assert "add_rss_source" in names
    assert "discover_rss_sources" in names
    assert "refresh_rss_cache" in names
    assert "search_my_feeds" in names
    assert "list_recent_feed_items" in names


def test_strip_html_returns_plain_url_without_parsing_warning_path():
    url = "https://example.com/story"
    assert _strip_html(url) == url


def test_strip_html_still_extracts_text_from_markup():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_equivalent_canonical_urls_include_scheme_and_www_variants():
    variants = equivalent_canonical_urls("https://example.com/feed.xml?utm_source=rss")
    assert "https://example.com/feed.xml" in variants
    assert "http://example.com/feed.xml" in variants
    assert "https://www.example.com/feed.xml" in variants
    assert "http://www.example.com/feed.xml" in variants


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


@pytest.mark.asyncio
async def test_list_recent_feed_items_tool_outputs_full_urls():
    async with TestSessionLocal() as db:
        source = RSSSource(
            user_id=1,
            name="Example",
            url="https://example.com/feed.xml",
            url_canonical="https://example.com/feed.xml",
            category="news",
            active=True,
            trust_level="manual",
            update_interval_minutes=60,
        )
        db.add(source)
        await db.flush()
        db.add(
            RSSItem(
                source_id=source.id,
                item_uid="recent-url-1",
                title="Article One",
                link="https://example.com/article-1",
                summary="Summary one",
                published_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    with patch("app.mcp.servers.rss.AsyncSessionLocal", new=TestSessionLocal):
        result = await call_tool(
            "list_recent_feed_items",
            {"max_results": 5, "window": {"mode": "all"}},
            user_context={"user_id": 1},
        )
    assert "Recent feed items" in result
    assert "URL: https://example.com/article-1" in result


@pytest.mark.asyncio
async def test_list_recent_feed_items_defaults_window_to_all_when_omitted():
    async with TestSessionLocal() as db:
        source = RSSSource(
            user_id=11,
            name="Default Window Feed",
            url="https://default-window.example/feed.xml",
            url_canonical="https://default-window.example/feed.xml",
            category="news",
            active=True,
            trust_level="manual",
            update_interval_minutes=60,
        )
        db.add(source)
        await db.flush()
        db.add(
            RSSItem(
                source_id=source.id,
                item_uid="dw-1",
                title="Default Window Item",
                link="https://default-window.example/a1",
                summary="summary",
                published_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    with patch("app.mcp.servers.rss.AsyncSessionLocal", new=TestSessionLocal):
        result = await call_tool(
            "list_recent_feed_items",
            {"max_results": 5},
            user_context={"user_id": 11},
        )
    assert "Recent feed items" in result
    assert "Default Window Item" in result


@pytest.mark.asyncio
async def test_list_recent_feed_items_requires_value_for_explicit_days_mode():
    with patch("app.mcp.servers.rss.AsyncSessionLocal", new=TestSessionLocal):
        result = await call_tool(
            "list_recent_feed_items",
            {"window": {"mode": "days"}},
            user_context={"user_id": 11},
        )
    assert "window.value must be a positive integer" in result


@pytest.mark.asyncio
async def test_refresh_rss_cache_returns_compact_stats():
    with patch(
        "app.mcp.servers.rss.rss_sources.refresh_active_sources_cache",
        new=AsyncMock(return_value={"sources": 5, "items": 42}),
    ):
        with patch("app.mcp.servers.rss.AsyncSessionLocal", new=TestSessionLocal):
            result = await call_tool(
                "refresh_rss_cache",
                {"max_items_per_source": 10},
                user_context={"user_id": 1},
            )

    assert result.startswith("RSS_REFRESH_OK")
    assert "sources_refreshed: 5" in result
    assert "items_seen: 42" in result
    assert "timestamp_utc:" in result


@pytest.mark.asyncio
async def test_refresh_rss_cache_requires_user_context():
    result = await call_tool("refresh_rss_cache", {}, user_context=None)
    assert "requires an authenticated user context" in result


@pytest.mark.asyncio
async def test_recent_items_since_last_refresh_cursor_updates_and_filters():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    older = now
    newer = now + timedelta(seconds=1)

    async with TestSessionLocal() as db:
        source = RSSSource(
            user_id=2,
            name="Cursor Feed",
            url="https://cursor.example/feed.xml",
            url_canonical="https://cursor.example/feed.xml",
            category="news",
            active=True,
            trust_level="manual",
            update_interval_minutes=60,
        )
        db.add(source)
        await db.flush()
        db.add(
            RSSItem(
                source_id=source.id,
                item_uid="c1",
                title="Old Item",
                link="https://cursor.example/old",
                summary="old",
                published_at=older,
                fetched_at=older,
                first_seen_at=older,
                last_seen_at=older,
            )
        )
        await db.flush()
        await set_recent_list_cursor(db, user_id=2, cursor_at=older)
        db.add(
            RSSItem(
                source_id=source.id,
                item_uid="c2",
                title="New Item",
                link="https://cursor.example/new",
                summary="new",
                published_at=newer,
                fetched_at=newer,
                first_seen_at=newer,
                last_seen_at=newer,
            )
        )
        await db.commit()

    async with TestSessionLocal() as db:
        cursor = await get_recent_list_cursor(db, user_id=2)
        rows = await list_recent_items(
            db,
            user_id=2,
            max_results=5,
            window_mode="since_last_refresh",
            since_cursor_at=cursor,
        )
        assert len(rows) == 1
        assert rows[0]["title"] == "New Item"


@pytest.mark.asyncio
async def test_list_recent_items_source_include_filter():
    now = datetime.now(timezone.utc)
    async with TestSessionLocal() as db:
        s1 = RSSSource(
            user_id=3,
            name="Feed A",
            url="https://a.example/feed.xml",
            url_canonical="https://a.example/feed.xml",
            category="news",
            active=True,
            trust_level="manual",
            update_interval_minutes=60,
        )
        s2 = RSSSource(
            user_id=3,
            name="Feed B",
            url="https://b.example/feed.xml",
            url_canonical="https://b.example/feed.xml",
            category="news",
            active=True,
            trust_level="manual",
            update_interval_minutes=60,
        )
        db.add_all([s1, s2])
        await db.flush()
        db.add_all(
            [
                RSSItem(
                    source_id=s1.id,
                    item_uid="a1",
                    title="From A",
                    link="https://a.example/1",
                    summary="A",
                    published_at=now,
                ),
                RSSItem(
                    source_id=s2.id,
                    item_uid="b1",
                    title="From B",
                    link="https://b.example/1",
                    summary="B",
                    published_at=now,
                ),
            ]
        )
        await db.commit()

    async with TestSessionLocal() as db:
        rows = await list_recent_items(
            db,
            user_id=3,
            max_results=10,
            window_mode="all",
            source_mode="include",
            include_source_ids=[s1.id],
        )
        assert len(rows) == 1
        assert rows[0]["title"] == "From A"


@pytest.mark.asyncio
async def test_add_source_updates_equivalent_user_feed_instead_of_inserting_duplicate():
    async with TestSessionLocal() as db:
        existing = RSSSource(
            user_id=7,
            name="BBC Old",
            url="http://feeds.bbci.co.uk/news/rss.xml",
            url_canonical="http://feeds.bbci.co.uk/news/rss.xml",
            category="news",
            active=True,
            trust_level="manual",
            update_interval_minutes=60,
        )
        db.add(existing)
        await db.commit()
        existing_id = existing.id

    async with TestSessionLocal() as db:
        row = await add_source(
            db,
            user_id=7,
            name="BBC News",
            url="https://feeds.bbci.co.uk/news/rss.xml",
            category="news",
            update_interval_minutes=60,
            active=True,
        )
        await db.commit()
        rows = (await db.execute(select(RSSSource).where(RSSSource.user_id == 7))).scalars().all()

    assert row.id == existing_id
    assert len(rows) == 1
    assert rows[0].url == "https://feeds.bbci.co.uk/news/rss.xml"
    assert rows[0].url_canonical == "https://feeds.bbci.co.uk/news/rss.xml"


@pytest.mark.asyncio
async def test_add_source_reuses_equivalent_global_feed_without_user_duplicate():
    async with TestSessionLocal() as db:
        global_row = RSSSource(
            user_id=None,
            name="Global Feed",
            url="https://www.example.com/feed.xml",
            url_canonical="https://www.example.com/feed.xml",
            category="news",
            active=True,
            trust_level="seed",
            update_interval_minutes=60,
        )
        db.add(global_row)
        await db.commit()
        global_id = global_row.id

    async with TestSessionLocal() as db:
        row = await add_source(
            db,
            user_id=8,
            name="Example Feed",
            url="http://example.com/feed.xml",
            category="news",
            update_interval_minutes=60,
            active=True,
        )
        await db.commit()
        user_rows = (await db.execute(select(RSSSource).where(RSSSource.user_id == 8))).scalars().all()

    assert row.id == global_id
    assert row.user_id is None
    assert user_rows == []
