from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RSSItem, RSSPublishedItem, RSSSource
from app.mcp.services import rss_sources

_SECTION_ORDER = ["Top", "World", "Politics", "Tech", "Business", "Culture", "Science", "Other"]
_TOP_COUNT = 8
_PER_SOURCE_CAP = 3
_SECTION_MINIMUMS: Dict[str, int] = {
    "World": 4,
    "Politics": 3,
    "Tech": 4,
    "Business": 3,
    "Culture": 3,
    "Science": 2,
}


def _score_item(*, published_at: datetime | None, source_name: str, title: str) -> float:
    now = datetime.now(timezone.utc)
    if published_at is None:
        recency = 0.2
    else:
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (now - published_at).total_seconds() / 3600.0)
        recency = max(0.0, 1.0 - (age_hours / 48.0))

    source_bias = 0.0
    source_lower = (source_name or "").lower()
    if any(k in source_lower for k in ("reuters", "ap", "bbc", "techcrunch", "ft", "wsj")):
        source_bias = 0.1

    title_bias = 0.05 if len((title or "").split()) > 4 else 0.0
    return round(recency + source_bias + title_bias, 4)


def _choose_section(title: str, summary: str, source_name: str, source_category: str | None = None) -> str:
    text = f"{title} {summary} {source_name}".lower()
    words = set(re.findall(r"[a-z0-9]+", text))
    category = (source_category or "").strip().lower()

    if any(k in category for k in ("politic", "government")):
        return "Politics"
    if any(k in category for k in ("science", "space", "research", "climate")):
        return "Science"
    if any(k in category for k in ("culture", "design", "arts", "books", "music", "film")):
        return "Culture"
    if any(k in category for k in ("tech", "technology", "startup")):
        return "Tech"
    if any(k in category for k in ("business", "finance", "econom")):
        return "Business"
    if any(k in category for k in ("world", "international")):
        return "World"

    if any(k in words for k in ("election", "congress", "senate", "campaign", "policy")) or "white house" in text:
        return "Politics"
    if any(k in words for k in ("market", "stocks", "economy", "inflation", "bank", "earnings", "oil")):
        return "Business"
    if any(k in words for k in ("ai", "tech", "software", "chip", "startup", "meta", "amazon", "google")):
        return "Tech"
    if any(k in words for k in ("museum", "film", "music", "art", "book", "culture")):
        return "Culture"
    if any(k in words for k in ("nasa", "space", "science", "climate", "research")):
        return "Science"
    if any(k in words for k in ("world", "global", "g7", "war", "europe", "asia")) or "middle east" in text:
        return "World"
    return "Other"


def _dedupe_by_canonical_url(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: list[Dict[str, Any]] = []
    for item in items:
        canonical = rss_sources.canonicalize_url(item.get("url") or "") or (item.get("url") or "")
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        item["url_canonical"] = canonical
        out.append(item)
    return out


def _pick_balanced_items(
    *,
    items: list[Dict[str, Any]],
    max_items: int,
    per_source_cap: int = _PER_SOURCE_CAP,
    section_minimums: Dict[str, int] | None = None,
) -> list[Dict[str, Any]]:
    if max_items <= 0:
        return []
    if not items:
        return []

    mins = section_minimums or {}
    selected: list[Dict[str, Any]] = []
    selected_urls: set[str] = set()
    source_counts: Counter[int] = Counter()
    section_counts: Counter[str] = Counter()

    def _can_take(item: Dict[str, Any], *, enforce_source_cap: bool) -> bool:
        url = item.get("url_canonical") or item.get("url")
        if not url or url in selected_urls:
            return False
        if enforce_source_cap:
            sid = int(item.get("source_id") or 0)
            if sid and source_counts[sid] >= per_source_cap:
                return False
        return True

    def _add(item: Dict[str, Any]) -> None:
        url = item.get("url_canonical") or item.get("url")
        sid = int(item.get("source_id") or 0)
        section = str(item.get("section") or "Other")
        selected.append(item)
        if url:
            selected_urls.add(url)
        if sid:
            source_counts[sid] += 1
        section_counts[section] += 1

    # First pass: satisfy per-section minimums while respecting per-source cap.
    for section, target in mins.items():
        while section_counts[section] < target and len(selected) < max_items:
            candidate = next(
                (
                    item
                    for item in items
                    if item.get("section") == section and _can_take(item, enforce_source_cap=True)
                ),
                None,
            )
            if candidate is None:
                break
            _add(candidate)

    # Second pass: fill remaining with strongest items while respecting source cap.
    for item in items:
        if len(selected) >= max_items:
            break
        if _can_take(item, enforce_source_cap=True):
            _add(item)

    # Third pass fallback: if cap is too restrictive, fill without source cap.
    for item in items:
        if len(selected) >= max_items:
            break
        if _can_take(item, enforce_source_cap=False):
            _add(item)

    selected.sort(key=lambda i: (i.get("score", 0), i.get("published_at", "")), reverse=True)
    return selected[:max_items]


async def build_magazine_dataset(
    db: AsyncSession,
    *,
    user_id: int,
    task_id: int,
    run_id: int,
    refresh: bool = True,
    window_hours: int = 24,
    max_items: int = 80,
) -> Dict[str, Any]:
    refresh_result = {"sources": 0, "items": 0}
    if refresh:
        refresh_result = await rss_sources.refresh_active_sources_cache(
            db,
            user_id=user_id,
            category=None,
            max_items_per_source=20,
        )
        await db.flush()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, window_hours))

    rows = (
        await db.execute(
            select(RSSItem, RSSSource)
            .join(RSSSource, RSSSource.id == RSSItem.source_id)
            .where(
                RSSSource.active == True,
                ((RSSSource.user_id == user_id) | (RSSSource.user_id.is_(None))),
                ((RSSItem.published_at.is_(None)) | (RSSItem.published_at >= cutoff)),
            )
            .order_by(RSSItem.published_at.desc().nullslast(), RSSItem.fetched_at.desc())
            .limit(max_items * 3)
        )
    ).all()

    published_rows = (
        await db.execute(
            select(RSSPublishedItem.url_canonical).where(RSSPublishedItem.task_id == task_id)
        )
    ).all()
    previously_published_urls = {str(row[0]) for row in published_rows if row[0]}

    prepared: list[Dict[str, Any]] = []
    for item, source in rows:
        published_at = item.published_at or item.fetched_at
        canonical_url = rss_sources.canonicalize_url(item.link or "") or (item.link or "")
        prepared.append(
            {
                "article_id": item.id,
                "source_id": source.id,
                "source": source.name,
                "source_category": source.category or "",
                "title": item.title,
                "summary": (item.summary or "")[:420],
                "url": item.link or "",
                "url_canonical": canonical_url,
                "published_at": published_at.isoformat() if published_at else "",
                "score": _score_item(
                    published_at=published_at,
                    source_name=source.name,
                    title=item.title,
                ),
                "previously_published": canonical_url in previously_published_urls if canonical_url else False,
            }
        )

    prepared = _dedupe_by_canonical_url(prepared)
    for item in prepared:
        item["section"] = _choose_section(
            item.get("title", ""),
            item.get("summary", ""),
            item.get("source", ""),
            item.get("source_category", ""),
        )
    prepared.sort(key=lambda i: (i.get("score", 0), i.get("published_at", "")), reverse=True)
    selected = _pick_balanced_items(
        items=prepared,
        max_items=max_items,
        per_source_cap=_PER_SOURCE_CAP,
        section_minimums=_SECTION_MINIMUMS,
    )

    sections: Dict[str, List[int]] = {name: [] for name in _SECTION_ORDER}
    for idx, item in enumerate(selected, start=1):
        section = item.get("section") or "Other"
        if idx <= _TOP_COUNT:
            sections["Top"].append(idx)
        sections.setdefault(section, []).append(idx)
        item["item_id"] = idx
        item["section"] = section

    section_counts: Counter[str] = Counter(item.get("section", "Other") for item in selected)
    sources_by_section: dict[str, set[str]] = {}
    for item in selected:
        section = item.get("section", "Other")
        source_name = item.get("source") or f"id:{item.get('source_id')}"
        sources_by_section.setdefault(section, set()).add(str(source_name))
    source_counts: Counter[str] = Counter(
        str(item.get("source") or f"id:{item.get('source_id')}") for item in selected
    )
    dominant_share = 0.0
    if selected:
        dominant_share = round(max(source_counts.values()) / len(selected), 3)

    return {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": window_hours,
        "refresh": {
            "enabled": refresh,
            "sources_refreshed": int(refresh_result.get("sources") or 0),
            "items_changed": int(refresh_result.get("items") or 0),
        },
        "items": selected,
        "sections": {k: v for k, v in sections.items() if v},
        "stats": {
            "candidate_count": len(prepared),
            "selected_count": len(selected),
            "unique_url_count": len({i.get("url_canonical") for i in selected if i.get("url_canonical")}),
            "previously_published_candidate_count": sum(1 for i in prepared if i.get("previously_published")),
            "unseen_candidate_count": sum(1 for i in prepared if not i.get("previously_published")),
            "section_counts": dict(section_counts),
            "unique_sources_per_section": {k: len(v) for k, v in sources_by_section.items()},
            "per_source_cap": _PER_SOURCE_CAP,
            "dominant_source_share": dominant_share,
        },
    }


def format_dataset_for_prompt(dataset: Dict[str, Any], *, max_items: int = 60) -> str:
    items = list(dataset.get("items") or [])[:max_items]
    lines = [
        "Prepared article dataset (authoritative):",
        f"run_id={dataset.get('run_id')} window_hours={dataset.get('window_hours')} selected={len(items)}",
        "Use only these item IDs and URLs in output.",
        "",
    ]
    for item in items:
        lines.append(
            f"[Item {item.get('item_id')}] section={item.get('section')} source={item.get('source')} "
            f"published={item.get('published_at')} title={item.get('title')}"
        )
        lines.append(f"URL: {item.get('url')}")
        lines.append(f"Summary: {(item.get('summary') or '')[:180]}")
        lines.append("")
    return "\n".join(lines)


def validate_magazine_markdown(markdown: str, *, dataset: Dict[str, Any]) -> Dict[str, Any]:
    import re

    text = markdown or ""
    allowed = {str(i.get("url") or "").strip() for i in (dataset.get("items") or []) if i.get("url")}
    found = re.findall(r"https?://[^\s)\]]+", text)
    found = [u.rstrip('.,;"\'') for u in found]
    invalid = sorted({u for u in found if u and u not in allowed})

    placeholders = re.findall(r"\[To be fetched\]|Date Not Provided|URL:\s*Link\b", text, flags=re.IGNORECASE)

    seen = set()
    dupes = set()
    for url in found:
        if url in seen:
            dupes.add(url)
        seen.add(url)

    line_items = [line for line in text.splitlines() if line.strip().startswith("- **Headline:**")]
    heading_items = [line for line in text.splitlines() if line.strip().startswith("### ")]
    item_count = max(len(line_items), len(heading_items))
    missing_link_items = max(0, item_count - len(found))

    return {
        "detected_urls": len(found),
        "invalid_urls": invalid,
        "duplicate_urls": sorted(dupes),
        "placeholder_hits": len(placeholders),
        "allowed_url_count": len(allowed),
        "item_count": item_count,
        "missing_link_items": missing_link_items,
        "dataset_run_id": dataset.get("run_id"),
    }


def compact_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
