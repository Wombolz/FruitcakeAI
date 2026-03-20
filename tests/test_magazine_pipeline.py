import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from app.autonomy.magazine_pipeline import (
    _choose_section,
    _pick_balanced_items,
    build_magazine_dataset,
    validate_magazine_markdown,
)
from app.autonomy.newspaper_export import export_newspaper_edition, normalize_magazine_markdown
from app.autonomy.profiles.news_magazine import (
    NewsMagazineExecutionProfile,
    _dedupe_output_by_url,
    _drop_unlinked_item_blocks,
    _inject_missing_links_from_dataset,
    _inject_missing_links_from_dataset_with_report,
    _trim_summary,
)
from app.config import settings
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


def test_normalize_magazine_markdown_preserves_blank_lines_after_links():
    source = (
        "March 19, 2026 Top of the Hour News Magazine\n"
        "Top Stories\n"
        "- **Headline:** Story One\n"
        "Source: Reuters\n"
        "Published at: 2026-03-19T15:56:03+00:00\n"
        "Summary: First summary.\n"
        "[Read More](https://example.com/one)\n"
        "- **Headline:** Story Two\n"
        "Source: BBC\n"
        "Published at: 2026-03-19T15:19:54+00:00\n"
        "Summary: Second summary.\n"
        "[Read More](https://example.com/two)\n"
    )
    normalized = normalize_magazine_markdown(source)
    assert "# Fruitcake News" in normalized
    assert "## Top Stories" in normalized
    assert "[Read More](https://example.com/one)\n\n- **Headline:** Story Two" in normalized
    assert normalized.endswith("\n")


def test_export_newspaper_edition_writes_pdf_markdown_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    markdown = (
        "# Fruitcake News\n\n"
        "## Top Stories\n\n"
        "- **Headline:** Story One\n"
        "Source: Reuters\n"
        "Published at: 2026-03-19T15:56:03+00:00\n"
        "Summary: First summary.\n"
        "[Read More](https://example.com/one)\n\n"
    )
    edition = export_newspaper_edition(
        task_id=48,
        task_run_id=561,
        session_id=688,
        profile="news_magazine",
        final_markdown=markdown,
        started_at=datetime(2026, 3, 19, 15, 54, tzinfo=timezone.utc),
        finished_at=datetime(2026, 3, 19, 16, 0, 21, tzinfo=timezone.utc),
        duration_seconds=381.0,
        publish_mode="full",
        dataset_stats={"selected_count": 100},
        refresh_stats={"sources_refreshed": 137},
        active_skills=["rss-grounded-briefing"],
    )

    assert edition.pdf_path.exists()
    assert edition.markdown_path.exists()
    assert edition.manifest_path.exists()
    assert "task-48/2026-03-19/" in edition.manifest["pdf_relative_path"]

    reader = PdfReader(str(edition.pdf_path))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Fruitcake News" in extracted
    assert "Story One" in extracted
    assert "Read More" in extracted

    manifest = json.loads(Path(edition.manifest_path).read_text(encoding="utf-8"))
    assert manifest["task_id"] == 48
    assert manifest["task_run_id"] == 561
    assert manifest["publish_mode"] == "full"
    assert manifest["display_timezone"]
    assert manifest["display_published_at"].endswith(("-04:00", "-05:00"))


def test_news_magazine_validate_finalize_builds_dense_fruitcake_news_edition():
    profile = NewsMagazineExecutionProfile()
    dataset_items = []
    sections = ["World", "Politics", "Business", "Tech", "Science", "Culture", "Other", "World", "Business", "Tech"]
    for idx, section in enumerate(sections, start=1):
        dataset_items.append(
            {
                "item_id": idx,
                "section": section,
                "title": f"Story {idx}",
                "source": f"Source {idx}",
                "summary": f"Summary for story {idx}.",
                "url": f"https://example.com/{idx}",
                "published_at": f"2026-03-19T1{idx % 10}:00:00+00:00",
            }
        )
    run_context = {
        "dataset": {"run_id": 201, "items": dataset_items},
        "dataset_prompt": "\n".join(f"URL: https://example.com/{idx}" for idx in range(1, 11)),
    }
    source = (
        "## Top Stories\n"
        "- **Headline:** Story 1\n"
        "Source: Source 1\n"
        "Published at: 2026-03-19T10:00:00+00:00\n"
        "Summary: Lead summary.\n"
        "[Read More](https://example.com/1)\n"
        "- **Headline:** Story 2\n"
        "Source: Source 2\n"
        "Published at: 2026-03-19T11:00:00+00:00\n"
        "Summary: Second summary.\n"
        "[Read More](https://example.com/2)\n"
    )
    cleaned, report = profile.validate_finalize(
        result=source,
        prior_full_outputs=[],
        run_context=run_context,
        is_final_step=True,
    )
    assert cleaned.startswith("# Fruitcake News\n")
    assert cleaned.count("[Read More](") == 10
    assert "## Technology" in cleaned
    assert "## Editor's Note" in cleaned
    assert "This hour's edition was led by" in cleaned
    assert report is not None
    assert report["edition"]["story_count"] == 10
    assert report["edition"]["section_count"] >= 5
    assert report["edition"]["auto_filled_story_count"] >= 8


def test_news_magazine_validate_finalize_preserves_clean_model_editors_note():
    profile = NewsMagazineExecutionProfile()
    dataset_items = []
    sections = ["World", "Politics", "Business", "Tech", "Science", "Culture", "Other", "World", "Business", "Tech"]
    for idx, section in enumerate(sections, start=1):
        dataset_items.append(
            {
                "item_id": idx,
                "section": section,
                "title": f"Story {idx}",
                "source": f"Source {idx}",
                "summary": f"Summary for story {idx}.",
                "url": f"https://example.com/{idx}",
                "published_at": f"2026-03-19T1{idx % 10}:00:00+00:00",
            }
        )
    run_context = {
        "dataset": {"run_id": 211, "items": dataset_items},
        "dataset_prompt": "\n".join(f"URL: https://example.com/{idx}" for idx in range(1, 11)),
    }
    source = (
        "## Top Stories\n"
        "- **Headline:** Story 1\n"
        "Source: Source 1\n"
        "Published at: 2026-03-19T10:00:00+00:00\n"
        "Summary: Lead summary with some nuance.\n"
        "[Read More](https://example.com/1)\n\n"
        "## Editor's Note\n"
        "Markets, conflict diplomacy, and vaccine policy defined the hour, with technology funding and regional security shifts broadening the agenda.\n"
    )
    cleaned, report = profile.validate_finalize(
        result=source,
        prior_full_outputs=[],
        run_context=run_context,
        is_final_step=True,
    )
    assert "Markets, conflict diplomacy, and vaccine policy defined the hour" in cleaned
    assert "This hour's edition was led by" not in cleaned
    assert report is not None
    assert report.get("fatal") is False


def test_news_magazine_validate_finalize_cleans_malformed_model_story_fields():
    profile = NewsMagazineExecutionProfile()
    dataset = {
        "run_id": 202,
        "items": [
            {
                "title": "Iran conflict looms large over Trump's meeting with Japan PM",
                "url": "https://example.com/world-1",
                "source": "BBC World",
                "summary": "The ongoing tension between the US and Iran overshadowed a meeting.",
                "published_at": "2026-03-19T18:06:37+00:00",
                "section": "World",
            }
        ],
    }
    run_context = {
        "dataset": dataset,
        "dataset_prompt": "URL: https://example.com/world-1\n",
    }
    source = (
        "## Top Stories\n"
        "- **Headline:** [Read More](https://example.com/world-1)\n"
        "**Source:** **Source:** BBC World\n"
        "**Published at:** 2026-03-19T18:06:37+00:00\n"
        "**Summary:** **Headline:** Iran conflict looms large over Trump's meeting with Japan PM "
        "**Published:** 19 March 2026 The ongoing tension between the US and Iran overshadowed a meeting. ---\n"
        "[Read More](https://example.com/world-1)\n"
    )
    cleaned, report = profile.validate_finalize(
        result=source,
        prior_full_outputs=[],
        run_context=run_context,
        is_final_step=True,
    )
    assert "- **Headline:** Iran conflict looms large over Trump's meeting with Japan PM" in cleaned
    assert "**Source:** BBC World" in cleaned
    assert "**Source:** **Source:**" not in cleaned
    assert "**Summary:** The ongoing tension between the US and Iran overshadowed a meeting." in cleaned
    assert "## Editor's Note" in cleaned
    assert report is not None
    assert report.get("fatal") is False


def test_news_magazine_validate_finalize_prefers_clean_model_summary_when_available():
    profile = NewsMagazineExecutionProfile()
    dataset = {
        "run_id": 203,
        "items": [
            {
                "title": "Lead Story",
                "url": "https://example.com/lead",
                "source": "Example Source",
                "summary": "Dataset summary fallback.",
                "published_at": "2026-03-19T18:06:37+00:00",
                "section": "World",
            }
        ],
    }
    run_context = {
        "dataset": dataset,
        "dataset_prompt": "URL: https://example.com/lead\n",
    }
    source = (
        "## Top Stories\n"
        "- **Headline:** Lead Story\n"
        "Source: Example Source\n"
        "Published at: 2026-03-19T18:06:37+00:00\n"
        "Summary: A cleaner model-written summary adds context beyond the raw feed blurb while staying grounded.\n"
        "[Read More](https://example.com/lead)\n"
    )
    cleaned, _ = profile.validate_finalize(
        result=source,
        prior_full_outputs=[],
        run_context=run_context,
        is_final_step=True,
    )
    assert "**Summary:** A cleaner model-written summary adds context beyond the raw feed blurb while staying grounded." in cleaned


def test_trim_summary_prefers_complete_sentence_boundary():
    text = (
        "Iranian aerial attacks caused extensive damage to the world's largest gas plant in Qatar. "
        "Analysts warned of further disruption to regional energy supplies."
    )
    trimmed = _trim_summary(text, 95)
    assert trimmed.endswith(".")
    assert "world's largest gas plant in Qatar." in trimmed
    assert "Analysts warned" not in trimmed


def test_export_newspaper_pdf_normalizes_unicode_for_helvetica(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    markdown = (
        "# Fruitcake News\n\n"
        "## Top Stories\n\n"
        "- **Headline:** Japan’s PM — delicate “Iran” talks\n"
        "**Source:** BBC World\n"
        "**Published at:** 2026-03-19T18:06:37+00:00\n"
        "**Summary:** The prime minister’s remarks used smart quotes, dashes — and an ellipsis…\n"
        "[Read More](https://example.com/world-1)\n\n"
        "## Editor's Note\n\n"
        "This hour’s edition was led by Japan’s PM — delicate talks.\n"
    )
    edition = export_newspaper_edition(
        task_id=48,
        task_run_id=648,
        session_id=688,
        profile="news_magazine",
        final_markdown=markdown,
        started_at=datetime(2026, 3, 19, 18, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 3, 19, 18, 5, tzinfo=timezone.utc),
        duration_seconds=300.0,
        publish_mode="full",
        dataset_stats={"selected_count": 1},
        refresh_stats={"sources_refreshed": 1},
        active_skills=[],
    )
    reader = PdfReader(str(edition.pdf_path))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Japan's PM - delicate" in extracted
    assert "This hour's edition was led by" in extracted


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


def test_news_magazine_validate_finalize_backfills_from_dataset_when_model_output_is_unusable():
    profile = NewsMagazineExecutionProfile()
    dataset = {
        "run_id": 102,
        "items": [
            {
                "title": "Known Story",
                "url": "https://example.com/known",
                "source": "Example Wire",
                "summary": "Known summary.",
                "published_at": "2026-03-19T12:00:00+00:00",
                "section": "World",
            }
        ],
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
    assert "Unmatched Headline" not in cleaned
    assert "https://example.com/known" in cleaned
    assert report is not None
    assert report.get("fatal") is False
    assert report["edition"]["story_count"] == 1


@pytest.mark.asyncio
async def test_news_magazine_export_artifacts_allows_nonfatal_partial_publish(tmp_path, monkeypatch):
    profile = NewsMagazineExecutionProfile()
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))

    task = SimpleNamespace(id=48)
    run = SimpleNamespace(
        id=681,
        session_id=805,
        started_at=datetime(2026, 3, 20, 11, 54, 17, tzinfo=timezone.utc),
        finished_at=datetime(2026, 3, 20, 12, 1, 26, tzinfo=timezone.utc),
    )
    markdown = (
        "# Fruitcake News\n\n"
        "## Top Stories\n\n"
        "- **Headline:** Story One\n"
        "**Source:** Example Wire\n"
        "**Published at:** 2026-03-20T11:43:13+00:00\n"
        "**Summary:** Example summary.\n"
        "[Read More](https://example.com/one)\n\n"
        "## Editor's Note\n\n"
        "This hour's edition was led by Story One.\n"
    )
    run_debug = {
        "grounding_report": {"publish_mode": "partial", "fatal": False},
        "dataset_stats": {"selected_count": 100},
        "refresh_stats": {"sources_refreshed": 137},
        "active_skills": ["rss-grounded-briefing"],
    }

    payloads = await profile.export_artifact_payloads(
        task=task,
        run=run,
        final_markdown=markdown,
        run_debug=run_debug,
    )

    assert len(payloads) == 1
    assert payloads[0]["artifact_type"] == "edition_export"
    manifest = payloads[0]["content_json"]
    assert manifest["publish_mode"] == "partial"
    assert manifest["download_path"] == f"/admin/task-runs/{run.id}/edition.pdf"
    assert (tmp_path / manifest["pdf_relative_path"]).exists()
