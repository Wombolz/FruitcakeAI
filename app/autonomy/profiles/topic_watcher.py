from __future__ import annotations

import hashlib
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, or_, select

from app.autonomy.magazine_pipeline import build_magazine_dataset
from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.spec_loader import load_profile_spec_text
from app.db.models import Memory, MemoryProposal, RSSPublishedItem, Task
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
_TOPIC_MEMORY_WINDOW_DAYS = 30
_TOPIC_MEMORY_MAX_ITEMS = 8
_TOPIC_MEMORY_SUMMARY_MAX_BULLETS = 6
_TOPIC_MEMORY_SUMMARY_MAX_CHARS = 1200
_TOPIC_ALIAS_MAP = {
    "iran": {"iran", "iranian", "tehran"},
    "israel": {"israel", "israeli", "jerusalem"},
    "gaza": {"gaza", "gazan"},
    "ukraine": {"ukraine", "ukrainian", "kyiv", "kiev"},
    "russia": {"russia", "russian", "moscow", "kremlin"},
    "china": {"china", "chinese", "beijing"},
    "taiwan": {"taiwan", "taiwanese", "taipei"},
    "syria": {"syria", "syrian", "damascus"},
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
        topic = str(config.get("topic") or "")
        topic_filtered_items, topic_match_stats = _filter_topic_items(
            items,
            topic=topic,
            threshold=threshold,
        )
        if items and not topic_filtered_items:
            warnings.append("Prepared RSS dataset contained no strong topical matches for this watcher.")
        items = topic_filtered_items
        source_inventory = _build_source_inventory(effective_sources, source_filters)
        if not source_inventory["active_sources"]:
            warnings.append("No active RSS sources are currently configured for this watcher.")
        if source_filters and not source_inventory["matching_active_sources"]:
            warnings.append("Configured source filters do not match any active RSS sources.")
        topic_memory_history = await _load_topic_memory_history(
            db,
            user_id=user_id,
            topic=str(config.get("topic") or ""),
        )
        topic_memory_timeline_summary = _format_topic_memory_timeline_summary(topic_memory_history)
        items, reuse_fallback_triggered = _select_topic_items(items, threshold=threshold)
        prepared_dataset = {
            "topic": config.get("topic", ""),
            "threshold": threshold,
            "sources": source_filters,
            "notes": config.get("notes", ""),
            "source_inventory": source_inventory,
            "topic_match_stats": topic_match_stats,
            "topic_memory_history": topic_memory_history,
            "topic_memory_timeline_summary": topic_memory_timeline_summary,
            "rss_items": items,
            "reuse_fallback_triggered": reuse_fallback_triggered,
        }
        return {
            "watcher_config": config,
            "config_warnings": warnings + list(config.get("warnings") or []),
            "dataset": prepared_dataset,
            "dataset_prompt": _format_topic_prompt_dataset(prepared_dataset),
            "topic_memory_history": topic_memory_history,
            "topic_memory_timeline_summary": topic_memory_timeline_summary,
            "dataset_stats": {
                "rss_count": len(items),
                "topic_match_candidate_count": topic_match_stats["topic_match_candidate_count"],
                "topic_match_selected_count": topic_match_stats["topic_match_selected_count"],
                "topic_match_fallback_used": topic_match_stats["topic_match_fallback_used"],
                "top_topic_match_titles": topic_match_stats["top_topic_match_titles"],
                "active_source_count": len(source_inventory["active_sources"]),
                "matching_active_source_count": len(source_inventory["matching_active_sources"]),
                "reused_available_count": sum(1 for item in items if item.get("previously_published")),
                "reuse_fallback_triggered": reuse_fallback_triggered,
                "topic_memory_count": len(topic_memory_history),
                "topic_memory_window_days": _TOPIC_MEMORY_WINDOW_DAYS,
                "topic_memory_summary_used": bool(topic_memory_timeline_summary),
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
        topic_memory_timeline_summary = str(run_context.get("topic_memory_timeline_summary") or "").strip()
        if topic_memory_timeline_summary:
            prompt_parts.append(
                "Approved topic memory timeline:\n"
                f"{topic_memory_timeline_summary}\n\n"
                "Use this prior approved topic context to judge whether current items are genuinely new, "
                "continuations, or repetitions. Do not cite memories as sources; source grounding must come "
                "only from the prepared RSS dataset."
            )

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

        memory_candidates = _build_memory_candidates(
            topic=topic,
            threshold=threshold,
            selected_items=selected_items,
            result_text=text,
            prior_topic_memories=list(run_context.get("topic_memory_history") or []),
        )
        suppressed_candidate_reasons = [
            str(candidate.get("suppressed_reason") or "").strip()
            for candidate in memory_candidates
            if isinstance(candidate, dict) and candidate.get("suppressed_reason")
        ]
        published_memory_candidates = [
            candidate for candidate in memory_candidates
            if isinstance(candidate, dict) and not candidate.get("suppressed")
        ]
        if published_memory_candidates:
            text = _append_memory_candidates(
                text,
                [str(candidate.get("content") or "") for candidate in published_memory_candidates],
            )

        report = {
            "fatal": False,
            "fired": True,
            "selected_count": len(deduped_urls),
            "reused_count": reused_count,
            "reuse_fallback_triggered": reuse_fallback_triggered,
            "threshold": threshold,
            "topic": topic,
            "published_items": selected_items,
            "memory_candidate_emitted": bool(published_memory_candidates),
            "memory_candidate_type": (published_memory_candidates[0] if published_memory_candidates else {}).get("memory_type"),
            "memory_candidate_confidence": float(((published_memory_candidates[0] if published_memory_candidates else {}) or {}).get("confidence") or 0.0),
            "memory_candidate_reason": ((published_memory_candidates[0] if published_memory_candidates else {}) or {}).get("reason"),
            "memory_candidate_support_count": len(((published_memory_candidates[0] if published_memory_candidates else {}) or {}).get("supporting_urls") or []),
            "memory_candidate": published_memory_candidates[0] if published_memory_candidates else None,
            "memory_candidates": published_memory_candidates,
            "topic_memory_context_considered": bool(run_context.get("topic_memory_history")),
            "topic_memory_duplicate_suppressed_count": sum(1 for candidate in memory_candidates if isinstance(candidate, dict) and candidate.get("suppressed")),
            "suppressed_candidate_reasons": [reason for reason in suppressed_candidate_reasons if reason],
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
            memory_candidates = [
                candidate for candidate in (grounding.get("memory_candidates") or [])
                if isinstance(candidate, dict)
            ]
            if memory_candidates:
                out.append(
                    {
                        "artifact_type": "memory_candidates",
                        "content_json": {"candidates": memory_candidates},
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
        created_proposals: list[dict[str, Any]] = []
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
        for candidate in grounding.get("memory_candidates") or []:
            if not isinstance(candidate, dict):
                continue
            proposal_key = str(candidate.get("proposal_key") or candidate.get("candidate_key") or "").strip()
            content = str(candidate.get("content") or "").strip()
            if not proposal_key or not content:
                continue
            proposal = MemoryProposal(
                proposal_key=proposal_key,
                user_id=task.user_id,
                proposal_type="flat_memory_create",
                source_type="topic_watcher",
                status="pending",
                task_id=task.id,
                task_run_id=run.id,
                content=content,
                confidence=float(candidate.get("confidence") or 0.0),
                reason=str(candidate.get("reason") or "").strip() or None,
            )
            proposal.proposal_payload = {
                "memory_type": str(candidate.get("memory_type") or "").strip(),
                "content": content,
                "topic": str(candidate.get("topic") or "").strip(),
                "supporting_urls": list(candidate.get("supporting_urls") or []),
                "source_names": list(candidate.get("source_names") or []),
                "reason": str(candidate.get("reason") or "").strip(),
                "confidence": float(candidate.get("confidence") or 0.0),
                "expires_at": str(candidate.get("expires_at") or "").strip(),
                "proposal_key": proposal_key,
            }
            db.add(proposal)
            await db.flush()
            candidate["proposal_id"] = proposal.id
            created_proposals.append(
                {
                    "proposal_id": proposal.id,
                    "proposal_key": proposal_key,
                    "status": proposal.status,
                    "content": content,
                }
            )
        if created_proposals:
            grounding["memory_candidates"] = list(grounding.get("memory_candidates") or [])
            for candidate in grounding["memory_candidates"]:
                if not isinstance(candidate, dict):
                    continue
                key = str(candidate.get("proposal_key") or candidate.get("candidate_key") or "").strip()
                for created in created_proposals:
                    if created["proposal_key"] == key:
                        candidate["proposal_id"] = created["proposal_id"]
                        candidate["status"] = created["status"]
                        break
            grounding["memory_candidate"] = grounding["memory_candidates"][0]


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
    topic_match_stats = dataset.get("topic_match_stats") or {}
    if topic_match_stats:
        lines.append(f"Topic match candidate count: {topic_match_stats.get('topic_match_candidate_count', 0)}")
        lines.append(f"Topic match selected count: {topic_match_stats.get('topic_match_selected_count', 0)}")
        lines.append(f"Topic match fallback used: {bool(topic_match_stats.get('topic_match_fallback_used'))}")
    sources = list(dataset.get("sources") or [])
    if sources:
        lines.append(f"Source filters: {', '.join(sources)}")
    notes = str(dataset.get("notes") or "").strip()
    if notes:
        lines.append(f"Notes: {notes}")
    timeline_summary = str(dataset.get("topic_memory_timeline_summary") or "").strip()
    if timeline_summary:
        lines.append("")
        lines.append("Approved topic memory timeline:")
        lines.append(timeline_summary)
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
                f"- source={item.get('source')} section={item.get('section')} score={item.get('score')} topic_match_score={item.get('topic_match_score')} title={item.get('title')} url={item.get('url')} previously_published={item.get('previously_published')}"
            )
    return "\n".join(lines)


def _append_memory_candidates(text: str, contents: List[str]) -> str:
    body = (text or "").rstrip()
    lines = [f"- {(content or '').strip()}" for content in contents if str(content or "").strip()]
    if not lines:
        return body
    return f"{body}\n\n## Memory candidates\n" + "\n".join(lines)


def _build_memory_candidates(
    *,
    topic: str,
    threshold: str,
    selected_items: List[Dict[str, Any]],
    result_text: str,
    prior_topic_memories: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not selected_items:
        return []

    result_text_lower = (result_text or "").lower()
    clusters: dict[str, List[Dict[str, Any]]] = {}
    for item in selected_items:
        item_text = " ".join(
            [
                result_text_lower,
                str(item.get("title") or "").lower(),
                str(item.get("summary") or "").lower(),
            ]
        )
        matched_themes = [
            theme
            for theme, keywords in _THEME_KEYWORDS.items()
            if any(keyword in item_text for keyword in keywords)
        ]
        cluster_key = matched_themes[0] if matched_themes else "notable developments"
        clusters.setdefault(cluster_key, []).append(item)

    ranked_clusters = sorted(
        clusters.items(),
        key=lambda pair: (
            -_consequential_hits_for_items(pair[1], result_text=result_text),
            -len({str(item.get("source") or "").strip() for item in pair[1] if item.get("source")}),
            -len(pair[1]),
            pair[0],
        ),
    )
    candidates: List[Dict[str, Any]] = []
    for theme, items in ranked_clusters:
        candidate = _build_cluster_memory_candidate(
            topic=topic,
            threshold=threshold,
            theme=theme,
            selected_items=items,
            result_text=result_text,
        )
        if candidate:
            duplicate_reason = _topic_memory_duplicate_reason(
                candidate=candidate,
                prior_topic_memories=prior_topic_memories,
            )
            if duplicate_reason:
                candidate["suppressed"] = True
                candidate["suppressed_reason"] = duplicate_reason
            candidates.append(candidate)
        if len(candidates) >= 3:
            break
    return candidates


def _build_cluster_memory_candidate(
    *,
    topic: str,
    threshold: str,
    theme: str,
    selected_items: List[Dict[str, Any]],
    result_text: str,
) -> Optional[Dict[str, Any]]:
    combined_text = " ".join(
        [
            result_text,
            *[str(item.get("title") or "") for item in selected_items],
            *[str(item.get("summary") or "") for item in selected_items],
        ]
    ).lower()
    consequential_hits = _consequential_hits_for_items(selected_items, result_text=result_text)
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

    published_values = [str(item.get("published_at") or "").strip() for item in selected_items if item.get("published_at")]
    memory_date = _normalize_memory_candidate_date(published_values) or datetime.now(timezone.utc).date().isoformat()
    source_clause = ", ".join(source_names[:3]) if source_names else "multiple sources"
    confidence = min(0.95, 0.65 + (0.1 * min(consequential_hits, 2)) + (0.05 * min(distinct_source_count, 2)))
    memory_type = "episodic"
    expires_at = _memory_candidate_expiry(memory_date)
    reason = f"Strong {threshold}-threshold watcher hit for {theme} with {consequential_hits} consequential signals across {distinct_source_count} source(s)."
    content = f"On {memory_date}, reports about {topic} indicated {theme}, based on coverage from {source_clause}."
    proposal_key = hashlib.sha256(
        "|".join([topic, theme, content, *support_urls]).encode("utf-8")
    ).hexdigest()
    return {
        "proposal_key": proposal_key,
        "memory_type": memory_type,
        "content": content,
        "topic": topic,
        "supporting_urls": support_urls,
        "source_names": source_names,
        "reason": reason,
        "confidence": round(confidence, 2),
        "expires_at": expires_at,
        "status": "pending",
        "approved_memory_id": None,
        "approved_at": None,
        "approved_by_user_id": None,
    }


def _consequential_hits_for_items(selected_items: List[Dict[str, Any]], *, result_text: str) -> int:
    combined_text = " ".join(
        [
            result_text,
            *[str(item.get("title") or "") for item in selected_items],
            *[str(item.get("summary") or "") for item in selected_items],
        ]
    ).lower()
    return sum(1 for word in _CONSEQUENTIAL_KEYWORDS if word in combined_text)


def _normalize_memory_candidate_date(values: List[str]) -> str:
    for value in values:
        if not value:
            continue
        if "T" in value:
            return value.split("T", 1)[0]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return value
    return ""


def _memory_candidate_expiry(memory_date: str) -> str:
    try:
        base_date = datetime.strptime(memory_date, "%Y-%m-%d").date()
    except ValueError:
        base_date = datetime.now(timezone.utc).date()
    expires_at = datetime.combine(base_date, time.min, tzinfo=timezone.utc) + timedelta(days=30)
    return expires_at.isoformat()


def _topic_slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (topic or "").strip().lower()).strip("_")


def _topic_terms(topic: str) -> List[str]:
    normalized = _normalize_candidate_text(topic)
    if not normalized:
        return []
    terms = {term for term in normalized.split() if len(term) > 2}
    slug = _topic_slug(topic)
    terms.update(_TOPIC_ALIAS_MAP.get(slug, set()))
    return sorted(terms)


def _topic_match_score(item: Dict[str, Any], *, topic: str) -> float:
    terms = _topic_terms(topic)
    if not terms:
        return 0.0
    title = str(item.get("title") or "").lower()
    summary = str(item.get("summary") or "").lower()
    source = str(item.get("source") or "").lower()
    section = str(item.get("section") or "").lower()
    score = 0.0
    exact_topic = _normalize_candidate_text(topic)
    haystack = f"{title} {summary} {source} {section}"
    if exact_topic and exact_topic in _normalize_candidate_text(haystack):
        score += 1.0
    for term in terms:
        if term in title:
            score += 1.5
        elif term in summary:
            score += 0.8
        elif term in section or term in source:
            score += 0.2
    return round(score, 3)


def _filter_topic_items(
    items: List[Dict[str, Any]],
    *,
    topic: str,
    threshold: str,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    scored: List[Dict[str, Any]] = []
    for item in items:
        enriched = dict(item)
        enriched["topic_match_score"] = _topic_match_score(item, topic=topic)
        scored.append(enriched)
    scored.sort(
        key=lambda item: (
            -float(item.get("topic_match_score") or 0.0),
            -float(item.get("score") or 0.0),
        )
    )
    if threshold == "high":
        minimum_score = 1.5
    elif threshold == "medium":
        minimum_score = 1.0
    else:
        minimum_score = 0.8
    matched = [item for item in scored if float(item.get("topic_match_score") or 0.0) >= minimum_score]
    fallback_used = False
    if not matched:
        fallback_threshold = 0.5 if threshold == "low" else 0.8
        matched = [item for item in scored if float(item.get("topic_match_score") or 0.0) >= fallback_threshold]
        fallback_used = bool(matched)
    return matched[:12], {
        "topic_match_candidate_count": sum(1 for item in scored if float(item.get("topic_match_score") or 0.0) > 0.0),
        "topic_match_selected_count": len(matched[:12]),
        "topic_match_fallback_used": fallback_used,
        "top_topic_match_titles": [str(item.get("title") or "").strip() for item in scored[:5] if str(item.get("title") or "").strip()],
    }


async def _load_topic_memory_history(
    db,
    *,
    user_id: int,
    topic: str,
) -> List[Dict[str, Any]]:
    topic_slug = _topic_slug(topic)
    if not topic_slug:
        return []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=_TOPIC_MEMORY_WINDOW_DAYS)
    result = await db.execute(
        select(Memory)
        .where(
            and_(
                Memory.user_id == user_id,
                Memory.is_active == True,
                Memory.tags.like('%"topic_watcher"%'),
                Memory.tags.like(f'%"{topic_slug}"%'),
                Memory.created_at >= cutoff,
                or_(Memory.expires_at == None, Memory.expires_at > now),
            )
        )
        .order_by(desc(Memory.created_at))
        .limit(_TOPIC_MEMORY_MAX_ITEMS)
    )
    rows = result.scalars().all()
    history: List[Dict[str, Any]] = []
    for memory in rows:
        history.append(
            {
                "id": memory.id,
                "content": str(memory.content or "").strip(),
                "memory_type": str(memory.memory_type or "").strip(),
                "created_at": memory.created_at.isoformat() if memory.created_at else None,
                "expires_at": memory.expires_at.isoformat() if memory.expires_at else None,
                "tags": memory.tags_list,
            }
        )
    return history


def _format_topic_memory_timeline_summary(history: List[Dict[str, Any]]) -> str:
    if not history:
        return ""
    bullets: List[str] = []
    for item in history[:_TOPIC_MEMORY_SUMMARY_MAX_BULLETS]:
        content = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
        if not content:
            continue
        created_at = str(item.get("created_at") or "").strip()
        date = created_at.split("T", 1)[0] if "T" in created_at else created_at[:10]
        prefix = f"- {date}: " if date else "- "
        bullets.append(f"{prefix}{content}")
    summary = "\n".join(bullets).strip()
    if len(summary) > _TOPIC_MEMORY_SUMMARY_MAX_CHARS:
        summary = summary[: _TOPIC_MEMORY_SUMMARY_MAX_CHARS].rstrip() + "..."
    return summary


def _normalize_candidate_text(value: str) -> str:
    text = re.sub(r"[^a-z0-9\s]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _token_overlap_ratio(a: str, b: str) -> float:
    a_tokens = {token for token in _normalize_candidate_text(a).split() if len(token) > 2}
    b_tokens = {token for token in _normalize_candidate_text(b).split() if len(token) > 2}
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / float(len(a_tokens | b_tokens))


def _topic_memory_duplicate_reason(
    *,
    candidate: Dict[str, Any],
    prior_topic_memories: List[Dict[str, Any]],
) -> Optional[str]:
    candidate_content = str(candidate.get("content") or "").strip()
    if not candidate_content:
        return None
    for memory in prior_topic_memories:
        prior_content = str(memory.get("content") or "").strip()
        if not prior_content:
            continue
        overlap = _token_overlap_ratio(candidate_content, prior_content)
        if overlap >= 0.7:
            return f"Suppressed duplicate topic memory candidate due to high similarity with approved topic memory {memory.get('id')}."
        prior_date = str(memory.get("created_at") or "").split("T", 1)[0]
        if prior_date and prior_date in candidate_content and overlap >= 0.55:
            return f"Suppressed duplicate topic memory candidate due to repeated same-day development already captured in approved topic memory {memory.get('id')}."
    return None
