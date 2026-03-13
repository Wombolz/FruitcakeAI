from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple

from app.autonomy.magazine_pipeline import (
    build_magazine_dataset,
    format_dataset_for_prompt,
    validate_magazine_markdown,
)

from app.autonomy.profiles.base import TaskExecutionProfile


class NewsMagazineExecutionProfile(TaskExecutionProfile):
    name = "news_magazine"

    async def plan_steps(
        self,
        *,
        goal: str,
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
        task_run_id: Optional[int],
    ) -> Dict[str, Any]:
        if not task_run_id:
            return {}
        dataset = await build_magazine_dataset(
            db,
            user_id=user_id,
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
            "get_feed_items",
            "search_feeds",
            "search_my_feeds",
            "list_recent_feed_items",
            "search_library",
            "summarize_document",
            "create_memory",
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
        prompt_parts.append(
            "News magazine profile policy: use only prepared dataset content; do not call retrieval tools."
        )
        prompt_parts.append(
            "Never call any library/memory tools for this task "
            "(search_library, summarize_document, create_memory)."
        )
        prompt_parts.append(
            "Formatting contract: every article entry MUST include a direct markdown link line "
            "in this exact form: [Read More](FULL_URL). "
            "If an item lacks a valid URL from dataset, omit that item."
        )
        prompt_parts.append(
            "If you cannot provide a valid dataset URL for an item, omit that item."
        )
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

        strict_report = validate_magazine_markdown(cleaned, dataset=dataset)
        report.update(
            {
                "invalid_urls_strict": strict_report.get("invalid_urls", []),
                "duplicate_urls_strict": strict_report.get("duplicate_urls", []),
                "placeholder_hits_strict": strict_report.get("placeholder_hits", 0),
                "auto_link_injected_count": inject_meta.get("injected_count", 0),
                "auto_link_ambiguous_count": inject_meta.get("ambiguous_count", 0),
                "dropped_missing_link_items": dropped_missing,
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
        diagnostics = {
            "dataset_stats": run_debug.get("dataset_stats", {}),
            "refresh_stats": run_debug.get("refresh_stats", {}),
            "suppression_events": run_debug.get("tool_failure_suppressions", []),
        }
        if isinstance(dataset, dict):
            out.append({"artifact_type": "prepared_dataset", "content_json": dataset})
        if final_markdown:
            out.append({"artifact_type": "final_output", "content_text": final_markdown})
            out.append({"artifact_type": "draft_output", "content_text": final_markdown})
        if isinstance(grounding, dict):
            out.append({"artifact_type": "validation_report", "content_json": grounding})
        out.append({"artifact_type": "run_diagnostics", "content_json": diagnostics})
        return out


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
        return stripped.split(":", 1)[1].strip()
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
