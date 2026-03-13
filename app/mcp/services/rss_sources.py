from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RSSItem, RSSSource, RSSSourceCandidate, RSSUserState
from app.mcp.services.rss_seed import DEFAULT_GLOBAL_RSS_SOURCES

_TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
}

_FEED_CONTENT_MARKERS = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")
_FEED_PATH_CANDIDATES = ("/feed", "/rss", "/atom.xml", "/feed.xml", "/rss.xml")
_MAX_SUMMARY_CHARS = 400
_DEFAULT_RETENTION_DAYS = 60
_QUERY_STOPWORDS = {
    "today",
    "yesterday",
    "tomorrow",
    "latest",
    "recent",
    "news",
    "headline",
    "headlines",
    "update",
    "updates",
}


def canonicalize_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        return ""

    try:
        parsed = urlsplit(value)
    except Exception:
        return ""

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = host
    elif port:
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")

    kept_qs = []
    for key, val in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _TRACKING_QUERY_KEYS:
            continue
        kept_qs.append((key, val))

    query = urlencode(kept_qs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def extract_domain(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


async def ensure_default_sources(db: AsyncSession) -> None:
    existing_global = await db.execute(select(RSSSource.id).where(RSSSource.user_id.is_(None)).limit(1))
    if existing_global.scalar_one_or_none() is not None:
        return

    for row in DEFAULT_GLOBAL_RSS_SOURCES:
        canonical = canonicalize_url(row["url"])
        if not canonical:
            continue
        db.add(
            RSSSource(
                user_id=None,
                name=row["name"],
                url=row["url"],
                url_canonical=canonical,
                category=row.get("category", "news"),
                active=True,
                trust_level="seed",
                update_interval_minutes=60,
            )
        )
    await db.flush()


async def list_effective_sources(
    db: AsyncSession,
    user_id: int,
    *,
    active_only: bool = False,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    await ensure_default_sources(db)

    q = select(RSSSource).where(or_(RSSSource.user_id == user_id, RSSSource.user_id.is_(None)))
    if category:
        q = q.where(RSSSource.category == category)
    rows = (await db.execute(q.order_by(RSSSource.user_id.is_(None), RSSSource.name))).scalars().all()

    user_by_canonical: Dict[str, RSSSource] = {
        r.url_canonical: r for r in rows if r.user_id == user_id
    }

    out: List[Dict[str, Any]] = []
    for row in rows:
        if row.user_id is None and row.url_canonical in user_by_canonical:
            continue
        if active_only and not row.active:
            continue
        out.append(_source_to_dict(row, scope="user" if row.user_id else "global"))
    return out


async def add_source(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
    url: str,
    category: str = "news",
    update_interval_minutes: int = 60,
    trust_level: str = "manual",
    active: bool = True,
) -> RSSSource:
    canonical = canonicalize_url(url)
    if not canonical:
        raise ValueError("Invalid RSS/Atom URL.")

    existing = await db.execute(
        select(RSSSource).where(RSSSource.user_id == user_id, RSSSource.url_canonical == canonical)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        row.name = name
        row.url = url
        row.category = category
        row.update_interval_minutes = max(5, min(int(update_interval_minutes), 1440))
        row.trust_level = trust_level
        row.active = active
        row.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return row

    row = RSSSource(
        user_id=user_id,
        name=name,
        url=url,
        url_canonical=canonical,
        category=category,
        active=active,
        trust_level=trust_level,
        update_interval_minutes=max(5, min(int(update_interval_minutes), 1440)),
    )
    db.add(row)
    await db.flush()
    return row


async def remove_source(db: AsyncSession, *, user_id: int, source_id: int) -> bool:
    row = await db.get(RSSSource, source_id)
    if row is None:
        return False
    if row.user_id != user_id:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def set_source_active(db: AsyncSession, *, user_id: int, source_id: int, active: bool) -> RSSSource:
    row = await db.get(RSSSource, source_id)
    if row is None:
        raise ValueError("Source not found")

    if row.user_id == user_id:
        row.active = active
        row.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return row

    if row.user_id is not None:
        raise ValueError("Source belongs to another user")

    # Global row toggle becomes a per-user override.
    existing = await db.execute(
        select(RSSSource).where(RSSSource.user_id == user_id, RSSSource.url_canonical == row.url_canonical)
    )
    override = existing.scalar_one_or_none()
    if override is None:
        override = RSSSource(
            user_id=user_id,
            name=row.name,
            url=row.url,
            url_canonical=row.url_canonical,
            category=row.category,
            active=active,
            trust_level="override",
            update_interval_minutes=row.update_interval_minutes,
        )
        db.add(override)
    else:
        override.active = active
        override.updated_at = datetime.now(timezone.utc)

    await db.flush()
    return override


async def resolve_active_source_urls(
    db: AsyncSession,
    *,
    user_id: int,
    category: Optional[str] = None,
) -> List[str]:
    sources = await list_effective_sources(db, user_id, active_only=True, category=category)
    seen = set()
    urls = []
    for src in sources:
        canonical = src.get("url_canonical")
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        urls.append(src["url"])
    return urls


async def resolve_active_sources(
    db: AsyncSession,
    *,
    user_id: int,
    category: Optional[str] = None,
) -> List[RSSSource]:
    rows = await list_effective_sources(db, user_id=user_id, active_only=True, category=category)
    ids = [row["id"] for row in rows if row.get("id")]
    if not ids:
        return []
    q = select(RSSSource).where(RSSSource.id.in_(ids))
    source_rows = (await db.execute(q)).scalars().all()
    by_id = {row.id: row for row in source_rows}
    return [by_id[i] for i in ids if i in by_id]


async def refresh_active_sources_cache(
    db: AsyncSession,
    *,
    user_id: int,
    category: Optional[str] = None,
    max_items_per_source: int = 20,
) -> Dict[str, Any]:
    sources = await resolve_active_sources(db, user_id=user_id, category=category)
    if not sources:
        return {"sources": 0, "items": 0}

    refreshed_items = 0
    for source in sources:
        try:
            entries = await _fetch_feed_entries(source.url)
            entries = entries[:max(1, min(max_items_per_source, 50))]
            refreshed_items += await upsert_feed_entries(db, source=source, entries=entries)
            source.last_ok_at = datetime.now(timezone.utc)
            source.last_error = None
        except Exception as exc:
            source.last_error = str(exc)
            source.updated_at = datetime.now(timezone.utc)
    await prune_old_items(db, retention_days=_DEFAULT_RETENTION_DAYS)
    await db.flush()
    return {"sources": len(sources), "items": refreshed_items}


async def search_cached_items(
    db: AsyncSession,
    *,
    user_id: int,
    query: str,
    max_results: int = 10,
    category: Optional[str] = None,
    days_back: Optional[int] = None,
) -> List[Dict[str, Any]]:
    sources = await resolve_active_sources(db, user_id=user_id, category=category)
    source_ids = [s.id for s in sources]
    if not source_ids:
        return []

    q = (
        select(RSSItem, RSSSource.name)
        .join(RSSSource, RSSSource.id == RSSItem.source_id)
        .where(RSSItem.source_id.in_(source_ids))
    )
    if days_back and days_back > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        q = q.where(or_(RSSItem.published_at.is_(None), RSSItem.published_at >= cutoff))

    terms = _meaningful_query_terms(query)
    if terms:
        term_filters = []
        for term in terms:
            ilike = f"%{term}%"
            term_filters.append(RSSItem.title.ilike(ilike))
            term_filters.append(RSSItem.summary.ilike(ilike))
        q = q.where(or_(*term_filters))

    q = q.order_by(RSSItem.published_at.desc().nullslast(), RSSItem.fetched_at.desc()).limit(max(1, min(max_results, 100)))
    rows = (await db.execute(q)).all()
    return [
        {
            "title": item.title,
            "url": item.link or "",
            "summary": item.summary or "",
            "published": item.published_at.isoformat() if item.published_at else "",
            "feed": source_name,
            "fetched_at": item.fetched_at.isoformat() if item.fetched_at else "",
        }
        for item, source_name in rows
    ]


async def get_recent_list_cursor(
    db: AsyncSession,
    *,
    user_id: int,
) -> Optional[datetime]:
    row = (
        await db.execute(
            select(RSSUserState).where(RSSUserState.user_id == user_id).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return row.last_list_recent_cursor_at


async def set_recent_list_cursor(
    db: AsyncSession,
    *,
    user_id: int,
    cursor_at: datetime,
) -> RSSUserState:
    row = (
        await db.execute(
            select(RSSUserState).where(RSSUserState.user_id == user_id).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        row = RSSUserState(user_id=user_id, last_list_recent_cursor_at=cursor_at)
        db.add(row)
    else:
        row.last_list_recent_cursor_at = cursor_at
    await db.flush()
    return row


async def list_recent_items(
    db: AsyncSession,
    *,
    user_id: int,
    max_results: int = 5,
    window_mode: str = "days",
    window_value: Optional[int] = 7,
    source_mode: str = "all",
    source_category: Optional[str] = None,
    include_source_ids: Optional[list[int]] = None,
    exclude_source_ids: Optional[list[int]] = None,
    since_cursor_at: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """
    Return recent cached RSS items with configurable window/source filtering.
    Ordered by published_at desc, then fetched_at desc.
    """
    effective_category = source_category if source_mode == "category" else None
    sources = await resolve_active_sources(db, user_id=user_id, category=effective_category)
    source_ids = [s.id for s in sources]
    if not source_ids:
        return []

    include_set = {int(x) for x in (include_source_ids or []) if int(x) > 0}
    exclude_set = {int(x) for x in (exclude_source_ids or []) if int(x) > 0}
    if source_mode == "include":
        source_ids = [sid for sid in source_ids if sid in include_set]
    elif source_mode == "exclude":
        source_ids = [sid for sid in source_ids if sid not in exclude_set]
    if not source_ids:
        return []

    q = (
        select(RSSItem, RSSSource.name, RSSSource.id)
        .join(RSSSource, RSSSource.id == RSSItem.source_id)
        .where(RSSItem.source_id.in_(source_ids))
    )

    now = datetime.now(timezone.utc)
    mode = (window_mode or "days").strip().lower()
    if mode in {"hours", "days", "weeks"}:
        value = int(window_value or 0)
        if value > 0:
            if mode == "hours":
                cutoff = now - timedelta(hours=value)
            elif mode == "weeks":
                cutoff = now - timedelta(weeks=value)
            else:
                cutoff = now - timedelta(days=value)
            q = q.where(or_(RSSItem.published_at >= cutoff, and_(RSSItem.published_at.is_(None), RSSItem.fetched_at >= cutoff)))
    elif mode == "since_last_refresh" and since_cursor_at is not None:
        q = q.where(or_(RSSItem.published_at > since_cursor_at, and_(RSSItem.published_at.is_(None), RSSItem.fetched_at > since_cursor_at)))
    # mode=all applies no additional time filter.

    q = q.order_by(RSSItem.published_at.desc().nullslast(), RSSItem.fetched_at.desc()).limit(max(1, min(max_results, 100)))
    rows = (await db.execute(q)).all()
    return [
        {
            "source_id": source_id,
            "title": item.title,
            "url": item.link or "",
            "summary": item.summary or "",
            "published": item.published_at.isoformat() if item.published_at else "",
            "feed": source_name,
            "fetched_at": item.fetched_at.isoformat() if item.fetched_at else "",
        }
        for item, source_name, source_id in rows
    ]


async def list_candidates(
    db: AsyncSession,
    *,
    user_id: int,
    status: Optional[str] = None,
) -> List[RSSSourceCandidate]:
    q = select(RSSSourceCandidate).where(RSSSourceCandidate.user_id == user_id)
    if status:
        q = q.where(RSSSourceCandidate.status == status)
    q = q.order_by(RSSSourceCandidate.created_at.desc())
    return (await db.execute(q)).scalars().all()


async def discover_candidate_urls(seed_url: str, max_candidates: int = 10) -> List[Dict[str, str]]:
    canonical_seed = canonicalize_url(seed_url)
    if not canonical_seed:
        raise ValueError("Invalid seed_url")

    links: List[Dict[str, str]] = []

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        resp = await client.get(canonical_seed)
        content_type = (resp.headers.get("content-type") or "").lower()

        if any(marker in content_type for marker in _FEED_CONTENT_MARKERS):
            links.append({"url": canonical_seed, "title_hint": ""})
            return links

        html = resp.text

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for node in soup.select("link[rel='alternate']"):
            href = (node.get("href") or "").strip()
            t = (node.get("type") or "").lower()
            if not href:
                continue
            if "rss" not in t and "atom" not in t and "xml" not in t:
                continue
            url = urljoin(canonical_seed, href)
            links.append({"url": url, "title_hint": (node.get("title") or "").strip()})
    except Exception:
        for m in re.finditer(r"<link[^>]+>", html, re.IGNORECASE):
            tag = m.group(0)
            if not re.search(r"rel=['\"]alternate['\"]", tag, re.IGNORECASE):
                continue
            hm = re.search(r"href=['\"]([^'\"]+)['\"]", tag, re.IGNORECASE)
            if not hm:
                continue
            links.append({"url": urljoin(canonical_seed, hm.group(1)), "title_hint": ""})

    for suffix in _FEED_PATH_CANDIDATES:
        links.append({"url": urljoin(canonical_seed, suffix), "title_hint": ""})

    deduped: List[Dict[str, str]] = []
    seen = set()
    for item in links:
        canonical = canonicalize_url(item["url"])
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        valid = await _looks_like_feed(canonical)
        if not valid:
            continue
        deduped.append({"url": item["url"], "url_canonical": canonical, "title_hint": item.get("title_hint", "")})
        if len(deduped) >= max_candidates:
            break

    return deduped


async def queue_discovered_candidates(
    db: AsyncSession,
    *,
    user_id: int,
    seed_url: str,
    max_candidates: int = 10,
) -> List[RSSSourceCandidate]:
    discovered = await discover_candidate_urls(seed_url, max_candidates=max_candidates)
    queued: List[RSSSourceCandidate] = []

    for item in discovered:
        canonical = item["url_canonical"]
        exists_source = await db.execute(
            select(RSSSource.id).where(
                or_(RSSSource.user_id == user_id, RSSSource.user_id.is_(None)),
                RSSSource.url_canonical == canonical,
            )
        )
        if exists_source.scalar_one_or_none() is not None:
            continue

        exists_pending = await db.execute(
            select(RSSSourceCandidate.id).where(
                RSSSourceCandidate.user_id == user_id,
                RSSSourceCandidate.url_canonical == canonical,
                RSSSourceCandidate.status == "pending",
            )
        )
        if exists_pending.scalar_one_or_none() is not None:
            continue

        cand = RSSSourceCandidate(
            user_id=user_id,
            seed_url=seed_url,
            url=item["url"],
            url_canonical=canonical,
            title_hint=item.get("title_hint") or None,
            domain=extract_domain(canonical),
            discovered_via="discover_rss_sources",
            status="pending",
        )
        db.add(cand)
        queued.append(cand)

    await db.flush()
    return queued


async def approve_candidate(
    db: AsyncSession,
    *,
    user_id: int,
    candidate_id: int,
    reviewer_id: int,
    name: Optional[str] = None,
    category: str = "news",
) -> RSSSource:
    cand = await db.get(RSSSourceCandidate, candidate_id)
    if cand is None or cand.user_id != user_id:
        raise ValueError("Candidate not found")
    if cand.status != "pending":
        raise ValueError("Candidate is not pending")

    source = await add_source(
        db,
        user_id=user_id,
        name=(name or cand.title_hint or extract_domain(cand.url) or "Discovered Feed"),
        url=cand.url,
        category=category,
        trust_level="approved_candidate",
        active=True,
    )
    cand.status = "approved"
    cand.reviewed_at = datetime.now(timezone.utc)
    cand.reviewed_by = reviewer_id
    await db.flush()
    return source


async def reject_candidate(
    db: AsyncSession,
    *,
    user_id: int,
    candidate_id: int,
    reviewer_id: int,
    reason: str = "Rejected by user",
) -> RSSSourceCandidate:
    cand = await db.get(RSSSourceCandidate, candidate_id)
    if cand is None or cand.user_id != user_id:
        raise ValueError("Candidate not found")
    if cand.status != "pending":
        raise ValueError("Candidate is not pending")

    cand.status = "rejected"
    cand.reason = reason
    cand.reviewed_at = datetime.now(timezone.utc)
    cand.reviewed_by = reviewer_id
    await db.flush()
    return cand


def _source_to_dict(row: RSSSource, *, scope: str) -> Dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "scope": scope,
        "name": row.name,
        "url": row.url,
        "url_canonical": row.url_canonical,
        "category": row.category,
        "active": row.active,
        "trust_level": row.trust_level,
        "update_interval_minutes": row.update_interval_minutes,
        "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "last_error": row.last_error,
    }


def candidate_to_dict(row: RSSSourceCandidate) -> Dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "seed_url": row.seed_url,
        "url": row.url,
        "url_canonical": row.url_canonical,
        "title_hint": row.title_hint,
        "domain": row.domain,
        "discovered_via": row.discovered_via,
        "status": row.status,
        "reason": row.reason,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _looks_like_feed(url: str) -> bool:
    def _parse() -> bool:
        try:
            import feedparser
        except ImportError:
            return False
        feed = feedparser.parse(url)
        return bool(getattr(feed, "entries", []))

    try:
        return await asyncio.wait_for(asyncio.to_thread(_parse), timeout=8.0)
    except Exception:
        return False


async def _fetch_feed_entries(url: str) -> List[Any]:
    def _parse() -> List[Any]:
        import feedparser

        parsed = feedparser.parse(url)
        if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", []):
            raise ValueError(f"Could not parse feed: {getattr(parsed, 'bozo_exception', 'unknown')}")
        return list(getattr(parsed, "entries", []))

    return await asyncio.wait_for(asyncio.to_thread(_parse), timeout=15.0)


async def upsert_feed_entries(
    db: AsyncSession,
    *,
    source: RSSSource,
    entries: List[Any],
) -> int:
    now = datetime.now(timezone.utc)
    upserted = 0
    for entry in entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        link = (entry.get("link") or "").strip() or None
        summary = _strip_html((entry.get("summary") or entry.get("description") or "").strip())
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[:_MAX_SUMMARY_CHARS] + "..."
        published = _coerce_datetime(
            entry.get("published")
            or entry.get("updated")
            or entry.get("pubDate")
        )
        uid = _entry_uid(entry, fallback_link=link or "", fallback_title=title)
        existing = await db.execute(
            select(RSSItem).where(RSSItem.source_id == source.id, RSSItem.item_uid == uid)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            row = RSSItem(
                source_id=source.id,
                item_uid=uid,
                title=title[:1000],
                link=link,
                summary=summary,
                published_at=published,
                fetched_at=now,
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(row)
        else:
            row.title = title[:1000]
            row.link = link
            row.summary = summary
            row.published_at = published or row.published_at
            row.fetched_at = now
            row.last_seen_at = now
        upserted += 1
    return upserted


async def prune_old_items(db: AsyncSession, *, retention_days: int = _DEFAULT_RETENTION_DAYS) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
    rows = await db.execute(
        select(RSSItem).where(
            RSSItem.last_seen_at < cutoff,
            or_(RSSItem.published_at.is_(None), RSSItem.published_at < cutoff),
        )
    )
    items = rows.scalars().all()
    count = len(items)
    for row in items:
        await db.delete(row)
    return count


def _entry_uid(entry: Any, *, fallback_link: str, fallback_title: str) -> str:
    base = (
        (entry.get("id") or "").strip()
        or (entry.get("guid") or "").strip()
        or fallback_link.strip()
        or fallback_title.strip()
    )
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        try:
            dt = parsedate_to_datetime(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _strip_html(text: str) -> str:
    if not text:
        return ""
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", text).strip()


def _meaningful_query_terms(query: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9]{2,}", (query or "").lower())
    terms = [t for t in tokens if t not in _QUERY_STOPWORDS]
    # dedupe while preserving order
    seen = set()
    out: List[str] = []
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        out.append(term)
    return out
