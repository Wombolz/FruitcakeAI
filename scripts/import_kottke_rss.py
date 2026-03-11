#!/usr/bin/env python3
"""
scripts/import_kottke_rss.py

Bulk-import curated RSS feeds from Docs/kottke_rolodex_rss.md as
global sources (user_id=NULL) or user-owned sources.

Only rows marked ✅ (curl-verified) or ⚡ (platform-inferred, reliable)
are imported. Rows marked ✗ or ❓ are skipped automatically.

Usage:
    # Preview — no DB writes
    python scripts/import_kottke_rss.py --dry-run

    # Import as global sources (visible to all users)
    python scripts/import_kottke_rss.py

    # Import as sources owned by a specific user
    python scripts/import_kottke_rss.py --user-id 1

    # Limit to one category
    python scripts/import_kottke_rss.py --filter-category design
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

# ── Category mapping: markdown section heading → DB category string ────────────

SECTION_TO_CATEGORY: dict[str, str] = {
    "Technology & Developer Culture": "tech",
    "Science & Environment":          "science",
    "News & Media Criticism":         "news",
    "Long-form, Essays & Curation":   "longform",
    "Politics & Society":             "politics",
    "Art, Design & Creativity":       "design",
    "Culture, History & Humanities":  "culture",
    "Personal Blogs":                 "blogs",
    "Newsletters (email-first, web-accessible RSS)": "newsletter",
    "Maps & Geography":               "maps",
    "Books & Literature":             "books",
    "Podcasts":                       "podcast",
    "Games & Interactive":            "games",
}

# Matches confirmed (✅) or platform-inferred (⚡) table rows, e.g.:
#   | [Ars Technica](https://arstechnica.com) | Desc | ✅ https://feeds.arstechnica.com/… |
_ROW_RE = re.compile(
    r"^\|\s*\[(?P<name>[^\]]+)\]\([^)]+\)\s*"   # [Name](site-url)
    r"\|[^|]+\|"                                  # | Description |
    r"\s*[✅⚡]\s+(?P<feed>https?://\S+?)\s*\|",  # ✅/⚡ https://feed-url |
    re.MULTILINE,
)
_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)


# ── Markdown parser ────────────────────────────────────────────────────────────

def parse_md(path: Path) -> list[dict]:
    """Extract (name, feed_url, category) for every confirmed/inferred feed."""
    text = path.read_text(encoding="utf-8")
    sources: list[dict] = []

    # Build list of (char_offset, section_name) pairs
    sections = [
        (m.start(), m.group(1).strip())
        for m in _SECTION_RE.finditer(text)
    ]

    for idx, (pos, section_name) in enumerate(sections):
        category = SECTION_TO_CATEGORY.get(section_name)
        if not category:
            continue  # intro / "No RSS" section — skip

        end = sections[idx + 1][0] if idx + 1 < len(sections) else len(text)
        chunk = text[pos:end]

        for m in _ROW_RE.finditer(chunk):
            sources.append({
                "name":     m.group("name").strip(),
                "url":      m.group("feed").strip(),
                "category": category,
            })

    return sources


# ── DB import ──────────────────────────────────────────────────────────────────

def _simple_url_ok(url: str) -> bool:
    """Lightweight URL check used in --dry-run (no app imports needed)."""
    return bool(re.match(r"^https?://\S+", url.strip()))


async def import_sources(
    sources: list[dict],
    *,
    user_id: int | None,
    dry_run: bool,
    filter_category: str | None,
) -> None:
    scope_label = "global" if user_id is None else f"user_id={user_id}"
    added = updated = skipped_dup = skipped_bad_url = 0

    # ── Dry-run: no DB imports needed ─────────────────────────────────────────
    if dry_run:
        for src in sources:
            if filter_category and src["category"] != filter_category:
                continue
            if not _simple_url_ok(src["url"]):
                print(f"  [SKIP-URL  ] {src['url']!r}  — bad URL")
                skipped_bad_url += 1
                continue
            print(
                f"  [DRY-ADD   ] [{src['category']:10s}]  "
                f"{src['name']}  →  {src['url']}"
            )
            added += 1
        print(
            f"\nDRY RUN — Scope: {scope_label}\n"
            f"  Would add:   {added}\n"
            f"  Bad URL:     {skipped_bad_url}\n"
            f"  Total seen:  {added + skipped_bad_url}"
        )
        return

    # ── Live run: import app modules ───────────────────────────────────────────
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.db.models import RSSSource
    from app.mcp.services.rss_sources import canonicalize_url

    async with AsyncSessionLocal() as db:
        for src in sources:
            if filter_category and src["category"] != filter_category:
                continue

            canonical = canonicalize_url(src["url"])
            if not canonical:
                print(f"  [SKIP-URL  ] {src['url']!r}  — canonicalize failed")
                skipped_bad_url += 1
                continue

            # Check for existing row — NULL-safe user_id comparison
            if user_id is None:
                existing_q = select(RSSSource).where(
                    RSSSource.user_id.is_(None),
                    RSSSource.url_canonical == canonical,
                )
            else:
                existing_q = select(RSSSource).where(
                    RSSSource.user_id == user_id,
                    RSSSource.url_canonical == canonical,
                )

            existing = (await db.execute(existing_q)).scalar_one_or_none()

            if existing:
                # Upsert: refresh name and category in case they changed
                if existing.name != src["name"] or existing.category != src["category"]:
                    existing.name = src["name"]
                    existing.category = src["category"]
                    print(f"  [UPDATE    ] [{src['category']:10s}]  {src['name']}")
                    updated += 1
                else:
                    print(f"  [SKIP-DUP  ] [{src['category']:10s}]  {src['name']}")
                    skipped_dup += 1
            else:
                row = RSSSource(
                    user_id=user_id,
                    name=src["name"],
                    url=src["url"],
                    url_canonical=canonical,
                    category=src["category"],
                    active=True,
                    trust_level="curated",
                    update_interval_minutes=60,
                )
                db.add(row)
                print(f"  [ADD       ] [{src['category']:10s}]  {src['name']}  →  {src['url']}")
                added += 1

        await db.commit()

    print(
        f"\nScope: {scope_label}\n"
        f"  Added:       {added}\n"
        f"  Updated:     {updated}\n"
        f"  Skipped dup: {skipped_dup}\n"
        f"  Bad URL:     {skipped_bad_url}\n"
        f"  Total seen:  {added + updated + skipped_dup + skipped_bad_url}"
    )


# ── Entry point ────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk-import Kottke Rolodex RSS feeds into FruitcakeAI.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and print what would be imported — no DB writes.",
    )
    parser.add_argument(
        "--user-id", type=int, default=None,
        help="Import as user-owned sources (default: global, user_id=NULL).",
    )
    parser.add_argument(
        "--filter-category", metavar="CAT", default=None,
        help=(
            "Only import sources in this category. "
            f"Choices: {', '.join(sorted(set(SECTION_TO_CATEGORY.values())))}"
        ),
    )
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent
    md_path = project_root / "Docs" / "kottke_rolodex_rss.md"

    if not md_path.exists():
        print(f"ERROR: {md_path} not found.", file=sys.stderr)
        sys.exit(1)

    sources = parse_md(md_path)

    if not sources:
        print("No confirmed/inferred feeds found in the markdown. Check the file.")
        sys.exit(1)

    scope_label = "global (user_id=NULL)" if args.user_id is None else f"user_id={args.user_id}"
    cat_label = args.filter_category or "all"

    print(
        f"Parsed {len(sources)} ✅/⚡ feeds from {md_path.name}\n"
        f"  Mode:     {'DRY RUN' if args.dry_run else 'LIVE'}\n"
        f"  Scope:    {scope_label}\n"
        f"  Category: {cat_label}\n"
    )

    await import_sources(
        sources,
        user_id=args.user_id,
        dry_run=args.dry_run,
        filter_category=args.filter_category,
    )


if __name__ == "__main__":
    asyncio.run(main())
