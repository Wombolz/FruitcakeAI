from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.autonomy.magazine_pipeline import build_magazine_dataset
from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.resolver import resolve_task_profile
from app.autonomy.profiles.spec_loader import load_profile_spec_text
from app.autonomy.profiles.topic_watcher import _filter_topic_items, _format_topic_prompt_dataset
from app.db.models import Task
from app.mcp.servers.filesystem import append_workspace_text, read_workspace_text, workspace_path_exists, write_workspace_text
from app.time_utils import utc_compact_timestamp

_SUPPORTED_KIND = "configured_executor"
_SUPPORTED_INPUT_MODE = "prepared_rss_topic_dataset"
_SUPPORTED_TOOL_POLICY = "dataset_plus_workspace_append"
_SUPPORTED_OUTPUT_MODE = "daily_research_briefing"
_SUPPORTED_PERSISTENCE_MODE = "append_workspace_file"
_SUPPORTED_VALIDATION_MODE = "grounded_briefing"
_SUPPORTED_NOTIFY_MODE = "appended_summary"
_SUPPORTED_NO_UPDATE_POLICY = "append_entry"
_SUPPORTED_DUPLICATE_OUTPUT_POLICY = "suppress_similar_recent_entry"

_NO_SIGNIFICANT_UPDATES = "NO_SIGNIFICANT_UPDATES"
_DATASET_REVIEW_SPEC = """
Prepared dataset review contract:
- Use only the prepared RSS dataset provided in the prompt.
- Do not call external search, browser, or task-management tools.
- Return a compact review of the most relevant prepared items, not a final briefing.
- Do not include sections named Implications, Key indicators to watch, or Links (from cached feeds).
- Do not emit memory-candidate sections.
- Keep the output short and focused on what appears most relevant in the prepared dataset.
"""
_RESEARCH_BRIEFING_SPEC = """
Daily research briefing contract:
- Use only the prepared RSS dataset provided in the prompt.
- Do not call external search, browser, or task-management tools.
- If there are meaningful developments, return:
  Implications:
  Key indicators to watch:
  Links (from cached feeds):
- Before those sections, include 1-6 concise bullet lines beginning with "- ".
- Every factual claim must be grounded in the prepared dataset.
- Preserve exact URLs from the dataset. Do not invent, rewrite, or shorten URLs.
- If there are no meaningful developments, return ONLY NO_SIGNIFICANT_UPDATES.
"""

_TOOL_POLICY_BLOCKED_TOOLS: dict[str, set[str]] = {
    "dataset_plus_workspace_append": {
        "add_memory_observations",
        "api_request",
        "append_file",
        "create_and_run_task_plan",
        "create_event",
        "create_memory",
        "create_memory_entities",
        "create_memory_relations",
        "create_task",
        "create_task_plan",
        "delete_event",
        "fetch_page",
        "find_files",
        "get_daily_market_data",
        "get_intraday_market_data",
        "get_task",
        "list_directory",
        "list_library_documents",
        "list_recent_feed_items",
        "list_rss_sources",
        "list_tasks",
        "make_directory",
        "open_memory_graph_nodes",
        "read_file",
        "run_task_now",
        "search_feeds",
        "search_library",
        "search_memory_graph",
        "search_my_feeds",
        "search_places",
        "stat_file",
        "summarize_document",
        "update_task",
        "web_search",
        "write_file",
    }
}


@dataclass(frozen=True)
class NormalizedConfiguredExecutor:
    kind: str
    input_mode: str
    tool_policy: str
    output_mode: str
    persistence_mode: str
    validation_mode: str
    notify_mode: str
    no_update_policy: str
    duplicate_output_policy: str
    input: dict[str, Any]
    persistence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "input_mode": self.input_mode,
            "tool_policy": self.tool_policy,
            "output_mode": self.output_mode,
            "persistence_mode": self.persistence_mode,
            "validation_mode": self.validation_mode,
            "notify_mode": self.notify_mode,
            "no_update_policy": self.no_update_policy,
            "duplicate_output_policy": self.duplicate_output_policy,
            "input": dict(self.input),
            "persistence": dict(self.persistence),
        }


@dataclass(frozen=True)
class InferredConfiguredExecutor:
    profile: str | None
    executor_config: dict[str, Any]


class ConfiguredDailyResearchBriefingExecution(TaskExecutionProfile):
    name = "configured_executor"

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config

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
        topic = str(((self.config.get("input") or {}).get("topic") or "")).strip()
        return [
            {
                "title": "Prepare Topic Dataset",
                "instruction": f"Prepare the cached RSS dataset for the topic '{topic}'.",
                "requires_approval": False,
            },
            {
                "title": "Draft Grounded Briefing",
                "instruction": (
                    "Draft a grounded daily research briefing using only the prepared RSS dataset. "
                    "Return 1-6 bullets, Implications, Key indicators to watch, and Links."
                ),
                "requires_approval": False,
            },
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
        effective_config = normalize_executor_config(task.executor_config or self.config)
        input_config = effective_config["input"]
        threshold = str(input_config.get("threshold") or "medium")
        topic = str(input_config.get("topic") or "").strip()
        window_hours = int(input_config.get("window_hours") or 24)
        source_filters = [str(item).strip().lower() for item in (input_config.get("sources") or []) if str(item).strip()]
        dataset = await build_magazine_dataset(
            db,
            user_id=user_id,
            task_id=task_id,
            run_id=task_run_id or 0,
            refresh=True,
            window_hours=window_hours,
            max_items=30,
        )
        items = list(dataset.get("items") or [])
        if source_filters:
            items = [
                item for item in items
                if any(
                    part in str(item.get("source") or "").strip().lower()
                    or part == str(item.get("source_category") or "").strip().lower()
                    for part in source_filters
                )
            ]
        topic_items, topic_match_stats = _filter_topic_items(items, topic=topic, threshold=threshold)
        duplicate_filter_stats = {
            "recent_entry_considered": False,
            "recent_overlap_count": 0,
            "recent_repeat_pruned_count": 0,
            "title_cluster_count": 0,
            "title_cluster_pruned_count": 0,
        }
        persistence = effective_config.get("persistence") or {}
        path = str(persistence.get("path") or "").strip()
        if path and workspace_path_exists(user_id, path):
            latest_entry = _extract_latest_briefing_entry(read_workspace_text(user_id, path))
            topic_items, duplicate_filter_stats = _prune_recently_reported_items(
                topic_items,
                latest_entry=latest_entry,
                now=datetime.now(timezone.utc),
            )
        topic_items, cluster_stats = _apply_light_title_cluster_diversity(topic_items)
        duplicate_filter_stats.update(cluster_stats)
        prepared_dataset = {
            "topic": topic,
            "threshold": threshold,
            "window_hours": window_hours,
            "sources": source_filters,
            "rss_items": topic_items,
            "topic_match_stats": topic_match_stats,
        }
        return {
            "executor_config": effective_config,
            "dataset": prepared_dataset,
            "dataset_prompt": _format_topic_prompt_dataset(prepared_dataset),
            "dataset_stats": {
                "rss_count": len(topic_items),
                "window_hours": window_hours,
                "topic": topic,
                "topic_match_candidate_count": topic_match_stats["topic_match_candidate_count"],
                "topic_match_selected_count": topic_match_stats["topic_match_selected_count"],
                "topic_match_fallback_used": topic_match_stats["topic_match_fallback_used"],
                **duplicate_filter_stats,
            },
            "refresh_stats": dataset.get("refresh", {}),
        }

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        config = normalize_executor_config(run_context.get("executor_config") or self.config)
        if config is None:
            return set()
        return set(_TOOL_POLICY_BLOCKED_TOOLS.get(config["tool_policy"], set()))

    def augment_prompt(
        self,
        *,
        prompt_parts: list[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> None:
        preserved_state = run_context.get("preserved_runtime_state")
        if isinstance(preserved_state, dict):
            prompt_parts.append(_render_preserved_runtime_prompt(preserved_state))
        if is_final_step:
            prompt_parts.append(load_profile_spec_text("topic_watcher"))
            prompt_parts.append(_RESEARCH_BRIEFING_SPEC.strip())
        else:
            prompt_parts.append(_DATASET_REVIEW_SPEC.strip())
        prepared = str(run_context.get("dataset_prompt") or "").strip()
        if prepared:
            dataset_limit = 18000 if not is_final_step else 6000
            prompt_parts.append(f"Prepared daily research briefing dataset:\n{prepared[:dataset_limit]}")

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

        config = normalize_executor_config(run_context.get("executor_config") or self.config)
        dataset = run_context.get("dataset") or {}
        rss_items = list(dataset.get("rss_items") or [])
        allowed_urls = {str(item.get("url") or "").strip() for item in rss_items if str(item.get("url") or "").strip()}
        topic = str((config.get("input") or {}).get("topic") or "").strip()
        text = (result or "").strip()
        if text == _NO_SIGNIFICANT_UPDATES:
            if rss_items:
                return "", {
                    "fatal": True,
                    "fatal_reason": "Briefing returned no-update despite prepared RSS matches being available.",
                }
            entry = _render_no_update_entry(topic=topic)
            return entry, {
                "fatal": False,
                "no_update": True,
                "topic": topic,
                "selected_count": 0,
                "appended_entry": entry,
                "entry_path": str((config.get("persistence") or {}).get("path") or "").strip(),
            }

        cleaned = _strip_memory_candidate_section(text.strip())
        if not cleaned:
            return "", {
                "fatal": True,
                "fatal_reason": "Briefing output was empty.",
            }

        bullet_lines, remaining = _extract_briefing_sections(cleaned)
        if not (1 <= len(bullet_lines) <= 6):
            return "", {
                "fatal": True,
                "fatal_reason": "Briefing must contain 1-6 bullet lines before the Implications section.",
            }
        if "Implications:" not in remaining or "Key indicators to watch:" not in remaining or "Links (from cached feeds):" not in remaining:
            return "", {
                "fatal": True,
                "fatal_reason": "Briefing is missing one or more required sections.",
            }

        urls = [u.rstrip('.,;"\'') for u in re.findall(r"https?://[^\s)\]]+", cleaned)]
        deduped_urls: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url and url not in seen:
                seen.add(url)
                deduped_urls.append(url)
        if not deduped_urls:
            return "", {
                "fatal": True,
                "fatal_reason": "Briefing must include grounded links from cached feeds.",
            }
        invalid_urls = sorted(url for url in deduped_urls if url not in allowed_urls)
        if invalid_urls:
            return "", {
                "fatal": True,
                "fatal_reason": "Briefing cited URLs outside the prepared dataset.",
                "invalid_urls": invalid_urls,
            }

        entry = _render_briefing_entry(cleaned)
        return entry, {
            "fatal": False,
            "no_update": False,
            "topic": topic,
            "selected_count": len(deduped_urls),
            "grounded_urls": deduped_urls,
            "entry_path": str((config.get("persistence") or {}).get("path") or "").strip(),
            "appended_entry": entry,
        }

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
            "executor_config": run_debug.get("executor_config", {}),
            "runtime_contract": run_debug.get("runtime_contract", {}),
            "dataset_stats": run_debug.get("dataset_stats", {}),
            "refresh_stats": run_debug.get("refresh_stats", {}),
            "append_result": run_debug.get("append_result", {}),
        }
        if isinstance(dataset, dict):
            out.append({"artifact_type": "prepared_dataset", "content_json": dataset})
        if isinstance(run_debug.get("runtime_contract"), dict):
            out.append({"artifact_type": "runtime_contract", "content_json": run_debug.get("runtime_contract")})
        if isinstance(run_debug.get("preserved_runtime_state"), dict):
            out.append({"artifact_type": "preserved_runtime_state", "content_json": run_debug.get("preserved_runtime_state")})
        if final_markdown:
            out.append({"artifact_type": "final_output", "content_text": final_markdown})
        if isinstance(grounding, dict):
            out.append({"artifact_type": "validation_report", "content_json": grounding})
        if isinstance(run_debug.get("append_result"), dict):
            out.append({"artifact_type": "workspace_append", "content_json": run_debug.get("append_result")})
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
        del db, run
        config = normalize_executor_config(task.executor_config or self.config)
        persistence = config.get("persistence") or {}
        path = str(persistence.get("path") or "").strip()
        if not path:
            raise RuntimeError("Configured executor is missing persistence.path.")

        if bool(persistence.get("write_preamble_if_missing", True)) and not workspace_path_exists(task.user_id, path):
            preamble = _render_file_preamble(str((config.get("input") or {}).get("topic") or "").strip())
            write_workspace_text(task.user_id, path, preamble)
        elif bool(persistence.get("write_preamble_if_missing", True)):
            existing = read_workspace_text(task.user_id, path)
            if not existing.strip():
                write_workspace_text(task.user_id, path, _render_file_preamble(str((config.get("input") or {}).get("topic") or "").strip()))

        duplicate_policy = _normalize_duplicate_output_policy(config.get("duplicate_output_policy"))
        existing_text = read_workspace_text(task.user_id, path)
        latest_entry = _extract_latest_briefing_entry(existing_text)
        if latest_entry and _is_similar_recent_entry(final_markdown, latest_entry):
            run_debug["append_result"] = {
                "path": path,
                "result": "Suppressed duplicate recent entry.",
                "suppressed_duplicate": True,
                "duplicate_output_policy": duplicate_policy,
            }
            return

        append_result = append_workspace_text(task.user_id, path, final_markdown.rstrip() + "\n\n")
        run_debug["append_result"] = {
            "path": path,
            "result": append_result,
            "suppressed_duplicate": False,
            "duplicate_output_policy": duplicate_policy,
        }


def resolve_task_execution_contract(task, user=None) -> TaskExecutionProfile:
    del user
    config = normalize_executor_config(getattr(task, "executor_config", None))
    if config is not None:
        return ConfiguredDailyResearchBriefingExecution(config)
    return resolve_task_profile(task)


def normalize_executor_config(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if str(value.get("kind") or "").strip().lower() != _SUPPORTED_KIND:
        return None
    if str(value.get("input_mode") or "").strip().lower() != _SUPPORTED_INPUT_MODE:
        return None
    tool_policy = _normalize_tool_policy(value.get("tool_policy"))
    if tool_policy != _SUPPORTED_TOOL_POLICY:
        return None
    if str(value.get("output_mode") or "").strip().lower() != _SUPPORTED_OUTPUT_MODE:
        return None
    if str(value.get("persistence_mode") or "").strip().lower() != _SUPPORTED_PERSISTENCE_MODE:
        return None
    if str(value.get("validation_mode") or "").strip().lower() != _SUPPORTED_VALIDATION_MODE:
        return None
    if str(value.get("notify_mode") or "").strip().lower() != _SUPPORTED_NOTIFY_MODE:
        return None
    normalized = NormalizedConfiguredExecutor(
        kind=_SUPPORTED_KIND,
        input_mode=_SUPPORTED_INPUT_MODE,
        tool_policy=tool_policy,
        output_mode=_SUPPORTED_OUTPUT_MODE,
        persistence_mode=_SUPPORTED_PERSISTENCE_MODE,
        validation_mode=_SUPPORTED_VALIDATION_MODE,
        notify_mode=_SUPPORTED_NOTIFY_MODE,
        no_update_policy=_SUPPORTED_NO_UPDATE_POLICY,
        duplicate_output_policy=_normalize_duplicate_output_policy(value.get("duplicate_output_policy")),
        input={
            "topic": str(((value.get("input") or {}).get("topic") or "")).strip(),
            "window_hours": max(1, min(int(((value.get("input") or {}).get("window_hours") or 24)), 168)),
            "threshold": _normalize_threshold((value.get("input") or {}).get("threshold")),
            "sources": [str(item).strip() for item in ((value.get("input") or {}).get("sources") or []) if str(item).strip()],
        },
        persistence={
            "path": _normalize_workspace_path((value.get("persistence") or {}).get("path")),
            "write_preamble_if_missing": bool((value.get("persistence") or {}).get("write_preamble_if_missing", True)),
        },
    )
    if not normalized.input["topic"] or not normalized.persistence["path"]:
        return None
    return normalized.to_dict()


def infer_configured_executor(
    *,
    title: str,
    instruction: str,
    task_type: str,
    requested_profile: Optional[str],
) -> InferredConfiguredExecutor:
    if requested_profile:
        return InferredConfiguredExecutor(profile=requested_profile, executor_config={})
    if task_type not in {"recurring", "one_shot"}:
        return InferredConfiguredExecutor(profile=None, executor_config={})

    lowered = f"{title}\n{instruction}".lower()
    if not any(marker in lowered for marker in ("append", "write", "save", "produce")):
        return InferredConfiguredExecutor(profile=None, executor_config={})
    path = _extract_workspace_report_path(instruction)
    if not path:
        return InferredConfiguredExecutor(profile=None, executor_config={})
    if not any(marker in lowered for marker in ("news", "rss", "feed", "feeds", "developments", "briefing", "research")):
        return InferredConfiguredExecutor(profile=None, executor_config={})
    topic = _extract_topic(title, instruction)
    if not topic:
        return InferredConfiguredExecutor(profile=None, executor_config={})

    config = {
        "kind": _SUPPORTED_KIND,
        "input_mode": _SUPPORTED_INPUT_MODE,
        "tool_policy": _SUPPORTED_TOOL_POLICY,
        "output_mode": _SUPPORTED_OUTPUT_MODE,
        "persistence_mode": _SUPPORTED_PERSISTENCE_MODE,
        "validation_mode": _SUPPORTED_VALIDATION_MODE,
        "notify_mode": _SUPPORTED_NOTIFY_MODE,
        "no_update_policy": _SUPPORTED_NO_UPDATE_POLICY,
        "duplicate_output_policy": _SUPPORTED_DUPLICATE_OUTPUT_POLICY,
        "input": {
            "topic": topic,
            "window_hours": _extract_window_hours(instruction),
            "threshold": "medium",
            "sources": [],
        },
        "persistence": {
            "path": path,
            "write_preamble_if_missing": True,
        },
    }
    return InferredConfiguredExecutor(profile=None, executor_config=config)


def _extract_workspace_report_path(instruction: str) -> str:
    text = str(instruction or "")
    match = re.search(
        r"(?:append|write|save|produce)(?:\s+\w+){0,10}\s+(?:to|into)\s+(?:the\s+)?(?:workspace\s+)?(?:file\s+|folder\s+|path\s+)?['\"`]?([A-Za-z0-9_./ \-]+\.md)['\"`]?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(r"([A-Za-z0-9_./ \-]+\.md)\b", text)
    if not match:
        return ""
    return _normalize_workspace_path(match.group(1))


def _extract_topic(title: str, instruction: str) -> str:
    text = re.sub(r"\s+", " ", str(instruction or "").strip())
    patterns = [
        r"(?:news|developments|briefing|research)\s+about\s+(.+?)\s+from\s+the\s+past\s+\d+\s+hours",
        r"(?:news|developments|briefing|research)\s+about\s+(.+?)\s+from\s+the\s+previous\s+\d+\s*hours",
        r"collect(?:s|ing)?\s+the\s+(?:previous|past)\s+\d+\s*hours?\s+of\s+news.+?\s+about\s+(.+?)(?:\s+and\s+(?:provide|write|append)|[.])",
        r"covering\s+the\s+(?:previous|past)\s+\d+\s*hours\s+that\s+mention\s+[\"']?(.+?)[\"']?(?:\s+or\s+directly\s+relate|\s*[).,])",
        r"analyze\s+the\s+news\s+about\s+(.+?)\s+from\s+the\s+past\s+\d+\s+hours",
        r"analyze\s+the\s+news\s+about\s+(.+?)\s+from\s+the\s+previous\s+\d+\s*hours",
        r"for\s+(.+?)\s+from\s+the\s+past\s+\d+\s+hours",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = _clean_topic(match.group(1))
            if candidate:
                return candidate
    title_match = re.search(r"daily\s+(.+?)\s+(?:24[\-‑ ]hour\s+)?summary", title, flags=re.IGNORECASE)
    if title_match:
        candidate = _clean_topic(title_match.group(1))
        if candidate:
            return candidate
    title_match = re.search(r"(.+?)\s+daily\s+(?:24[\-‑ ]hour\s+)?summary", title, flags=re.IGNORECASE)
    if title_match:
        candidate = _clean_topic(title_match.group(1))
        if candidate:
            return candidate
    title_match = re.search(r"daily\s+(.+?)\s+(?:24[\-‑ ]hour\s+)?analysis", title, flags=re.IGNORECASE)
    if title_match:
        candidate = _clean_topic(title_match.group(1))
        if candidate:
            return candidate
    title_match = re.search(r"(.+?)\s+daily\s+(?:24[\-‑ ]hour\s+)?analysis", title, flags=re.IGNORECASE)
    if title_match:
        candidate = _clean_topic(title_match.group(1))
        if candidate:
            return candidate
    title_match = re.search(r"daily\s+(.+?)\s+(?:developments\s+)?briefing", title, flags=re.IGNORECASE)
    if title_match:
        candidate = _clean_topic(title_match.group(1))
        if candidate:
            return candidate
    title_match = re.search(r"(.+?)\s+daily\s+(?:developments\s+)?briefing", title, flags=re.IGNORECASE)
    if title_match:
        candidate = _clean_topic(title_match.group(1))
        if candidate:
            return candidate
    return ""


def _extract_window_hours(instruction: str) -> int:
    match = re.search(r"past\s+(\d+)\s+hours", str(instruction or ""), flags=re.IGNORECASE)
    if not match:
        return 24
    try:
        return max(1, min(int(match.group(1)), 168))
    except ValueError:
        return 24


def _clean_topic(value: str) -> str:
    candidate = re.sub(r"\s+", " ", str(value or "").strip(" .,:;\"'`"))
    candidate = re.sub(r"\b(?:the\s+)?wider\s+", "", candidate, flags=re.IGNORECASE)
    return candidate.strip()


def _normalize_threshold(value: Any) -> str:
    threshold = str(value or "medium").strip().lower()
    if threshold not in {"low", "medium", "high"}:
        return "medium"
    return threshold


def _normalize_tool_policy(value: Any) -> str:
    policy = str(value or _SUPPORTED_TOOL_POLICY).strip().lower()
    if policy not in _TOOL_POLICY_BLOCKED_TOOLS:
        return ""
    return policy


def _normalize_duplicate_output_policy(value: Any) -> str:
    policy = str(value or _SUPPORTED_DUPLICATE_OUTPUT_POLICY).strip().lower()
    if policy != _SUPPORTED_DUPLICATE_OUTPUT_POLICY:
        return _SUPPORTED_DUPLICATE_OUTPUT_POLICY
    return policy


def build_preserved_runtime_state(
    *,
    executor_config: dict[str, Any],
    step_index: int,
    step_title: str,
    step_instruction: str,
    is_final_step: bool,
    dataset: dict[str, Any] | None,
    prior_step_summaries: list[str],
    active_skill_slugs: list[str],
    skill_injection_details: list[dict[str, Any]],
) -> dict[str, Any]:
    input_config = executor_config.get("input") or {}
    persistence = executor_config.get("persistence") or {}
    rss_items = list((dataset or {}).get("rss_items") or [])
    selected_items = []
    for item in rss_items[:6]:
        selected_items.append(
            {
                "title": str(item.get("title") or "").strip(),
                "source": str(item.get("source") or "").strip(),
                "url": str(item.get("url") or "").strip(),
            }
        )
    active_skill_summary = []
    details_by_slug = {
        str(item.get("slug") or ""): item
        for item in skill_injection_details
        if isinstance(item, dict) and item.get("included")
    }
    for slug in active_skill_slugs:
        detail = details_by_slug.get(str(slug), {})
        active_skill_summary.append(
            {
                "slug": str(slug),
                "reason": str(detail.get("reason") or "").strip(),
            }
        )
    compact_prior_summaries = [_compact_prior_step_summary(text) for text in prior_step_summaries]
    compact_prior_summaries = [text for text in compact_prior_summaries if text]
    return {
        "runtime_contract": {
            "kind": executor_config.get("kind"),
            "input_mode": executor_config.get("input_mode"),
            "tool_policy": executor_config.get("tool_policy"),
            "output_mode": executor_config.get("output_mode"),
            "persistence_mode": executor_config.get("persistence_mode"),
            "validation_mode": executor_config.get("validation_mode"),
            "notify_mode": executor_config.get("notify_mode"),
            "no_update_policy": executor_config.get("no_update_policy"),
            "duplicate_output_policy": executor_config.get("duplicate_output_policy"),
        },
        "current_step": {
            "step_index": step_index,
            "title": step_title,
            "instruction": step_instruction,
            "is_final_step": is_final_step,
        },
        "input_summary": {
            "topic": str(input_config.get("topic") or "").strip(),
            "window_hours": int(input_config.get("window_hours") or 24),
            "threshold": str(input_config.get("threshold") or "medium"),
            "selected_item_count": len(rss_items),
            "selected_items": selected_items,
        },
        "persistence_target": {
            "path": str(persistence.get("path") or "").strip(),
            "write_preamble_if_missing": bool(persistence.get("write_preamble_if_missing", True)),
        },
        "active_skills_summary": active_skill_summary,
        "prior_step_summaries": compact_prior_summaries,
    }


def _compact_prior_step_summary(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    if not first_line:
        return ""
    compact = re.sub(r"\[(.*?)\]\((https?://[^)]+)\)", r"\1", first_line)
    compact = re.sub(r"\*\*(.*?)\*\*", r"\1", compact)
    compact = re.sub(r"\s+", " ", compact).strip()
    if len(compact) > 180:
        compact = compact[:177].rstrip() + "..."
    return compact


def _render_preserved_runtime_prompt(state: dict[str, Any]) -> str:
    contract = state.get("runtime_contract") or {}
    step = state.get("current_step") or {}
    input_summary = state.get("input_summary") or {}
    persistence = state.get("persistence_target") or {}
    skill_summary = list(state.get("active_skills_summary") or [])
    prior_summaries = list(state.get("prior_step_summaries") or [])

    lines = ["Preserved runtime contract for this configured executor run:"]
    lines.append(
        "- Contract: "
        f"input={contract.get('input_mode')}, "
        f"tools={contract.get('tool_policy')}, "
        f"output={contract.get('output_mode')}, "
        f"persistence={contract.get('persistence_mode')}, "
        f"validation={contract.get('validation_mode')}"
    )
    lines.append(
        "- Input summary: "
        f"topic='{input_summary.get('topic')}', "
        f"window_hours={input_summary.get('window_hours')}, "
        f"selected_items={input_summary.get('selected_item_count')}"
    )
    selected_items = list(input_summary.get("selected_items") or [])
    if selected_items:
        lines.append("- Selected prepared items:")
        for item in selected_items[:6]:
            lines.append(
                f"  - {item.get('title') or 'Untitled'} | {item.get('source') or 'Unknown source'} | {item.get('url') or ''}"
            )
    lines.append(
        "- Persistence target: "
        f"path='{persistence.get('path')}', "
        f"write_preamble_if_missing={persistence.get('write_preamble_if_missing')}"
    )
    lines.append(
        "- Current step: "
        f"{step.get('step_index')}. {step.get('title')} "
        f"(final_synthesis={step.get('is_final_step')})"
    )
    if skill_summary:
        lines.append("- Active skills:")
        for item in skill_summary:
            reason = str(item.get("reason") or "").strip()
            suffix = f" — {reason}" if reason else ""
            lines.append(f"  - {item.get('slug')}{suffix}")
    if prior_summaries:
        lines.append("- Prior step summaries:")
        for summary in prior_summaries:
            lines.append(f"  - {summary}")
    return "\n".join(lines)


def _normalize_workspace_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    path = re.sub(r"^(?:at\s+)+", "", path, flags=re.IGNORECASE)
    path = path.lstrip("/")
    if ".." in path.split("/"):
        return ""
    return path


def _extract_latest_briefing_entry(text: str) -> str:
    raw = str(text or "")
    matches = list(re.finditer(r"(?m)^\d{8}T\d{6}Z\s*$", raw))
    if not matches:
        return ""
    start = matches[-1].start()
    return raw[start:].strip()


def _parse_compact_utc_stamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}T\d{6}Z", text):
        return None
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _prune_recently_reported_items(
    items: list[dict[str, Any]],
    *,
    latest_entry: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stats = {
        "recent_entry_considered": False,
        "recent_overlap_count": 0,
        "recent_repeat_pruned_count": 0,
    }
    if not latest_entry:
        return items, stats
    lines = [line.strip() for line in str(latest_entry).splitlines() if line.strip()]
    if not lines:
        return items, stats
    latest_stamp = _parse_compact_utc_stamp(lines[0])
    if latest_stamp is None:
        return items, stats
    stats["recent_entry_considered"] = True
    if (now - latest_stamp).total_seconds() > 2 * 60 * 60:
        return items, stats
    recent_urls = _extract_link_set(latest_entry)
    if not recent_urls:
        return items, stats
    overlapping: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        if url and url in recent_urls:
            overlapping.append(item)
        else:
            fresh.append(item)
    stats["recent_overlap_count"] = len(overlapping)
    if len(overlapping) < 2 or not fresh:
        return items, stats
    pruned = overlapping[:1] + fresh
    stats["recent_repeat_pruned_count"] = len(items) - len(pruned)
    return pruned, stats


def _normalize_title_for_cluster(title: str) -> str:
    text = str(title or "").strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"\bu\.s\.\b", "us", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\b(the|a|an)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _apply_light_title_cluster_diversity(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats = {
        "title_cluster_count": 0,
        "title_cluster_pruned_count": 0,
    }
    if len(items) < 3:
        return items, stats

    kept: list[dict[str, Any]] = []
    normalized_titles: list[str] = []
    cluster_count = 0
    pruned_count = 0

    for item in items:
        title = _normalize_title_for_cluster(str(item.get("title") or ""))
        if not title:
            kept.append(item)
            normalized_titles.append("")
            continue

        matched = False
        for existing_title in normalized_titles:
            if not existing_title:
                continue
            similarity = difflib.SequenceMatcher(a=title, b=existing_title).ratio()
            if similarity >= 0.82:
                matched = True
                cluster_count += 1
                pruned_count += 1
                break
        if not matched:
            kept.append(item)
            normalized_titles.append(title)

    stats["title_cluster_count"] = cluster_count
    stats["title_cluster_pruned_count"] = pruned_count
    return kept, stats


def _normalize_entry_for_similarity(text: str) -> str:
    body_lines = str(text or "").splitlines()
    if body_lines and re.fullmatch(r"\d{8}T\d{6}Z", body_lines[0].strip()):
        body_lines = body_lines[1:]
    normalized = "\n".join(body_lines)
    normalized = re.sub(r"\[(.*?)\]\((https?://[^)]+)\)", r"\1 \2", normalized)
    normalized = re.sub(r"\*\*(.*?)\*\*", r"\1", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{2,}", "\n", normalized)
    return normalized.strip().lower()


def _extract_link_set(text: str) -> set[str]:
    return {match.group(0).rstrip(").,") for match in re.finditer(r"https?://\S+", str(text or ""))}


def _is_similar_recent_entry(candidate: str, previous: str) -> bool:
    candidate_links = _extract_link_set(candidate)
    previous_links = _extract_link_set(previous)
    if not candidate_links or candidate_links != previous_links:
        return False
    candidate_norm = _normalize_entry_for_similarity(candidate)
    previous_norm = _normalize_entry_for_similarity(previous)
    if not candidate_norm or not previous_norm:
        return False
    similarity = difflib.SequenceMatcher(a=candidate_norm, b=previous_norm).ratio()
    return similarity >= 0.94


def _extract_briefing_sections(text: str) -> tuple[list[str], str]:
    if "Implications:" not in text:
        return [], text
    before, after = text.split("Implications:", 1)
    bullets = [
        line.strip()
        for line in before.splitlines()
        if line.strip().startswith("- ")
    ]
    return bullets, f"Implications:{after}".strip()


def _render_briefing_entry(body: str) -> str:
    stamp = utc_compact_timestamp(datetime.now(timezone.utc))
    return f"{stamp}\n{body.strip()}"


def _render_no_update_entry(*, topic: str) -> str:
    stamp = utc_compact_timestamp(datetime.now(timezone.utc))
    return (
        f"{stamp}\n"
        f"- No significant developments were identified for {topic} in the prepared cached feeds during this window.\n\n"
        "Implications:\n"
        "- No material shift met the configured threshold in the cached source set.\n\n"
        "Key indicators to watch:\n"
        "- Escalation, diplomatic, sanctions, energy, or leadership developments in the next window.\n\n"
        "Links (from cached feeds):\n"
        "- none"
    )


def _render_file_preamble(topic: str) -> str:
    topic_label = topic or "Daily Research Briefing"
    return (
        f"# {topic_label} Developments\n\n"
        "Purpose: Rolling append-only briefing generated from cached RSS topic research.\n"
        "Format: Each entry contains a UTC timestamp, 1-6 grounded bullets, implications, key indicators to watch, and cached-feed links.\n\n"
    )


def _strip_memory_candidate_section(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return body
    stripped = re.split(r"\n## Memory candidate(?:s)?\n", body, maxsplit=1, flags=re.IGNORECASE)[0].rstrip()
    return stripped or body
