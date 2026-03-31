from __future__ import annotations

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
_SUPPORTED_OUTPUT_MODE = "daily_research_briefing"
_SUPPORTED_PERSISTENCE_MODE = "append_workspace_file"
_SUPPORTED_VALIDATION_MODE = "grounded_briefing"
_SUPPORTED_NOTIFY_MODE = "appended_summary"
_SUPPORTED_NO_UPDATE_POLICY = "append_entry"

_NO_SIGNIFICANT_UPDATES = "NO_SIGNIFICANT_UPDATES"
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
            },
            "refresh_stats": dataset.get("refresh", {}),
        }

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        del run_context
        return {
            "add_memory_observations",
            "api_request",
            "create_and_run_task_plan",
            "create_event",
            "create_memory",
            "create_memory_entities",
            "create_memory_relations",
            "create_task",
            "create_task_plan",
            "delete_event",
            "fetch_page",
            "get_daily_market_data",
            "get_intraday_market_data",
            "get_task",
            "list_library_documents",
            "list_recent_feed_items",
            "list_rss_sources",
            "list_tasks",
            "list_directory",
            "find_files",
            "stat_file",
            "read_file",
            "write_file",
            "append_file",
            "make_directory",
            "open_memory_graph_nodes",
            "run_task_now",
            "search_feeds",
            "search_library",
            "search_memory_graph",
            "search_my_feeds",
            "search_places",
            "summarize_document",
            "update_task",
            "web_search",
        }

    def augment_prompt(
        self,
        *,
        prompt_parts: list[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> None:
        del is_final_step
        prompt_parts.append(load_profile_spec_text("topic_watcher"))
        prompt_parts.append(_RESEARCH_BRIEFING_SPEC.strip())
        prepared = str(run_context.get("dataset_prompt") or "").strip()
        if prepared:
            prompt_parts.append(f"Prepared daily research briefing dataset:\n{prepared[:18000]}")

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
            "dataset_stats": run_debug.get("dataset_stats", {}),
            "refresh_stats": run_debug.get("refresh_stats", {}),
            "append_result": run_debug.get("append_result", {}),
        }
        if isinstance(dataset, dict):
            out.append({"artifact_type": "prepared_dataset", "content_json": dataset})
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

        append_result = append_workspace_text(task.user_id, path, final_markdown.rstrip() + "\n\n")
        run_debug["append_result"] = {
            "path": path,
            "result": append_result,
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
    if str(value.get("output_mode") or "").strip().lower() != _SUPPORTED_OUTPUT_MODE:
        return None
    if str(value.get("persistence_mode") or "").strip().lower() != _SUPPORTED_PERSISTENCE_MODE:
        return None
    if str(value.get("validation_mode") or "").strip().lower() != _SUPPORTED_VALIDATION_MODE:
        return None
    if str(value.get("notify_mode") or "").strip().lower() != _SUPPORTED_NOTIFY_MODE:
        return None
    normalized = {
        "kind": _SUPPORTED_KIND,
        "input_mode": _SUPPORTED_INPUT_MODE,
        "output_mode": _SUPPORTED_OUTPUT_MODE,
        "persistence_mode": _SUPPORTED_PERSISTENCE_MODE,
        "validation_mode": _SUPPORTED_VALIDATION_MODE,
        "notify_mode": _SUPPORTED_NOTIFY_MODE,
        "no_update_policy": _SUPPORTED_NO_UPDATE_POLICY,
        "input": {
            "topic": str(((value.get("input") or {}).get("topic") or "")).strip(),
            "window_hours": max(1, min(int(((value.get("input") or {}).get("window_hours") or 24)), 168)),
            "threshold": _normalize_threshold((value.get("input") or {}).get("threshold")),
            "sources": [str(item).strip() for item in ((value.get("input") or {}).get("sources") or []) if str(item).strip()],
        },
        "persistence": {
            "path": _normalize_workspace_path((value.get("persistence") or {}).get("path")),
            "write_preamble_if_missing": bool((value.get("persistence") or {}).get("write_preamble_if_missing", True)),
        },
    }
    if not normalized["input"]["topic"] or not normalized["persistence"]["path"]:
        return None
    return normalized


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
    if not any(marker in lowered for marker in ("append", "write")):
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
        "output_mode": _SUPPORTED_OUTPUT_MODE,
        "persistence_mode": _SUPPORTED_PERSISTENCE_MODE,
        "validation_mode": _SUPPORTED_VALIDATION_MODE,
        "notify_mode": _SUPPORTED_NOTIFY_MODE,
        "no_update_policy": _SUPPORTED_NO_UPDATE_POLICY,
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
        r"(?:append|write)(?:\s+\w+){0,6}\s+(?:to|into)\s+(?:the\s+)?(?:workspace\s+)?(?:file\s+)?['\"`]?([A-Za-z0-9_./-]+\.md)['\"`]?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(r"\b([A-Za-z0-9_./-]+\.md)\b", text)
    if not match:
        return ""
    return _normalize_workspace_path(match.group(1))


def _extract_topic(title: str, instruction: str) -> str:
    text = re.sub(r"\s+", " ", str(instruction or "").strip())
    patterns = [
        r"(?:news|developments|briefing|research)\s+about\s+(.+?)\s+from\s+the\s+past\s+\d+\s+hours",
        r"analyze\s+the\s+news\s+about\s+(.+?)\s+from\s+the\s+past\s+\d+\s+hours",
        r"for\s+(.+?)\s+from\s+the\s+past\s+\d+\s+hours",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = _clean_topic(match.group(1))
            if candidate:
                return candidate
    title_match = re.search(r"daily\s+(.+?)\s+(?:developments\s+)?briefing", title, flags=re.IGNORECASE)
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


def _normalize_workspace_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    path = path.lstrip("/")
    if ".." in path.split("/"):
        return ""
    return path


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
