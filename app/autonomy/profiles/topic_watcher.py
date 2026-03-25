from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.autonomy.magazine_pipeline import build_magazine_dataset
from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.spec_loader import load_profile_spec_text
from app.db.models import RSSPublishedItem, Task
from app.mcp.services import rss_sources

_NOTHING_NEW = "NOTHING_NEW"
_VALID_THRESHOLDS = {"low", "medium", "high"}
_CONSEQUENTIAL_KEYWORDS = {
    "agreement",
    "attack",
    "ceasefire",
    "conflict",
    "deal",
    "diplomatic",
    "diplomacy",
    "enrichment",
    "missile",
    "military",
    "minister",
    "negotiation",
    "negotiations",
    "nuclear",
    "president",
    "sanction",
    "sanctions",
    "strike",
    "summit",
    "talk",
    "talks",
    "uranium",
    "warning",
}
_THEME_KEYWORDS = {
    "diplomatic talks": {"agreement", "ceasefire", "deal", "diplomatic", "diplomacy", "negotiation", "negotiations", "summit", "talk", "talks"},
    "military activity": {"attack", "conflict", "military", "missile", "strike", "warning"},
    "nuclear developments": {"enrichment", "nuclear", "uranium"},
    "sanctions and economic pressure": {"sanction", "sanctions"},
    "government and leadership changes": {"minister", "president"},
}


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
        effective_sources = await rss_sources.list_effective_sources(db, user_id=user_id, active_only=True)
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
        source_inventory = _build_source_inventory(effective_sources, source_filters)
        if not source_inventory["active_sources"]:
            warnings.append("No active RSS sources are currently configured for this watcher.")
        if source_filters and not source_inventory["matching_active_sources"]:
            warnings.append("Configured source filters do not match any active RSS sources.")
        items, reuse_fallback_triggered = _select_topic_items(items, threshold=threshold)
        prepared_dataset = {
            "topic": config.get("topic", ""),
            "threshold": threshold,
            "sources": source_filters,
            "notes": config.get("notes", ""),
            "source_inventory": source_inventory,
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
                "active_source_count": len(source_inventory["active_sources"]),
                "matching_active_source_count": len(source_inventory["matching_active_sources"]),
                "reused_available_count": sum(1 for item in items if item.get("previously_published")),
                "reuse_fallback_triggered": reuse_fallback_triggered,
                "rss_dataset_stats": dataset.get("stats", {}),
            },
            "refresh_stats": dataset.get("refresh", {}),
        }

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        del run_context
        return {
            "list_rss_sources",
            "add_rss_source",
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
            "create_memory_entities",
            "create_memory_relations",
            "add_memory_observations",
            "search_memory_graph",
            "open_memory_graph_nodes",
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
                "memory_candidate_emitted": False,
                "memory_candidate_type": None,
                "memory_candidate_confidence": 0.0,
                "memory_candidate_reason": None,
                "memory_candidate_support_count": 0,
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
                "memory_candidate_emitted": False,
                "memory_candidate_type": None,
                "memory_candidate_confidence": 0.0,
                "memory_candidate_reason": None,
                "memory_candidate_support_count": 0,
                "suppress_push": True,
                "collapsed_to_nothing_new": True,
            }

        reused_urls = {str(item.get("url") or "").strip() for item in rss_items if item.get("previously_published")}
        selected_by_url = {
            str(item.get("url") or "").strip(): item
            for item in rss_items
            if str(item.get("url") or "").strip() in deduped_urls
        }
        selected_items = [
            {
                "rss_item_id": int(item.get("article_id") or 0),
                "url_canonical": str(item.get("url") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "source": str(item.get("source") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "published_at": str(item.get("published_at") or "").strip(),
                "reused": str(item.get("url") or "").strip() in reused_urls,
            }
            for item in selected_by_url.values()
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
                "memory_candidate_emitted": False,
                "memory_candidate_type": None,
                "memory_candidate_confidence": 0.0,
                "memory_candidate_reason": None,
                "memory_candidate_support_count": 0,
                "suppress_push": True,
                "collapsed_to_nothing_new": True,
            }

        memory_candidate = _build_memory_candidate(
            topic=topic,
            threshold=threshold,
            selected_items=selected_items,
            result_text=text,
        )
        if memory_candidate:
            text = _append_memory_candidate(text, memory_candidate["content"])

        report = {
            "fatal": False,
            "fired": True,
            "selected_count": len(deduped_urls),
            "reused_count": reused_count,
            "reuse_fallback_triggered": reuse_fallback_triggered,
            "threshold": threshold,
            "topic": topic,
            "published_items": selected_items,
            "memory_candidate_emitted": bool(memory_candidate),
            "memory_candidate_type": (memory_candidate or {}).get("memory_type"),
            "memory_candidate_confidence": float((memory_candidate or {}).get("confidence") or 0.0),
            "memory_candidate_reason": (memory_candidate or {}).get("reason"),
            "memory_candidate_support_count": len((memory_candidate or {}).get("supporting_urls") or []),
            "memory_candidate": memory_candidate,
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
            memory_candidate = grounding.get("memory_candidate")
            if isinstance(memory_candidate, dict):
                out.append(
                    {
                        "artifact_type": "memory_candidates",
                        "content_json": {"candidates": [memory_candidate]},
                    }
                )
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


def _build_source_inventory(effective_sources: List[Dict[str, Any]], source_filters: List[str]) -> Dict[str, Any]:
    active_sources = []
    matching_active_sources = []
    for row in effective_sources:
        if not bool(row.get("active")):
            continue
        entry = {
            "id": row.get("id"),
            "name": str(row.get("name") or "").strip(),
            "url": str(row.get("url") or "").strip(),
            "category": str(row.get("category") or "").strip(),
            "scope": str(row.get("scope") or "").strip(),
        }
        active_sources.append(entry)
        if not source_filters or _matches_source_filter(
            {"source": entry["name"], "source_category": entry["category"]},
            source_filters,
        ):
            matching_active_sources.append(entry)
    return {
        "active_sources": active_sources,
        "matching_active_sources": matching_active_sources,
    }


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
    inventory = dataset.get("source_inventory") or {}
    active_sources = list(inventory.get("active_sources") or [])
    matching_sources = list(inventory.get("matching_active_sources") or [])
    lines.append(f"Active source count: {len(active_sources)}")
    if matching_sources:
        lines.append("Matching active sources:")
        for src in matching_sources[:12]:
            lines.append(
                f"- {src.get('name')} [{src.get('category')}] scope={src.get('scope')} url={src.get('url')}"
            )
    elif active_sources:
        lines.append("Active sources do not currently match the configured source filters.")
    else:
        lines.append("No active RSS sources are currently configured.")
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


def _append_memory_candidate(text: str, content: str) -> str:
    body = (text or "").rstrip()
    candidate = (content or "").strip()
    if not candidate:
        return body
    return f"{body}\n\n## Memory candidate\n- {candidate}"


def _build_memory_candidate(
    *,
    topic: str,
    threshold: str,
    selected_items: List[Dict[str, Any]],
    result_text: str,
) -> Optional[Dict[str, Any]]:
    if not selected_items:
        return None

    combined_text = " ".join(
        [
            result_text,
            *[str(item.get("title") or "") for item in selected_items],
            *[str(item.get("summary") or "") for item in selected_items],
        ]
    ).lower()
    consequential_hits = sum(1 for word in _CONSEQUENTIAL_KEYWORDS if word in combined_text)
    source_names = sorted({str(item.get("source") or "").strip() for item in selected_items if item.get("source")})
    distinct_source_count = len(source_names)
    support_urls = [
        str(item.get("url_canonical") or "").strip()
        for item in selected_items
        if str(item.get("url_canonical") or "").strip()
    ][:5]

    strong_update = False
    if threshold == "high":
        strong_update = consequential_hits >= 1 and distinct_source_count >= 1
    elif threshold == "medium":
        strong_update = consequential_hits >= 2 or (consequential_hits >= 1 and distinct_source_count >= 2)
    else:
        strong_update = consequential_hits >= 2 and distinct_source_count >= 2
    if not strong_update:
        return None

    themes = [
        theme
        for theme, keywords in _THEME_KEYWORDS.items()
        if any(keyword in combined_text for keyword in keywords)
    ]
    if not themes:
        themes = ["notable developments"]

    published_values = [str(item.get("published_at") or "").strip() for item in selected_items if item.get("published_at")]
    memory_date = _normalize_memory_candidate_date(published_values) or datetime.now(timezone.utc).date().isoformat()
    primary_theme = " and ".join(themes[:2])
    source_clause = ", ".join(source_names[:3]) if source_names else "multiple sources"
    confidence = min(0.95, 0.65 + (0.1 * min(consequential_hits, 2)) + (0.05 * min(distinct_source_count, 2)))
    memory_type = "semantic" if any(theme != "notable developments" for theme in themes) and consequential_hits >= 2 else "episodic"
    reason = f"Strong {threshold}-threshold watcher hit with {consequential_hits} consequential signals across {distinct_source_count} source(s)."
    content = (
        f"On {memory_date}, reports about {topic} indicated {primary_theme}, based on coverage from {source_clause}."
    )
    return {
        "memory_type": memory_type,
        "content": content,
        "topic": topic,
        "supporting_urls": support_urls,
        "source_names": source_names,
        "reason": reason,
        "confidence": round(confidence, 2),
        "status": "pending",
        "approved_memory_id": None,
        "approved_at": None,
        "approved_by_user_id": None,
    }


def _normalize_memory_candidate_date(values: List[str]) -> str:
    for value in values:
        if not value:
            continue
        if "T" in value:
            return value.split("T", 1)[0]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return value
    return ""
