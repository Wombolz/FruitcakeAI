from datetime import datetime, timedelta, timezone

import pytest

from app.autonomy.magazine_pipeline import (
    _choose_section,
    _pick_balanced_items,
    build_magazine_dataset,
    validate_magazine_markdown,
)
from app.autonomy.profiles.news_magazine import (
    NewsMagazineExecutionProfile,
    _dedupe_output_by_url,
    _drop_unlinked_item_blocks,
    _inject_missing_links_from_dataset,
    _inject_missing_links_from_dataset_with_report,
)
from app.db.models import RSSItem, RSSSource
from tests.conftest import TestSessionLocal


@pytest.mark.asyncio
async def test_build_magazine_dataset_dedupes_and_buckets_without_refresh():
    now = datetime.now(timezone.utc)
    async with TestSessionLocal() as db:
        src = RSSSource(
            user_id=1,
            name="Tech Source",
            url="https://tech.example/feed.xml",
            url_canonical="https://tech.example/feed.xml",
            category="news",
            active=True,
            trust_level="manual",
            update_interval_minutes=60,
        )
        db.add(src)
        await db.flush()

        db.add_all(
            [
                RSSItem(
                    source_id=src.id,
                    item_uid="a1",
                    title="AI startup raises funding",
                    link="https://tech.example/articles/ai?utm_source=x",
                    summary="Funding round details",
                    published_at=now,
                ),
                RSSItem(
                    source_id=src.id,
                    item_uid="a2",
                    title="AI startup raises funding duplicate",
                    link="https://tech.example/articles/ai",
                    summary="Duplicate link with cleaner URL",
                    published_at=now - timedelta(minutes=2),
                ),
            ]
        )
        await db.commit()

        out = await build_magazine_dataset(
            db,
            user_id=1,
            run_id=9001,
            refresh=False,
            window_hours=24,
            max_items=20,
        )

    assert out["run_id"] == 9001
    assert out["stats"]["selected_count"] == 1
    assert out["stats"]["unique_url_count"] == 1
    assert out["sections"]
    item = out["items"][0]
    assert item["section"] in {"Tech", "Business", "Other", "Top"}


def test_validate_magazine_markdown_flags_invalid_and_duplicate_urls():
    dataset = {
        "run_id": 777,
        "items": [
            {"url": "https://example.com/one"},
            {"url": "https://example.com/two"},
        ],
    }
    markdown = (
        "Story A https://example.com/one\n"
        "Story B https://example.com/one\n"
        "Story C https://invalid.example/three\n"
    )
    report = validate_magazine_markdown(markdown, dataset=dataset)
    assert report["detected_urls"] == 3
    assert "https://invalid.example/three" in report["invalid_urls"]
    assert "https://example.com/one" in report["duplicate_urls"]


def test_validate_magazine_markdown_flags_missing_links_per_item():
    dataset = {
        "run_id": 778,
        "items": [
            {"url": "https://example.com/one"},
            {"url": "https://example.com/two"},
        ],
    }
    markdown = (
        "## Top Stories\n"
        "- **Headline:** Story One\n"
        "  **Source:** Example\n"
        "  **Summary:** Text\n"
        "- **Headline:** Story Two\n"
        "  **Source:** Example\n"
        "  **Summary:** Text\n"
        "[Read More](https://example.com/one)\n"
    )
    report = validate_magazine_markdown(markdown, dataset=dataset)
    assert report["item_count"] == 2
    assert report["detected_urls"] == 1
    assert report["missing_link_items"] == 1


def test_choose_section_does_not_misclassify_said_as_ai():
    section = _choose_section(
        title="Officials said the weather remains severe",
        summary="Emergency teams continue response operations.",
        source_name="Global Wire",
        source_category="news",
    )
    assert section in {"World", "Other"}
    assert section != "Tech"


def test_pick_balanced_items_applies_per_source_cap():
    items = []
    for idx in range(10):
        items.append(
            {
                "url": f"https://a.example/{idx}",
                "url_canonical": f"https://a.example/{idx}",
                "source_id": 1,
                "section": "Tech",
                "score": 1.0 - (idx * 0.01),
                "published_at": f"2026-03-12T10:{idx:02d}:00+00:00",
            }
        )
    for idx in range(10):
        items.append(
            {
                "url": f"https://b.example/{idx}",
                "url_canonical": f"https://b.example/{idx}",
                "source_id": 2,
                "section": "World",
                "score": 0.9 - (idx * 0.01),
                "published_at": f"2026-03-12T09:{idx:02d}:00+00:00",
            }
        )

    picked = _pick_balanced_items(
        items=items,
        max_items=6,
        per_source_cap=3,
        section_minimums={"World": 2, "Tech": 2},
    )
    assert len(picked) == 6
    by_source = {}
    for item in picked:
        by_source[item["source_id"]] = by_source.get(item["source_id"], 0) + 1
    assert by_source == {1: 3, 2: 3}


def test_dedupe_output_by_url_keeps_first_item_per_url():
    text = (
        "## Top Stories\n"
        "- **Headline:** A\n"
        "  **Source:** One\n"
        "  [Read More](https://example.com/a)\n"
        "- **Headline:** A duplicate\n"
        "  **Source:** Two\n"
        "  [Read More](https://example.com/a)\n"
        "- **Headline:** B\n"
        "  **Source:** Three\n"
        "  [Read More](https://example.com/b)\n"
    )
    deduped = _dedupe_output_by_url(text)
    assert deduped.count("https://example.com/a") == 1
    assert deduped.count("https://example.com/b") == 1


def test_inject_missing_links_from_dataset_by_title():
    dataset = {
        "items": [
            {"title": "Story One", "url": "https://example.com/one"},
            {"title": "Story Two", "url": "https://example.com/two"},
        ]
    }
    text = (
        "## Top Stories\n"
        "- **Headline:** Story One\n"
        "  **Source:** Example\n"
        "  **Summary:** First story summary\n"
        "### Story Two\n"
        "Second story summary\n"
    )
    repaired = _inject_missing_links_from_dataset(text, dataset=dataset)
    assert "[Read More](https://example.com/one)" in repaired
    assert "[Read More](https://example.com/two)" in repaired


def test_inject_missing_links_from_dataset_fuzzy_title_match():
    dataset = {"items": [{"title": "Federal Reserve signals rate pause", "url": "https://example.com/rates"}]}
    text = "### Federal Reserve signals pause on rates\nSummary text\n"
    repaired, meta = _inject_missing_links_from_dataset_with_report(text, dataset=dataset)
    assert "[Read More](https://example.com/rates)" in repaired
    assert meta["injected_count"] == 1


def test_inject_missing_links_from_dataset_ambiguous_title_skips_injection():
    dataset = {
        "items": [
            {"title": "Market update live", "url": "https://example.com/one"},
            {"title": "Markets update live", "url": "https://example.com/two"},
        ]
    }
    text = "### Market updates live\nSummary text\n"
    repaired, meta = _inject_missing_links_from_dataset_with_report(text, dataset=dataset)
    assert "[Read More](" not in repaired
    assert meta["ambiguous_count"] >= 1


def test_drop_unlinked_item_blocks_keeps_linked_items_only():
    text = (
        "## Top Stories\n"
        "### Keep me\n"
        "[Read More](https://example.com/keep)\n"
        "### Drop me\n"
        "No link here\n"
    )
    cleaned, dropped = _drop_unlinked_item_blocks(text)
    assert "### Keep me" in cleaned
    assert "### Drop me" not in cleaned
    assert dropped == 1


def test_news_magazine_validate_finalize_publishes_partial_when_some_items_missing_links():
    profile = NewsMagazineExecutionProfile()
    dataset = {
        "run_id": 101,
        "items": [
            {"title": "Story One", "url": "https://example.com/one"},
            {"title": "Story Two", "url": "https://example.com/two"},
        ],
    }
    run_context = {
        "dataset": dataset,
        "dataset_prompt": "URL: https://example.com/one\nURL: https://example.com/two\n",
    }
    source = (
        "## Top Stories\n"
        "### Story One\n"
        "Summary one\n"
        "### Story Two\n"
        "Summary two\n"
        "[Read More](https://example.com/two)\n"
    )
    cleaned, report = profile.validate_finalize(
        result=source,
        prior_full_outputs=[],
        run_context=run_context,
        is_final_step=True,
    )
    assert report is not None
    assert report.get("fatal") is False
    assert report.get("publish_mode") == "full"
    assert report.get("auto_link_injected_count") == 1
    assert "https://example.com/one" in cleaned
    assert "https://example.com/two" in cleaned


def test_news_magazine_validate_finalize_fails_when_no_publishable_linked_items():
    profile = NewsMagazineExecutionProfile()
    dataset = {
        "run_id": 102,
        "items": [{"title": "Known Story", "url": "https://example.com/known"}],
    }
    run_context = {
        "dataset": dataset,
        "dataset_prompt": "URL: https://example.com/known\n",
    }
    source = "## Top Stories\n### Unmatched Headline\nNo valid link\n"
    cleaned, report = profile.validate_finalize(
        result=source,
        prior_full_outputs=[],
        run_context=run_context,
        is_final_step=True,
    )
    assert "https://example.com/" not in cleaned
    assert report is not None
    assert report.get("fatal") is True
    assert "no publishable linked items" in str(report.get("fatal_reason", "")).lower()
