from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.autonomy.magazine_pipeline import build_magazine_dataset
from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.spec_loader import load_profile_spec_text
from app.db.models import Task, User
from app.mcp.servers.calendar import _dedupe_events, _get_provider

_MAX_RSS_ITEMS = 8
_MAX_WORDS = 600
_PLACEHOLDER_HEADINGS = {
    "today",
    "news",
    "update",
    "updates",
}
_EMPTY_RESULT = "Nothing to brief today - no calendar events and no fresh headlines."


class MorningBriefingExecutionProfile(TaskExecutionProfile):
    name = "morning_briefing"

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
                "title": "Assemble Morning Briefing",
                "instruction": "Assemble the morning briefing from the prepared calendar and RSS datasets only.",
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
        user = await db.get(User, user_id)
        tz_name = getattr(task, "active_hours_tz", None) or getattr(user, "active_hours_tz", None) or "UTC"
        local_now = datetime.now(ZoneInfo(tz_name))
        day_start = datetime.combine(local_now.date(), time.min, tzinfo=local_now.tzinfo)
        day_end = day_start + timedelta(days=1)

        provider = _get_provider()
        events: list[dict[str, Any]] = []
        calendar_error = ""
        if provider is not None:
            try:
                events = await provider.list_events(
                    calendar_id=None,
                    start=day_start.astimezone(timezone.utc).isoformat(),
                    end=day_end.astimezone(timezone.utc).isoformat(),
                    max_results=20,
                )
                events = _dedupe_events(events)
            except Exception as exc:
                calendar_error = str(exc)

        rss_dataset = await build_magazine_dataset(
            db,
            user_id=user_id,
            task_id=task_id,
            run_id=task_run_id or 0,
            refresh=True,
            window_hours=24,
            max_items=24,
        )
        rss_items = list(rss_dataset.get("items") or [])[:_MAX_RSS_ITEMS]
        dataset = {
            "calendar_events": [_normalize_event(ev, local_now) for ev in events],
            "rss_items": rss_items,
            "timezone": tz_name,
            "local_date": local_now.date().isoformat(),
        }
        return {
            "dataset": dataset,
            "dataset_prompt": _format_prompt_dataset(dataset),
            "dataset_stats": {
                "calendar_count": len(dataset["calendar_events"]),
                "rss_count": len(rss_items),
                "rss_dataset_stats": rss_dataset.get("stats", {}),
            },
            "refresh_stats": {
                "rss_refresh": rss_dataset.get("refresh", {}),
                "calendar_error": calendar_error,
            },
        }

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        del run_context
        return {
            # This profile should synthesize only from the prepared calendar and
            # RSS datasets. Leaving unrelated task/memory/admin tools visible
            # makes local models more brittle without improving the result.
            "add_memory_observations",
            "api_request",
            "create_and_run_task_plan",
            "create_memory",
            "create_memory_entities",
            "create_memory_relations",
            "create_task",
            "create_task_plan",
            "get_daily_market_data",
            "get_feed_items",
            "get_intraday_market_data",
            "get_task",
            "list_tasks",
            "search_feeds",
            "search_my_feeds",
            "list_recent_feed_items",
            "search_library",
            "search_memory_graph",
            "search_places",
            "summarize_document",
            "list_library_documents",
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
        del is_final_step
        prompt_parts.append(load_profile_spec_text(self.name))
        dataset = run_context.get("dataset") or {}
        if dataset.get("calendar_events"):
            prompt_parts.append(
                "Calendar events are present in the prepared dataset. You must include a `## Today at a glance` section before any headlines."
            )
        prepared = (run_context.get("dataset_prompt") or "").strip()
        if prepared:
            prompt_parts.append(f"Prepared briefing dataset:\n{prepared[:18000]}")

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

        dataset = run_context.get("dataset") or {}
        events = list(dataset.get("calendar_events") or [])
        rss_items = list(dataset.get("rss_items") or [])
        allowed_urls = {str(item.get("url") or "").strip() for item in rss_items if item.get("url")}
        text = (result or "").strip()
        if not text:
            text = _EMPTY_RESULT if not events and not rss_items else text

        if not events and not rss_items:
            return _EMPTY_RESULT, {
                "fatal": False,
                "publish_mode": "empty",
                "empty_result": True,
                "calendar_count": 0,
                "rss_count": 0,
            }

        urls = [u.rstrip('.,;"\'') for u in re.findall(r"https?://[^\s)\]]+", text)]
        invalid_urls = sorted({u for u in urls if u and u not in allowed_urls})
        word_count = len(re.findall(r"\S+", text))
        placeholder_hits = sum(
            1
            for match in re.findall(r"^##\s+(.+)$", text, flags=re.MULTILINE)
            if _is_placeholder_heading(match)
        )
        has_calendar = "## Today at a glance" in text
        has_headlines = "## Headlines" in text
        has_attention = "## Worth your attention" in text
        fatal = False
        fatal_reason = ""
        if invalid_urls:
            fatal = True
            fatal_reason = "Morning briefing contains URL(s) not present in prepared RSS dataset."
        elif word_count > _MAX_WORDS:
            fatal = True
            fatal_reason = f"Morning briefing exceeded {_MAX_WORDS} words."
        elif placeholder_hits:
            fatal = True
            fatal_reason = "Morning briefing contains placeholder section headings."
        elif events and not has_calendar:
            fatal = True
            fatal_reason = "Morning briefing omitted the required calendar section despite prepared calendar events."
        elif not has_calendar and not has_headlines:
            fatal = True
            fatal_reason = "Morning briefing did not produce any publishable section."

        report = {
            "fatal": fatal,
            "fatal_reason": fatal_reason,
            "publish_mode": "full" if not fatal else "invalid",
            "calendar_count": len(events),
            "rss_count": len(rss_items),
            "word_count": word_count,
            "invalid_urls": invalid_urls,
            "has_calendar_section": has_calendar,
            "has_headlines_section": has_headlines,
            "has_attention_section": has_attention,
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
        if isinstance(dataset, dict):
            out.append({"artifact_type": "prepared_dataset", "content_json": dataset})
        if final_markdown:
            out.append({"artifact_type": "final_output", "content_text": final_markdown})
        if isinstance(grounding, dict):
            out.append({"artifact_type": "validation_report", "content_json": grounding})
        out.extend(super().artifact_payloads(final_markdown="", run_debug=run_debug))
        return out


def _normalize_event(event: Dict[str, Any], now_local: datetime) -> Dict[str, Any]:
    start_raw = str(event.get("start") or "")
    end_raw = str(event.get("end") or "")
    start_dt = _coerce_dt(start_raw, now_local.tzinfo or timezone.utc)
    within_2h = False
    if start_dt is not None:
        delta = (start_dt - now_local).total_seconds()
        within_2h = 0 <= delta <= 7200
    return {
        "id": event.get("id") or "",
        "title": str(event.get("summary") or "Untitled").strip(),
        "start": start_raw,
        "end": end_raw,
        "location": str(event.get("location") or "").strip(),
        "description": str(event.get("description") or "").strip(),
        "starts_within_2h": within_2h,
    }


def _coerce_dt(value: str, tzinfo) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tzinfo)
    except Exception:
        return None


def _format_prompt_dataset(dataset: Dict[str, Any]) -> str:
    lines = [
        f"Local date: {dataset.get('local_date')}",
        f"Timezone: {dataset.get('timezone')}",
        "",
        "Calendar events:",
    ]
    events = list(dataset.get("calendar_events") or [])
    if not events:
        lines.append("- none")
    else:
        for event in events:
            lines.append(
                f"- {event.get('start')} title={event.get('title')} location={event.get('location') or '-'} starts_within_2h={event.get('starts_within_2h')}"
            )
    lines.append("")
    lines.append("RSS items:")
    rss_items = list(dataset.get("rss_items") or [])
    if not rss_items:
        lines.append("- none")
    else:
        for item in rss_items:
            lines.append(
                f"- source={item.get('source')} section={item.get('section')} title={item.get('title')} url={item.get('url')}"
            )
    return "\n".join(lines)


def _is_placeholder_heading(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", (value or "").strip().lower()).strip(" :.-")
    return normalized in _PLACEHOLDER_HEADINGS
