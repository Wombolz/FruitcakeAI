from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any, Dict, List, Optional, Tuple

from app.autonomy.magazine_pipeline import (
    build_magazine_dataset,
    format_dataset_for_prompt,
    validate_magazine_markdown,
)
from app.autonomy.newspaper_export import export_newspaper_edition, normalize_magazine_markdown

from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.spec_loader import load_profile_spec_text
from app.db.models import RSSPublishedItem

_MIN_EDITION_STORIES = 10
_TARGET_EDITION_STORIES = 12
_MAX_EDITION_STORIES = 14
_MIN_SECTION_DIVERSITY = 5
_FEATURED_STORY_COUNT = 3
_SECTION_DISPLAY = {
    "Tech": "Technology",
}


@dataclass
class EditionStory:
    rss_item_id: int
    title: str
    source: str
    published_at: str
    summary: str
    url: str
    section: str
    tier: str
    from_model: bool
    reused: bool


@dataclass
class FinalizedEdition:
    markdown: str
    title: str
    editor_note: str
    story_count: int
    featured_count: int
    brief_count: int
    section_count: int
    sections: List[str]
    auto_filled_story_count: int
    selected_unseen_count: int
    selected_reused_count: int
    reuse_fallback_triggered: bool
    published_items: List[Dict[str, Any]]


class NewsMagazineExecutionProfile(TaskExecutionProfile):
    name = "rss_newspaper"

    async def plan_steps(
        self,
        *,
        goal: str,
        user_id: int,
        task_id: int,
        task_instruction: str,
        max_steps: int,
        notes: str,
        style: str,
        model_override: str | None,
        default_planner,
    ) -> List[Dict[str, Any]]:
        base = [
            {
                "title": "Draft Magazine from Dataset",
                "instruction": "Draft all required magazine sections from prepared dataset items only.",
                "requires_approval": False,
            },
            {
                "title": "Final Dedupe and Publish",
                "instruction": "Finalize markdown, dedupe links/stories, and publish grounded output.",
                "requires_approval": False,
            },
        ]
        return base[: max(1, min(max_steps, 2))]

    async def prepare_run_context(
        self,
        *,
        db,
        user_id: int,
        task_id: int,
        task_run_id: Optional[int],
    ) -> Dict[str, Any]:
        if not task_run_id:
            return {}
        dataset = await build_magazine_dataset(
            db,
            user_id=user_id,
            task_id=task_id,
            run_id=task_run_id,
            refresh=True,
            window_hours=24,
            max_items=100,
        )
        return {
            "dataset": dataset,
            "dataset_prompt": format_dataset_for_prompt(dataset, max_items=60),
            "dataset_stats": dataset.get("stats", {}),
            "refresh_stats": dataset.get("refresh", {}),
        }

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        return {
            # This profile is intentionally dataset-driven. Giving local models the
            # broader task-management / memory tool surface has proven brittle and
            # can derail the drafting step into empty non-content turns.
            "add_memory_observations",
            "api_request",
            "create_and_run_task_plan",
            "create_memory",
            "create_memory_entities",
            "create_memory_relations",
            "create_task",
            "create_task_plan",
            "get_daily_market_data",
            "get_intraday_market_data",
            "get_task",
            "get_feed_items",
            "list_tasks",
            "search_feeds",
            "search_my_feeds",
            "list_recent_feed_items",
            "search_library",
            "search_memory_graph",
            "search_places",
            "summarize_document",
            "open_memory_graph_nodes",
            "run_task_now",
            "update_task",
            "web_search",
            "fetch_page",
        }

    def augment_prompt(
        self,
        *,
        prompt_parts: list[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> None:
        prompt_parts.append(load_profile_spec_text(self.name))
        prepared = (run_context.get("dataset_prompt") or "").strip()
        if prepared:
            prompt_parts.append(f"Prepared dataset (authoritative source list):\n{prepared[:18000]}")

    def validate_finalize(
        self,
        *,
        result: str,
        prior_full_outputs: List[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        if not is_final_step:
            return result, None

        dataset = run_context.get("dataset")
        if not isinstance(dataset, dict):
            return result, None

        # Repair missing article links from dataset titles before strict validation.
        repaired, inject_meta = _inject_missing_links_from_dataset_with_report(result, dataset=dataset)
        allowed_urls = _extract_urls(run_context.get("dataset_prompt") or "")
        cleaned, report = _ground_output(repaired, allowed_urls=allowed_urls)
        cleaned = _dedupe_output_by_url(cleaned)
        cleaned, dropped_missing = _drop_unlinked_item_blocks(cleaned)
        edition = _finalize_edition(cleaned, dataset=dataset)
        cleaned = edition.markdown

        strict_report = validate_magazine_markdown(cleaned, dataset=dataset)
        available_item_count = len(list(dataset.get("items") or []))
        expected_story_floor = min(_MIN_EDITION_STORIES, available_item_count) if available_item_count else 0
        expected_section_floor = min(
            _MIN_SECTION_DIVERSITY,
            len({str(item.get("section") or "Other") for item in (dataset.get("items") or []) if item.get("url")}),
            edition.story_count,
        )
        report.update(
            {
                "invalid_urls_strict": strict_report.get("invalid_urls", []),
                "duplicate_urls_strict": strict_report.get("duplicate_urls", []),
                "placeholder_hits_strict": strict_report.get("placeholder_hits", 0),
                "auto_link_injected_count": inject_meta.get("injected_count", 0),
                "auto_link_ambiguous_count": inject_meta.get("ambiguous_count", 0),
                "dropped_missing_link_items": dropped_missing,
                "edition": {
                    "title": edition.title,
                    "story_count": edition.story_count,
                    "featured_count": edition.featured_count,
                    "brief_count": edition.brief_count,
                    "section_count": edition.section_count,
                    "sections": edition.sections,
                    "auto_filled_story_count": edition.auto_filled_story_count,
                    "selected_unseen_count": edition.selected_unseen_count,
                    "selected_reused_count": edition.selected_reused_count,
                    "reuse_fallback_triggered": edition.reuse_fallback_triggered,
                    "target_story_count": min(_TARGET_EDITION_STORIES, available_item_count),
                },
                "freshness": {
                    "selected_unseen_count": edition.selected_unseen_count,
                    "selected_reused_count": edition.selected_reused_count,
                    "reuse_fallback_triggered": edition.reuse_fallback_triggered,
                    "unseen_candidate_count": (run_context.get("dataset_stats") or {}).get("unseen_candidate_count", 0),
                    "previously_published_candidate_count": (run_context.get("dataset_stats") or {}).get("previously_published_candidate_count", 0),
                },
                "published_items": [
                    dict(item) for item in edition.published_items
                ],
            }
        )
        if strict_report.get("invalid_urls"):
            report["fatal"] = True
            report["fatal_reason"] = "Final output contains URL(s) not present in prepared dataset."
        if strict_report.get("duplicate_urls"):
            report["duplicate_urls_warning"] = strict_report.get("duplicate_urls")
        if strict_report.get("detected_urls", 0) == 0:
            report["fatal"] = True
            report["fatal_reason"] = (
                "Final output has no publishable linked items after grounding/repair."
            )
        if expected_story_floor and edition.story_count < expected_story_floor:
            report["fatal"] = True
            report["fatal_reason"] = (
                f"Final output produced only {edition.story_count} publishable stories; "
                f"expected at least {expected_story_floor}."
            )
        if expected_section_floor and edition.section_count < expected_section_floor:
            report["fatal"] = True
            report["fatal_reason"] = (
                f"Final output covered only {edition.section_count} sections; "
                f"expected at least {expected_section_floor}."
            )
        report["publish_mode"] = "partial" if dropped_missing > 0 else "full"
        return cleaned, report

    def artifact_payloads(
        self,
        *,
        final_markdown: str,
        run_debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        out = []
        dataset = run_debug.get("dataset")
        grounding = run_debug.get("grounding_report")
        diagnostics = self._build_run_diagnostics(run_debug=run_debug)
        if isinstance(dataset, dict):
            out.append({"artifact_type": "prepared_dataset", "content_json": dataset})
        if final_markdown:
            out.append({"artifact_type": "final_output", "content_text": final_markdown})
            out.append({"artifact_type": "draft_output", "content_text": final_markdown})
        if isinstance(grounding, dict):
            out.append({"artifact_type": "validation_report", "content_json": grounding})
        out.append({"artifact_type": "run_diagnostics", "content_json": diagnostics})
        return out

    async def export_artifact_payloads(
        self,
        *,
        task,
        run,
        final_markdown: str,
        run_debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not final_markdown:
            return []
        grounding = run_debug.get("grounding_report")
        if not isinstance(grounding, dict):
            return []
        if grounding.get("fatal"):
            return []

        edition = export_newspaper_edition(
            task_id=task.id,
            task_run_id=run.id,
            session_id=run.session_id,
            profile=self.name,
            final_markdown=final_markdown,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=_safe_duration_seconds(run.started_at, run.finished_at),
            publish_mode=str(grounding.get("publish_mode") or "full"),
            dataset_stats=dict(run_debug.get("dataset_stats") or {}),
            refresh_stats=dict(run_debug.get("refresh_stats") or {}),
            active_skills=list(run_debug.get("active_skills") or []),
            timezone_name=getattr(task, "active_hours_tz", None),
        )
        manifest = dict(edition.manifest)
        manifest["download_path"] = f"/admin/task-runs/{run.id}/edition.pdf"
        return [
            {
                "artifact_type": "edition_export",
                "content_json": manifest,
            }
        ]

    async def persist_run_records(
        self,
        *,
        db,
        task,
        run,
        final_markdown: str,
        run_debug: Dict[str, Any],
    ) -> None:
        del final_markdown
        grounding = run_debug.get("grounding_report")
        if not isinstance(grounding, dict) or grounding.get("fatal"):
            return
        published_items = grounding.get("published_items") or []
        for item in published_items:
            if not isinstance(item, dict):
                continue
            rss_item_id = item.get("rss_item_id")
            url_canonical = str(item.get("url_canonical") or "").strip()
            if not rss_item_id or not url_canonical:
                continue
            db.add(
                RSSPublishedItem(
                    task_id=task.id,
                    task_run_id=run.id,
                    rss_item_id=int(rss_item_id),
                    url_canonical=url_canonical,
                    published_at=run.finished_at or datetime.now(timezone.utc),
                )
            )


def _extract_urls(text: str) -> set[str]:
    if not text:
        return set()
    matches = re.findall(r"https?://[^\s)>\]]+", text)
    cleaned = {m.rstrip(".,;\"'") for m in matches if m}
    return {u for u in cleaned if u.startswith("http://") or u.startswith("https://")}


def _ground_output(text: str, *, allowed_urls: set[str]) -> Tuple[str, Dict[str, Any]]:
    original = text or ""
    placeholder_hits = len(
        re.findall(r"\[To be fetched\]|Date Not Provided|URL:\s*Link\b", original, flags=re.IGNORECASE)
    )
    found_urls = _extract_urls(original)
    invalid_urls = sorted(u for u in found_urls if u not in allowed_urls)

    cleaned = original
    dropped = 0
    if invalid_urls:
        kept_lines: list[str] = []
        for line in original.splitlines():
            if any(u in line for u in invalid_urls):
                dropped += 1
                continue
            if re.search(r"\[To be fetched\]|Date Not Provided|URL:\s*Link\b", line, flags=re.IGNORECASE):
                dropped += 1
                continue
            kept_lines.append(line)
        cleaned = "\n".join(kept_lines).strip()

    total_issues = len(invalid_urls) + placeholder_hits
    total_items = max(1, len(found_urls) + placeholder_hits)
    failure_rate = total_issues / total_items
    fatal = failure_rate > 0.50 or (not _extract_urls(cleaned) and bool(found_urls))
    report: Dict[str, Any] = {
        "detected_urls": len(found_urls),
        "invalid_urls": invalid_urls,
        "placeholder_hits": placeholder_hits,
        "dropped_lines": dropped,
        "failure_rate": round(failure_rate, 3),
        "fatal": fatal,
    }
    if fatal:
        report["fatal_reason"] = (
            "Final magazine output failed grounding validation. "
            "Regenerate using only URLs from prepared dataset."
        )
    elif total_issues > 0:
        cleaned = (
            f"{cleaned}\n\n"
            f"_Grounding note: removed {total_issues} unverified placeholder/link element(s)._")
    return cleaned, report


def _dedupe_output_by_url(text: str) -> str:
    if not text:
        return text

    lines = text.splitlines()
    out: list[str] = []
    block: list[str] = []
    in_item = False
    seen_urls: set[str] = set()

    def _flush_block() -> None:
        nonlocal block
        if not block:
            return
        urls = _extract_urls("\n".join(block))
        if not urls:
            out.extend(block)
            block = []
            return
        first = sorted(urls)[0]
        if first in seen_urls:
            block = []
            return
        seen_urls.add(first)
        out.extend(block)
        block = []

    for line in lines:
        stripped = line.strip()
        starts_item = stripped.startswith("- **Headline:**") or stripped.startswith("### ")
        starts_section = stripped.startswith("## ")
        if starts_item:
            _flush_block()
            block = [line]
            in_item = True
            continue
        if starts_section and in_item:
            _flush_block()
            out.append(line)
            in_item = False
            continue
        if in_item:
            block.append(line)
        else:
            out.append(line)

    _flush_block()
    return "\n".join(out).strip()


def _inject_missing_links_from_dataset(text: str, *, dataset: Dict[str, Any]) -> str:
    repaired, _ = _inject_missing_links_from_dataset_with_report(text, dataset=dataset)
    return repaired


def _inject_missing_links_from_dataset_with_report(text: str, *, dataset: Dict[str, Any]) -> Tuple[str, Dict[str, int]]:
    if not text:
        return text, {"injected_count": 0, "ambiguous_count": 0}

    items = dataset.get("items") or []
    if not isinstance(items, list):
        return text, {"injected_count": 0, "ambiguous_count": 0}

    title_to_url: dict[str, list[str]] = {}
    normalized_titles: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        key = _normalize_title(title)
        if not key:
            continue
        if key not in title_to_url:
            title_to_url[key] = []
            normalized_titles.append(key)
        if url not in title_to_url[key]:
            title_to_url[key].append(url)
    if not title_to_url:
        return text, {"injected_count": 0, "ambiguous_count": 0}

    lines = text.splitlines()
    out: list[str] = []
    injected_count = 0
    ambiguous_count = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        title = _extract_item_title(line)
        if title is None:
            out.append(line)
            i += 1
            continue

        j = i + 1
        block: list[str] = [line]
        while j < len(lines):
            nxt = lines[j]
            if _extract_item_title(nxt) is not None or nxt.strip().startswith("## "):
                break
            block.append(nxt)
            j += 1

        if not _extract_urls("\n".join(block)):
            url, ambiguous = _best_url_for_title(
                title,
                title_to_url=title_to_url,
                normalized_titles=normalized_titles,
            )
            if url:
                block.append(f"[Read More]({url})")
                injected_count += 1
            elif ambiguous:
                ambiguous_count += 1
        out.extend(block)
        i = j

    return "\n".join(out), {"injected_count": injected_count, "ambiguous_count": ambiguous_count}


def _extract_item_title(line: str) -> Optional[str]:
    stripped = line.strip()
    if stripped.startswith("- **Headline:**"):
        value = stripped[len("- **Headline:**") :].strip()
        return value.lstrip("*").strip()
    if stripped.startswith("### "):
        return stripped[4:].strip()
    return None


def _normalize_title(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", lowered)


def _best_url_for_title(
    raw_title: str,
    *,
    title_to_url: Dict[str, list[str]],
    normalized_titles: list[str],
) -> Tuple[Optional[str], bool]:
    normalized = _normalize_title(raw_title)
    if not normalized:
        return None, False

    exact = title_to_url.get(normalized)
    if exact:
        if len(exact) == 1:
            return exact[0], False
        return None, True

    best_key: Optional[str] = None
    best_score = 0.0
    second_score = 0.0
    for candidate in normalized_titles:
        score = difflib.SequenceMatcher(None, normalized, candidate).ratio()
        if score > best_score:
            second_score = best_score
            best_score = score
            best_key = candidate
        elif score > second_score:
            second_score = score

    if best_key is None or best_score < 0.80:
        return None, False
    if (best_score - second_score) < 0.03:
        return None, True

    urls = title_to_url.get(best_key) or []
    if len(urls) != 1:
        return None, True
    return urls[0], False


def _safe_duration_seconds(started_at, finished_at) -> Optional[float]:
    if started_at is None or finished_at is None:
        return None
    if getattr(started_at, "tzinfo", None) is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if getattr(finished_at, "tzinfo", None) is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    return round((finished_at - started_at).total_seconds(), 3)


def _drop_unlinked_item_blocks(text: str) -> Tuple[str, int]:
    if not text:
        return text, 0

    lines = text.splitlines()
    out: list[str] = []
    i = 0
    dropped = 0
    while i < len(lines):
        line = lines[i]
        title = _extract_item_title(line)
        if title is None:
            out.append(line)
            i += 1
            continue

        block = [line]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if _extract_item_title(nxt) is not None or nxt.strip().startswith("## "):
                break
            block.append(nxt)
            j += 1

        if _extract_urls("\n".join(block)):
            out.extend(block)
        else:
            dropped += 1
        i = j

    return "\n".join(out).strip(), dropped


def _finalize_edition(text: str, *, dataset: Dict[str, Any]) -> FinalizedEdition:
    dataset_items = [item for item in list(dataset.get("items") or []) if isinstance(item, dict) and item.get("url")]
    parsed_blocks = _parse_story_blocks(text)
    block_by_url = {block["url"]: block for block in parsed_blocks if block.get("url")}
    source_editor_note = _extract_editor_note(text)

    desired_count = min(
        max(_MIN_EDITION_STORIES, len(block_by_url)),
        _TARGET_EDITION_STORIES,
        len(dataset_items),
    ) if dataset_items else len(block_by_url)
    if desired_count <= 0:
        desired_count = len(block_by_url)

    selected_items, freshness = _select_edition_items(
        dataset_items,
        block_by_url=block_by_url,
        desired_count=desired_count,
    )
    stories = _build_edition_stories(selected_items, block_by_url=block_by_url)
    sections = []
    seen_sections = set()
    for story in stories[_FEATURED_STORY_COUNT:]:
        section = story.section
        if section not in seen_sections:
            seen_sections.add(section)
            sections.append(section)
    editor_note = _choose_editor_note(stories, sections=sections, model_note=source_editor_note)
    markdown = _render_edition_markdown(stories, editor_note=editor_note)
    auto_filled_story_count = sum(1 for story in stories if not story.from_model)
    return FinalizedEdition(
        markdown=markdown,
        title="Fruitcake News",
        editor_note=editor_note,
        story_count=len(stories),
        featured_count=sum(1 for story in stories if story.tier == "featured"),
        brief_count=sum(1 for story in stories if story.tier == "brief"),
        section_count=len({story.section for story in stories}),
        sections=sections,
        auto_filled_story_count=auto_filled_story_count,
        selected_unseen_count=int(freshness["selected_unseen_count"]),
        selected_reused_count=int(freshness["selected_reused_count"]),
        reuse_fallback_triggered=bool(freshness["reuse_fallback_triggered"]),
        published_items=[
            {
                "rss_item_id": story.rss_item_id,
                "url_canonical": story.url,
                "section": story.section,
                "title": story.title,
                "reused": story.reused,
            }
            for story in stories
        ],
    )


def _parse_story_blocks(text: str) -> List[Dict[str, str]]:
    if not text:
        return []
    lines = text.splitlines()
    blocks: List[Dict[str, str]] = []
    i = 0
    while i < len(lines):
        title = _extract_item_title(lines[i])
        if title is None:
            i += 1
            continue
        block_lines = [lines[i]]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if _extract_item_title(nxt) is not None or nxt.strip().startswith("## "):
                break
            block_lines.append(nxt)
            j += 1
        url_match = re.search(r"\[Read More\]\((https?://[^\s)]+)\)", "\n".join(block_lines))
        url = url_match.group(1).rstrip('.,;"\'') if url_match else ""
        source = ""
        published_at = ""
        summary_parts: List[str] = []
        for raw in block_lines[1:]:
            stripped = raw.strip()
            if stripped.startswith("**Source:**") or stripped.startswith("Source:"):
                source = _strip_label(stripped)
            elif stripped.startswith("**Published at:**") or stripped.startswith("Published at:"):
                published_at = _strip_label(stripped)
            elif stripped.startswith("**Summary:**") or stripped.startswith("Summary:"):
                summary_parts.append(_strip_label(stripped))
            elif stripped and not stripped.startswith("[Read More]("):
                summary_parts.append(stripped)
        raw_summary = " ".join(part for part in summary_parts if part).strip()
        embedded_title = _extract_embedded_field(raw_summary, "Headline")
        embedded_published = _extract_embedded_field(raw_summary, "Published")
        title = _sanitize_title(title or embedded_title or "")
        source = _sanitize_field(source)
        published_at = _sanitize_published_at(published_at or embedded_published or "")
        summary = _sanitize_summary(raw_summary)
        if (not title or title.startswith("[Read More](")) and embedded_title:
            title = _sanitize_title(embedded_title)
        blocks.append(
            {
                "title": title,
                "source": source,
                "published_at": published_at,
                "summary": summary,
                "url": url,
            }
        )
        i = j
    return blocks


def _select_edition_items(
    dataset_items: List[Dict[str, Any]],
    *,
    block_by_url: Dict[str, Dict[str, str]],
    desired_count: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, int | bool]]:
    if not dataset_items or desired_count <= 0:
        return [], {"selected_unseen_count": 0, "selected_reused_count": 0, "reuse_fallback_triggered": False}

    def _rank(item: Dict[str, Any]) -> tuple[int, float, str]:
        model_bonus = 1 if str(item.get("url") or "") in block_by_url else 0
        return (
            model_bonus,
            float(item.get("score") or 0.0),
            str(item.get("published_at") or ""),
        )

    unseen_items = sorted(
        [item for item in dataset_items if not item.get("previously_published")],
        key=_rank,
        reverse=True,
    )
    reused_items = sorted(
        [item for item in dataset_items if item.get("previously_published")],
        key=_rank,
        reverse=True,
    )
    ordered = list(unseen_items)

    selected: List[Dict[str, Any]] = []
    selected_urls: set[str] = set()
    selected_sections: set[str] = set()
    available_sections = {str(item.get("section") or "Other") for item in dataset_items}
    section_goal = min(
        _MIN_SECTION_DIVERSITY,
        len(available_sections),
        desired_count,
    )

    if section_goal:
        section_priority = ["World", "Politics", "Business", "Tech", "Science", "Culture", "Other"]
        remaining_sections = [section for section in section_priority if section in available_sections]
        remaining_sections.extend(sorted(available_sections - set(remaining_sections)))
        for section in remaining_sections:
            if len(selected_sections) >= section_goal or len(selected) >= desired_count:
                break
            candidate = next(
                (
                    item
                    for item in ordered
                    if str(item.get("section") or "Other") == section
                    and str(item.get("url") or "")
                    and str(item.get("url") or "") not in selected_urls
                ),
                None,
            )
            if candidate is None:
                continue
            url = str(candidate.get("url") or "")
            selected.append(candidate)
            selected_urls.add(url)
            selected_sections.add(section)

    for item in ordered:
        url = str(item.get("url") or "")
        if not url or url in selected_urls:
            continue
        selected.append(item)
        selected_urls.add(url)
        selected_sections.add(str(item.get("section") or "Other"))
        if len(selected) >= desired_count:
            break

    for item in ordered:
        if len(selected) >= desired_count:
            break
        url = str(item.get("url") or "")
        if not url or url in selected_urls:
            continue
        selected.append(item)
        selected_urls.add(url)

    reuse_fallback_triggered = False
    if len(selected) < desired_count and reused_items:
        reuse_fallback_triggered = True
        ordered = reused_items
        available_sections = {str(item.get("section") or "Other") for item in reused_items}
        if section_goal:
            section_priority = ["World", "Politics", "Business", "Tech", "Science", "Culture", "Other"]
            remaining_sections = [section for section in section_priority if section in available_sections]
            remaining_sections.extend(sorted(available_sections - set(remaining_sections)))
            for section in remaining_sections:
                if len(selected) >= desired_count:
                    break
                candidate = next(
                    (
                        item
                        for item in ordered
                        if str(item.get("section") or "Other") == section
                        and str(item.get("url") or "")
                        and str(item.get("url") or "") not in selected_urls
                    ),
                    None,
                )
                if candidate is None:
                    continue
                url = str(candidate.get("url") or "")
                selected.append(candidate)
                selected_urls.add(url)
        for item in ordered:
            if len(selected) >= desired_count:
                break
            url = str(item.get("url") or "")
            if not url or url in selected_urls:
                continue
            selected.append(item)
            selected_urls.add(url)

    selected = selected[: min(desired_count, _MAX_EDITION_STORIES)]
    selected_unseen_count = sum(1 for item in selected if not item.get("previously_published"))
    selected_reused_count = sum(1 for item in selected if item.get("previously_published"))
    return selected, {
        "selected_unseen_count": selected_unseen_count,
        "selected_reused_count": selected_reused_count,
        "reuse_fallback_triggered": reuse_fallback_triggered,
    }


def _build_edition_stories(
    dataset_items: List[Dict[str, Any]],
    *,
    block_by_url: Dict[str, Dict[str, str]],
) -> List[EditionStory]:
    stories: List[EditionStory] = []
    for idx, item in enumerate(dataset_items):
        url = str(item.get("url") or "")
        block = block_by_url.get(url) or {}
        tier = "featured" if idx < _FEATURED_STORY_COUNT else "brief"
        dataset_title = str(item.get("title") or "").strip()
        dataset_source = str(item.get("source") or "").strip()
        dataset_published = _sanitize_published_at(str(item.get("published_at") or "").strip())
        dataset_summary = str(item.get("summary") or "").strip()
        block_title = _sanitize_title(str(block.get("title") or ""))
        block_source = _sanitize_field(str(block.get("source") or ""))
        block_published = _sanitize_published_at(str(block.get("published_at") or ""))
        block_summary = _sanitize_summary(str(block.get("summary") or ""))
        use_block_title = bool(block_title) and not _looks_malformed_title(block_title)
        use_block_source = bool(block_source) and not _looks_malformed_field(block_source)
        use_block_published = bool(block_published) and not _looks_malformed_field(block_published)
        use_block_summary = bool(block_summary) and not _looks_malformed_summary(block_summary)
        raw_summary = block_summary if use_block_summary else dataset_summary
        summary_limit = 260 if tier == "featured" else 180
        stories.append(
            EditionStory(
                rss_item_id=int(item.get("article_id") or 0),
                title=block_title if use_block_title else dataset_title,
                source=block_source if use_block_source else dataset_source,
                published_at=block_published if use_block_published else dataset_published,
                summary=_trim_summary(raw_summary, summary_limit),
                url=url,
                section=str(item.get("section") or "Other"),
                tier=tier,
                from_model=use_block_summary,
                reused=bool(item.get("previously_published")),
            )
        )
    return stories


def _render_edition_markdown(stories: List[EditionStory], *, editor_note: str) -> str:
    lines: List[str] = ["# Fruitcake News", ""]
    if not stories:
        lines.extend(_render_editors_note(editor_note))
        return "\n".join(lines).rstrip() + "\n"

    featured = stories[:_FEATURED_STORY_COUNT]
    briefs = stories[_FEATURED_STORY_COUNT:]
    if featured:
        lines.append("## Top Stories")
        lines.append("")
        for story in featured:
            lines.extend(_render_story_block(story))
            lines.append("")

    section_order = ["World", "Politics", "Business", "Tech", "Science", "Culture", "Other"]
    by_section: Dict[str, List[EditionStory]] = {}
    for story in briefs:
        by_section.setdefault(story.section, []).append(story)
    for section in section_order:
        section_stories = by_section.get(section) or []
        if not section_stories:
            continue
        lines.append(f"## {_SECTION_DISPLAY.get(section, section)}")
        lines.append("")
        for story in section_stories:
            lines.extend(_render_story_block(story))
            lines.append("")

    lines.extend(_render_editors_note(editor_note))
    return "\n".join(line.rstrip() for line in lines).rstrip() + "\n"


def _render_story_block(story: EditionStory) -> List[str]:
    title = _sanitize_title(story.title)
    source = _sanitize_field(story.source)
    published_at = _sanitize_published_at(story.published_at)
    summary = _sanitize_summary(story.summary)
    return [
        f"- **Headline:** {title}",
        f"**Source:** {source}",
        f"**Published at:** {published_at}",
        f"**Summary:** {summary}",
        f"[Read More]({story.url})",
    ]


def _render_editors_note(note: str) -> List[str]:
    return [
        "## Editor's Note",
        "",
        note,
        "",
    ]


def _strip_label(line: str) -> str:
    return re.sub(r"^(?:\*\*[^*]+\*\*|Source|Published at|Summary):\s*", "", line).strip()


def _trim_summary(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return ""
    if len(value) <= limit:
        return value
    sentence_trimmed = _truncate_at_sentence_boundary(value, limit)
    if sentence_trimmed:
        return sentence_trimmed
    trimmed = value[:limit].rsplit(" ", 1)[0].strip(" ,;:-")
    return f"{trimmed}..."


def _truncate_at_sentence_boundary(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    candidate = text[:limit]
    matches = list(re.finditer(r"([.!?])(?:\s|$)", candidate))
    if not matches:
        return ""
    end = matches[-1].end(1)
    return candidate[:end].strip()


def _summarize_featured_titles(titles: List[str]) -> str:
    cleaned = [re.sub(r"\s+", " ", title).strip(" .") for title in titles if title]
    if not cleaned:
        return "the strongest available developments"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{cleaned[0]}, {cleaned[1]}, and {cleaned[2]}"


def _extract_editor_note(text: str) -> str:
    if not text:
        return ""
    marker = re.search(r"^##\s+Editor's Note\s*$", text, flags=re.MULTILINE)
    if not marker:
        return ""
    tail = text[marker.end() :].strip()
    if not tail:
        return ""
    lines: List[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if stripped:
            lines.append(stripped)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _choose_editor_note(stories: List[EditionStory], sections: List[str], model_note: str) -> str:
    cleaned_model_note = _sanitize_summary(model_note)
    if _is_usable_editor_note(cleaned_model_note):
        return cleaned_model_note
    covered = ", ".join(_SECTION_DISPLAY.get(section, section) for section in sections) if sections else "today's strongest available sections"
    leads = [_sanitize_title(story.title) for story in stories[: min(3, len(stories))] if _sanitize_title(story.title)]
    lead_summary = _summarize_featured_titles(leads)
    return (
        f"This hour's edition was led by {lead_summary}. "
        f"Coverage this hour spans {covered}, assembled from validated RSS reporting."
    )


def _sanitize_title(value: str) -> str:
    text = _sanitize_field(value)
    text = text.lstrip("*").strip()
    if text.startswith("[Read More]("):
        return ""
    text = re.sub(r"^\d+\.\s*", "", text).strip()
    return text


def _sanitize_field(value: str) -> str:
    text = value.strip()
    while True:
        cleaned = re.sub(r"^(?:\*\*[^*]+\*\*|Source|Published at|Published|Summary|Headline):\s*", "", text).strip()
        if cleaned == text:
            break
        text = cleaned
    return text.replace("---", "").strip()


def _sanitize_summary(value: str) -> str:
    text = value.strip()
    for label in ("Headline", "Published", "Source"):
        extracted = _extract_embedded_field(text, label)
        if extracted:
            text = re.sub(
                rf"(?:\*\*{label}\*\*|{label}):\s*{re.escape(extracted)}",
                "",
                text,
                count=1,
            ).strip()
    text = re.sub(r"\s*---\s*", " ", text)
    return _sanitize_field(re.sub(r"\s+", " ", text).strip())


def _sanitize_published_at(value: str) -> str:
    text = _sanitize_field(value)
    if not text:
        return ""
    iso_match = re.search(
        r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b",
        text,
    )
    if iso_match:
        return iso_match.group(0)
    date_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if date_match:
        return date_match.group(0)
    return ""


def _extract_embedded_field(text: str, label: str) -> Optional[str]:
    pattern = re.compile(
        rf"(?:\*\*{re.escape(label)}\*\*|{re.escape(label)}):\s*(.+?)(?=\s+(?:\*\*[A-Za-z ][A-Za-z ]*\*\*|[A-Za-z][A-Za-z ]*):|$)"
    )
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).strip()


def _looks_malformed_title(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    placeholders = {
        "top stories",
        "world",
        "politics",
        "business",
        "tech",
        "technology",
        "science",
        "culture",
        "other",
        "read next",
    }
    return (
        (not value)
        or value.startswith("[Read More](")
        or value.startswith("**")
        or normalized in placeholders
    )


def _looks_malformed_field(value: str) -> bool:
    return (not value) or value.startswith("**") or any(token in value for token in ("**Source:**", "**Published", "**Summary:**"))


def _looks_malformed_summary(value: str) -> bool:
    if not value or value in {"**"}:
        return True
    if value.startswith("**"):
        return True
    return any(token in value for token in ("**Source:**", "**Published", "**Summary:**", "[Read More]("))


def _is_usable_editor_note(value: str) -> bool:
    if not value:
        return False
    if len(value.split()) < 14:
        return False
    if any(token in value for token in ("[Read More](", "**Source:**", "**Published")):
        return False
    return True
