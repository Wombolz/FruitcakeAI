from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.autonomy.magazine_pipeline import build_magazine_dataset
from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.spec_loader import load_profile_spec_text
from app.db.models import RSSPublishedItem, Task

_NOTHING_NEW = "NOTHING_NEW"
_VALID_THRESHOLDS = {"low", "medium", "high"}


class TopicWatcherExecutionProfile(TaskExecutionProfile):
    name = "topic_watcher"

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
        del goal, user_id, task_id, task_instruction, max_steps, notes, style, model_override, default_planner
        return [
            {
                "title": "Evaluate Topic Watcher",
                "instruction": "Review the prepared RSS dataset and either emit NOTHING_NEW or a watcher briefing.",
                "requires_approval": False,
            }
        ]

    async def prepare_run_context(
        self,
        *,
        db,
        user_id: int,
        task_id: int,
        task_run_id: Optional[int],
    ) -> Dict[str, Any]:
        task = await db.get(Task, task_id)
        config = _parse_topic_watcher_instruction(getattr(task, "instruction", ""))
        dataset = await build_magazine_dataset(
            db,
            user_id=user_id,
            task_id=task_id,
            run_id=task_run_id or 0,
            refresh=True,
            window_hours=24,
            max_items=30,
        )
        items = list(dataset.get("items") or [])
        warnings: list[str] = []
        threshold = str(config.get("threshold") or "medium")
        source_filters = config.get("sources") or []
        if source_filters:
            before = len(items)
            items = [
                item for item in items
                if _matches_source_filter(item, source_filters)
            ]
            if not items and before:
                warnings.append("Configured sources filter excluded all prepared RSS items.")
        items, reuse_fallback_triggered = _select_topic_items(items, threshold=threshold)
        prepared_dataset = {
            "topic": config.get("topic", ""),
            "threshold": threshold,
            "sources": source_filters,
            "notes": config.get("notes", ""),
            "rss_items": items,
            "reuse_fallback_triggered": reuse_fallback_triggered,
        }
        return {
            "watcher_config": config,
            "config_warnings": warnings + list(config.get("warnings") or []),
            "dataset": prepared_dataset,
            "dataset_prompt": _format_topic_prompt_dataset(prepared_dataset),
            "dataset_stats": {
                "rss_count": len(items),
                "reused_available_count": sum(1 for item in items if item.get("previously_published")),
                "reuse_fallback_triggered": reuse_fallback_triggered,
                "rss_dataset_stats": dataset.get("stats", {}),
            },
            "refresh_stats": dataset.get("refresh", {}),
        }

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        del run_context
        return {
            "get_feed_items",
            "search_feeds",
            "search_my_feeds",
            "list_recent_feed_items",
            "search_library",
            "summarize_document",
            "list_library_documents",
            "web_search",
            "fetch_page",
            "create_memory",
        }

    def augment_prompt(
        self,
        *,
        prompt_parts: list[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> None:
        del is_final_step
        prompt_parts.append(load_profile_spec_text(self.name))
        prepared = (run_context.get("dataset_prompt") or "").strip()
        if prepared:
            prompt_parts.append(f"Prepared topic watcher dataset:\n{prepared[:18000]}")

    def validate_finalize(
        self,
        *,
        result: str,
        prior_full_outputs: List[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        del prior_full_outputs
        if not is_final_step:
            return result, None

        config = run_context.get("watcher_config") or {}
        dataset = run_context.get("dataset") or {}
        topic = str(config.get("topic") or "").strip()
        if not topic:
            return "", {
                "fatal": True,
                "fatal_reason": "Topic watcher task is missing required 'topic' configuration.",
            }

        threshold = str(config.get("threshold") or "medium")
        rss_items = list(dataset.get("rss_items") or [])
        allowed_urls = {str(item.get("url") or "").strip() for item in rss_items if item.get("url")}
        text = (result or "").strip()
        if not text:
            text = _NOTHING_NEW
        if text == _NOTHING_NEW:
            return _NOTHING_NEW, {
                "fatal": False,
                "fired": False,
                "selected_count": 0,
                "reused_count": 0,
                "reuse_fallback_triggered": False,
                "threshold": threshold,
                "topic": topic,
                "suppress_push": True,
            }

        urls = [u.rstrip('.,;"\'') for u in re.findall(r"https?://[^\s)\]]+", text)]
        seen: set[str] = set()
        deduped_urls: list[str] = []
        for url in urls:
            if url and url not in seen:
                seen.add(url)
                deduped_urls.append(url)
        invalid_urls = sorted({u for u in deduped_urls if u not in allowed_urls})
        if invalid_urls:
            return _NOTHING_NEW, {
                "fatal": False,
                "fired": False,
                "selected_count": 0,
                "reused_count": 0,
                "reuse_fallback_triggered": False,
                "threshold": threshold,
                "topic": topic,
                "invalid_urls": invalid_urls,
                "suppress_push": True,
                "collapsed_to_nothing_new": True,
            }

        reused_urls = {str(item.get("url") or "").strip() for item in rss_items if item.get("previously_published")}
        selected_items = [
            {
                "rss_item_id": int(item.get("article_id") or 0),
                "url_canonical": str(item.get("url") or "").strip(),
                "reused": str(item.get("url") or "").strip() in reused_urls,
            }
            for item in rss_items
            if str(item.get("url") or "").strip() in deduped_urls
        ]
        reused_count = sum(1 for item in selected_items if item["reused"])
        reuse_fallback_triggered = bool(dataset.get("reuse_fallback_triggered")) or reused_count > 0
        if not deduped_urls:
            return _NOTHING_NEW, {
                "fatal": False,
                "fired": False,
                "selected_count": 0,
                "reused_count": 0,
                "reuse_fallback_triggered": False,
                "threshold": threshold,
                "topic": topic,
                "suppress_push": True,
                "collapsed_to_nothing_new": True,
            }

        report = {
            "fatal": False,
            "fired": True,
            "selected_count": len(deduped_urls),
            "reused_count": reused_count,
            "reuse_fallback_triggered": reuse_fallback_triggered,
            "threshold": threshold,
            "topic": topic,
            "published_items": selected_items,
            "suppress_push": False,
        }
        return text, report

    def artifact_payloads(
        self,
        *,
        final_markdown: str,
        run_debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        dataset = run_debug.get("dataset")
        grounding = run_debug.get("grounding_report")
        diagnostics = {
            "watcher_config": run_debug.get("watcher_config", {}),
            "config_warnings": run_debug.get("config_warnings", []),
            "dataset_stats": run_debug.get("dataset_stats", {}),
            "refresh_stats": run_debug.get("refresh_stats", {}),
        }
        if isinstance(dataset, dict):
            out.append({"artifact_type": "prepared_dataset", "content_json": dataset})
        if final_markdown:
            out.append({"artifact_type": "final_output", "content_text": final_markdown})
        if isinstance(grounding, dict):
            out.append({"artifact_type": "validation_report", "content_json": grounding})
        out.append({"artifact_type": "run_diagnostics", "content_json": diagnostics})
        return out

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
        config = run_debug.get("watcher_config") or {}
        threshold = str(config.get("threshold") or "medium")
        if not isinstance(grounding, dict) or not grounding.get("fired"):
            return
        if grounding.get("reuse_fallback_triggered") and threshold != "low":
            return
        for item in grounding.get("published_items") or []:
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


def _parse_topic_watcher_instruction(instruction: str) -> Dict[str, Any]:
    lines = (instruction or "").splitlines()
    config: Dict[str, Any] = {"warnings": []}
    body_start = 0
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            body_start = idx + 1
            break
        if ":" not in stripped:
            body_start = idx
            break
        key, value = stripped.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key not in {"topic", "threshold", "sources"}:
            body_start = idx
            break
        config[key] = value
        body_start = idx + 1
    threshold = str(config.get("threshold") or "medium").strip().lower()
    if threshold not in _VALID_THRESHOLDS:
        if threshold:
            config["warnings"].append(f"Unknown threshold '{threshold}', defaulted to medium.")
        threshold = "medium"
    config["threshold"] = threshold
    sources_raw = str(config.get("sources") or "").strip()
    if sources_raw:
        config["sources"] = [part.strip().lower() for part in sources_raw.split(",") if part.strip()]
    else:
        config["sources"] = []
    config["topic"] = str(config.get("topic") or "").strip()
    config["notes"] = "\n".join(lines[body_start:]).strip()
    return config


def _matches_source_filter(item: Dict[str, Any], source_filters: List[str]) -> bool:
    source = str(item.get("source") or "").strip().lower()
    category = str(item.get("source_category") or "").strip().lower()
    return any(part in source or part == category for part in source_filters)


def _select_topic_items(items: List[Dict[str, Any]], *, threshold: str) -> tuple[List[Dict[str, Any]], bool]:
    unseen = [item for item in items if not item.get("previously_published")]
    reused = [item for item in items if item.get("previously_published")]
    if threshold != "low":
        return unseen, False
    if len(unseen) >= 5 or not reused:
        return unseen, False
    return unseen + reused, True


def _format_topic_prompt_dataset(dataset: Dict[str, Any]) -> str:
    lines = [
        f"Topic: {dataset.get('topic')}",
        f"Threshold: {dataset.get('threshold')}",
    ]
    sources = list(dataset.get("sources") or [])
    if sources:
        lines.append(f"Source filters: {', '.join(sources)}")
    notes = str(dataset.get("notes") or "").strip()
    if notes:
        lines.append(f"Notes: {notes}")
    lines.append("")
    lines.append("Prepared RSS items:")
    items = list(dataset.get("rss_items") or [])
    if not items:
        lines.append("- none")
    else:
        for item in items:
            lines.append(
                f"- source={item.get('source')} section={item.get('section')} score={item.get('score')} title={item.get('title')} url={item.get('url')} previously_published={item.get('previously_published')}"
            )
    return "\n".join(lines)
