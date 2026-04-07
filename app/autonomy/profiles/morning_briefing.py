from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.api_service import execute_api_request, fetch_daily_market_data_payload
from app.autonomy.magazine_pipeline import build_magazine_dataset
from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.spec_loader import load_profile_spec_text
from app.autonomy.profiles.weather_conditions import _build_contract as _build_weather_contract
from app.db.models import Task, User
from app.mcp.servers.calendar import _dedupe_events, _get_provider
from app.time_utils import is_valid_timezone_name, resolve_effective_timezone

_MAX_RSS_ITEMS = 8
_MAX_WORDS = 650
_PLACEHOLDER_HEADINGS = {
    "today",
    "news",
    "update",
    "updates",
}
_EMPTY_RESULT = "Nothing to brief today - no calendar events and no fresh headlines."
_SECTION_ALIASES = {
    "today_at_a_glance": ("## Today at a glance", "## Today context"),
    "market_snapshot": ("## KO market snapshot",),
    "weather": ("## Weather",),
    "history": ("## Today in history",),
    "headlines": ("## Headlines", "## Links (from cached feeds)"),
    "worth_attention": ("## Worth your attention", "## What to watch today"),
    "tomorrow_at_a_glance": ("## Tomorrow at a glance", "## What to watch tomorrow"),
    "day_in_review": ("## Day in review",),
}
_LEGACY_SECTION_NAME_MAP = {
    "today_context": "today_at_a_glance",
    "headline_bullets": "headlines",
    "links": "headlines",
    "watch_today": "worth_attention",
    "watch_tomorrow": "tomorrow_at_a_glance",
}
_DEFAULT_HEADLINE_LIMIT = 5


class BriefingExecutionProfile(TaskExecutionProfile):
    name = "briefing"

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
                "title": "Assemble Briefing",
                "instruction": "Assemble the briefing from the prepared calendar and RSS datasets only.",
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
        recipe = task.task_recipe if isinstance(getattr(task, "task_recipe", None), dict) else {}
        params = recipe.get("params") if isinstance(recipe.get("params"), dict) else {}
        briefing_mode = _resolve_briefing_mode(task=task, params=params)

        tz_name = _resolve_briefing_timezone(task=task, user=user)
        local_now = datetime.now(ZoneInfo(tz_name))
        day_start = datetime.combine(local_now.date(), time.min, tzinfo=local_now.tzinfo)
        day_end = day_start + timedelta(days=1)
        tomorrow_start = day_end
        tomorrow_end = tomorrow_start + timedelta(days=1)

        provider = _get_provider()
        today_events: list[dict[str, Any]] = []
        tomorrow_events: list[dict[str, Any]] = []
        calendar_error = ""
        if provider is not None:
            try:
                today_events = await provider.list_events(
                    calendar_id=None,
                    start=day_start.astimezone(timezone.utc).isoformat(),
                    end=day_end.astimezone(timezone.utc).isoformat(),
                    max_results=20,
                )
                tomorrow_events = await provider.list_events(
                    calendar_id=None,
                    start=tomorrow_start.astimezone(timezone.utc).isoformat(),
                    end=tomorrow_end.astimezone(timezone.utc).isoformat(),
                    max_results=20,
                )
                today_events = _dedupe_events(today_events)
                tomorrow_events = _dedupe_events(tomorrow_events)
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
        market_snapshot, market_error = await _load_market_snapshot(db=db, user_id=user_id)
        weather_snapshot, weather_error = await _load_weather_snapshot(
            db=db,
            user_id=user_id,
            task_id=task_id,
            instruction=str(getattr(task, "instruction", "") or ""),
            task_timezone=getattr(task, "active_hours_tz", None) or getattr(user, "active_hours_tz", None),
        )
        dataset = {
            "briefing_mode": briefing_mode,
            "calendar_events": [_normalize_event(ev, local_now) for ev in today_events],
            "tomorrow_events": [_normalize_event(ev, local_now) for ev in tomorrow_events],
            "rss_items": rss_items,
            "market_snapshot": market_snapshot,
            "weather_snapshot": weather_snapshot,
            "ingredients": _effective_ingredients(
                briefing_mode=briefing_mode,
                provided=_normalize_string_list(params.get("ingredients")),
                has_market_snapshot=bool(market_snapshot),
                has_weather_snapshot=bool(weather_snapshot),
            ),
            "required_sections": _effective_required_sections(params.get("sections"), briefing_mode=briefing_mode),
            "headline_limit": _resolve_headline_limit(task=task, params=params, briefing_mode=briefing_mode),
            "timezone": tz_name,
            "local_date": local_now.date().isoformat(),
        }
        return {
            "dataset": dataset,
            "dataset_prompt": _format_prompt_dataset(dataset),
            "dataset_stats": {
                "briefing_mode": briefing_mode,
                "calendar_count": len(dataset["calendar_events"]),
                "tomorrow_calendar_count": len(dataset["tomorrow_events"]),
                "rss_count": len(rss_items),
                "rss_dataset_stats": rss_dataset.get("stats", {}),
                "has_market_snapshot": bool(market_snapshot),
                "has_weather_snapshot": bool(weather_snapshot),
                },
            "refresh_stats": {
                "rss_refresh": rss_dataset.get("refresh", {}),
                "calendar_error": calendar_error,
                "market_error": market_error,
                "weather_error": weather_error,
            },
        }

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        del run_context
        return {
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
        prompt_parts.append(load_profile_spec_text("morning_briefing"))
        dataset = run_context.get("dataset") or {}
        briefing_mode = str(dataset.get("briefing_mode") or "morning")
        if briefing_mode == "evening":
            prompt_parts.append(
                "This is an evening briefing. Prefer `## Day in review`, then `## What to watch tomorrow` or `## Tomorrow at a glance` when tomorrow events exist, followed by `## Links (from cached feeds)` or equivalent grounded headline sections."
            )
        elif dataset.get("calendar_events"):
            prompt_parts.append(
                "Calendar events are present in the prepared dataset. You must include either `## Today context` or `## Today at a glance` before any headlines or watch items."
            )
        required_sections = [str(item).strip() for item in (dataset.get("required_sections") or []) if str(item).strip()]
        headline_limit = int(dataset.get("headline_limit") or _DEFAULT_HEADLINE_LIMIT)
        if briefing_mode == "morning":
            prompt_parts.append(
                "Morning briefing contract: include the required section set with explicit empty-state stubs when prepared data is unavailable."
            )
            if required_sections:
                prompt_parts.append(
                    "Required morning sections:\n- " + "\n- ".join(_display_section_name(name) for name in required_sections)
                )
            prompt_parts.append(
                f"Include at most {headline_limit} headline bullets, and each headline bullet must include a one-line summary before the read-more link."
            )
            prompt_parts.append(
                "If market or weather data is not available in the prepared dataset, include those sections and write `No update available in prepared data.`"
            )
            prompt_parts.append(
                "For `## Today in history`, prefer any prepared history fact when present. If none is prepared, you may write a short 1-2 sentence item from general model knowledge for the current calendar date."
            )
            prompt_parts.append(
                "If there are no events today or tomorrow, still include the calendar sections with `No events scheduled today.` and `No events scheduled tomorrow.`"
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
        briefing_mode = str(dataset.get("briefing_mode") or "morning")
        events = list(dataset.get("calendar_events") or [])
        tomorrow_events = list(dataset.get("tomorrow_events") or [])
        rss_items = list(dataset.get("rss_items") or [])
        required_sections = [str(item).strip() for item in (dataset.get("required_sections") or []) if str(item).strip()]
        headline_limit = int(dataset.get("headline_limit") or _DEFAULT_HEADLINE_LIMIT)
        allowed_urls = {str(item.get("url") or "").strip() for item in rss_items if item.get("url")}
        text = (result or "").strip()
        if not text:
            text = _EMPTY_RESULT if not events and not rss_items else text

        if not events and not rss_items and not tomorrow_events:
            return _EMPTY_RESULT, {
                "fatal": False,
                "publish_mode": "empty",
                "empty_result": True,
                "briefing_mode": briefing_mode,
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
        has_calendar = _has_section(text, "today_at_a_glance")
        has_market = _has_section(text, "market_snapshot")
        has_weather = _has_section(text, "weather")
        has_history = _has_section(text, "history")
        has_day_review = _has_section(text, "day_in_review")
        has_tomorrow = _has_section(text, "tomorrow_at_a_glance")
        has_headlines = _has_section(text, "headlines")
        has_attention = _has_section(text, "worth_attention")
        headline_count = _count_headline_bullets(text)
        headline_bullets_have_summaries = _headline_bullets_have_summaries(text)
        fatal = False
        fatal_reason = ""
        if invalid_urls:
            fatal = True
            fatal_reason = "Briefing contains URL(s) not present in prepared RSS dataset."
        elif word_count > _MAX_WORDS:
            fatal = True
            fatal_reason = f"Briefing exceeded {_MAX_WORDS} words."
        elif placeholder_hits:
            fatal = True
            fatal_reason = "Briefing contains placeholder section headings."
        elif briefing_mode == "morning" and events and not has_calendar:
            fatal = True
            fatal_reason = "Morning briefing omitted the required calendar section despite prepared calendar events."
        elif briefing_mode == "evening" and not has_day_review:
            fatal = True
            fatal_reason = "Evening briefing omitted the required day-in-review section."
        elif briefing_mode == "evening" and tomorrow_events and not has_tomorrow:
            fatal = True
            fatal_reason = "Evening briefing omitted the required tomorrow section despite prepared next-day events."
        elif briefing_mode == "morning" and required_sections:
            missing = [name for name in required_sections if not _has_section(text, name)]
            if missing:
                fatal = True
                fatal_reason = "Morning briefing omitted required sections: " + ", ".join(_display_section_name(name) for name in missing)
        elif briefing_mode == "morning" and not has_calendar and not has_headlines and not has_attention:
            fatal = True
            fatal_reason = "Morning briefing did not produce any publishable section."
        elif briefing_mode == "evening" and not has_day_review and not has_tomorrow and not has_headlines:
            fatal = True
            fatal_reason = "Evening briefing did not produce any publishable section."
        elif briefing_mode == "morning" and headline_count > headline_limit:
            fatal = True
            fatal_reason = f"Morning briefing exceeded the allowed {headline_limit} headlines."
        elif briefing_mode == "morning" and has_headlines and not headline_bullets_have_summaries:
            fatal = True
            fatal_reason = "Morning briefing headline bullets must include one-line summaries."

        report = {
            "fatal": fatal,
            "fatal_reason": fatal_reason,
            "publish_mode": "full" if not fatal else "invalid",
            "briefing_mode": briefing_mode,
            "calendar_count": len(events),
            "tomorrow_calendar_count": len(tomorrow_events),
            "rss_count": len(rss_items),
            "word_count": word_count,
            "invalid_urls": invalid_urls,
            "has_calendar_section": has_calendar,
            "has_market_section": has_market,
            "has_weather_section": has_weather,
            "has_history_section": has_history,
            "has_day_review_section": has_day_review,
            "has_tomorrow_section": has_tomorrow,
            "has_headlines_section": has_headlines,
            "has_attention_section": has_attention,
            "headline_count": headline_count,
            "headline_limit": headline_limit,
            "headline_bullets_have_summaries": headline_bullets_have_summaries,
            "required_sections": required_sections,
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


MorningBriefingExecutionProfile = BriefingExecutionProfile


def _resolve_briefing_mode(*, task: Task, params: Dict[str, Any]) -> str:
    explicit = str(params.get("briefing_mode") or "").strip().lower()
    if explicit in {"morning", "evening"}:
        return explicit
    title = str(getattr(task, "title", "") or "").lower()
    instruction = str(getattr(task, "instruction", "") or "").lower()
    if getattr(task, "profile", "") == "morning_briefing":
        return "morning"
    if "evening" in title or "evening" in instruction or "tonight" in instruction:
        return "evening"
    return "morning"


def _resolve_briefing_timezone(*, task: Task, user: User) -> str:
    task_tz = getattr(task, "active_hours_tz", None)
    user_tz = getattr(user, "active_hours_tz", None)
    effective = resolve_effective_timezone(task_tz, user_tz)
    if effective != "UTC":
        return effective
    instruction = str(getattr(task, "instruction", "") or "")
    weather_contract = _build_weather_contract(instruction, task_timezone=None)
    inferred = str(weather_contract.get("display_timezone") or "").strip()
    if inferred and inferred != "UTC" and is_valid_timezone_name(inferred):
        return inferred
    return effective


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
        "start": _format_local_event_dt(start_dt, fallback=start_raw),
        "end": _format_local_event_dt(_coerce_dt(end_raw, now_local.tzinfo or timezone.utc), fallback=end_raw),
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
        f"Briefing mode: {dataset.get('briefing_mode')}",
        f"Ingredients: {', '.join(str(item) for item in (dataset.get('ingredients') or [])) or '-'}",
        f"Required sections: {', '.join(str(item) for item in (dataset.get('required_sections') or [])) or '-'}",
        f"Headline limit: {dataset.get('headline_limit')}",
        f"Local date: {dataset.get('local_date')}",
        f"Timezone: {dataset.get('timezone')}",
        "",
        "KO market snapshot:",
    ]
    market_snapshot = dataset.get("market_snapshot")
    if isinstance(market_snapshot, dict) and market_snapshot:
        lines.append(
            "- "
            f"symbol={market_snapshot.get('symbol')} date={market_snapshot.get('date')} "
            f"open={market_snapshot.get('open')} high={market_snapshot.get('high')} "
            f"low={market_snapshot.get('low')} close={market_snapshot.get('close')} "
            f"provider={market_snapshot.get('provider')}"
        )
    else:
        lines.append("- none")
    lines.extend([
        "",
        "Weather snapshot:",
    ])
    weather_snapshot = dataset.get("weather_snapshot")
    if isinstance(weather_snapshot, dict) and weather_snapshot:
        location = weather_snapshot.get("location") if isinstance(weather_snapshot.get("location"), dict) else {}
        current_weather = weather_snapshot.get("current_weather") if isinstance(weather_snapshot.get("current_weather"), dict) else {}
        observed_local = _format_weather_observed_local(
            current_weather.get("observed_at_utc"),
            timezone_name=str(dataset.get("timezone") or "UTC"),
        )
        lines.append(
            "- "
            f"city={location.get('city_name') or '-'} country={location.get('country') or '-'} "
            f"temp_f={_c_to_f(current_weather.get('temperature_c'))} feels_like_f={_c_to_f(current_weather.get('feels_like_c'))} "
            f"description={current_weather.get('description') or current_weather.get('weather_main') or '-'} "
            f"observed_local={observed_local}"
        )
    else:
        lines.append("- none")
    lines.extend([
        "",
        "Calendar events:",
    ])
    events = list(dataset.get("calendar_events") or [])
    if not events:
        lines.append("- none")
    else:
        for event in events:
            lines.append(
                f"- {event.get('start')} title={event.get('title')} location={event.get('location') or '-'} starts_within_2h={event.get('starts_within_2h')}"
            )
    lines.append("")
    lines.append("Tomorrow events:")
    tomorrow_events = list(dataset.get("tomorrow_events") or [])
    if not tomorrow_events:
        lines.append("- none")
    else:
        for event in tomorrow_events:
            lines.append(
                f"- {event.get('start')} title={event.get('title')} location={event.get('location') or '-'}"
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


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _default_ingredients(briefing_mode: str) -> list[str]:
    if briefing_mode == "evening":
        return ["calendar", "rss_news", "market_snapshot", "weather", "history", "tomorrow_prep"]
    return ["calendar", "rss_news", "market_snapshot", "weather", "history", "tomorrow_prep"]


def _effective_ingredients(
    *,
    briefing_mode: str,
    provided: list[str],
    has_market_snapshot: bool,
    has_weather_snapshot: bool,
) -> list[str]:
    baseline = ["calendar", "rss_news", "history", "tomorrow_prep"]
    if has_market_snapshot:
        baseline.append("market_snapshot")
    if has_weather_snapshot:
        baseline.append("weather")
    merged: list[str] = []
    for item in [*baseline, *provided]:
        normalized = str(item or "").strip()
        if not normalized or normalized in merged:
            continue
        merged.append(normalized)
    return merged


def _default_required_sections(briefing_mode: str) -> list[str]:
    if briefing_mode == "evening":
        return ["day_in_review", "market_snapshot", "weather", "history", "headlines", "worth_attention", "tomorrow_at_a_glance"]
    return ["today_at_a_glance", "market_snapshot", "weather", "history", "headlines", "worth_attention", "tomorrow_at_a_glance"]


def _effective_required_sections(raw_sections: Any, *, briefing_mode: str) -> list[str]:
    baseline = _default_required_sections(briefing_mode)
    provided = _normalize_string_list(raw_sections)
    canonical_provided = [_canonical_section_name(name) for name in provided]
    merged: list[str] = []
    for name in [*baseline, *canonical_provided]:
        if not name or name in merged:
            continue
        if name not in _SECTION_ALIASES:
            continue
        merged.append(name)
    return merged


def _canonical_section_name(name: str) -> str:
    normalized = str(name or "").strip().lower()
    return _LEGACY_SECTION_NAME_MAP.get(normalized, normalized)


async def _load_market_snapshot(*, db, user_id: int) -> tuple[dict[str, Any], str]:
    try:
        payload = await fetch_daily_market_data_payload(
            db,
            user_id=user_id,
            provider="alphavantage",
            symbol="KO",
            days=1,
            secret_name="alphavantage_api_key",
        )
    except Exception as exc:
        return {}, str(exc)

    days = list(payload.get("days") or [])
    if not days:
        return {}, ""
    latest = days[0]
    return {
        "symbol": payload.get("symbol") or "KO",
        "provider": payload.get("provider") or "alphavantage",
        "date": latest.get("date"),
        "open": latest.get("open"),
        "high": latest.get("high"),
        "low": latest.get("low"),
        "close": latest.get("close"),
        "volume": latest.get("volume"),
    }, ""


async def _load_weather_snapshot(
    *,
    db,
    user_id: int,
    task_id: int,
    instruction: str,
    task_timezone: str | None,
) -> tuple[dict[str, Any], str]:
    contract = _build_weather_contract(instruction, task_timezone=task_timezone)
    try:
        raw = await execute_api_request(
            db,
            user_id=user_id,
            service=str(contract.get("service") or "weather"),
            endpoint=str(contract.get("endpoint") or "current_conditions"),
            query_params=dict(contract.get("query_params") or {}),
            secret_name=str(contract.get("secret_name") or "openweathermap_api_key"),
            response_fields=dict(contract.get("response_fields") or {}),
            task_id=task_id,
        )
    except Exception as exc:
        return {}, str(exc)

    parsed = _parse_structured_api_result(raw)
    fields = parsed.get("fields") if isinstance(parsed, dict) else {}
    if not isinstance(fields, dict):
        return {}, ""
    location = fields.get("location") if isinstance(fields.get("location"), dict) else {}
    current_weather = fields.get("current_weather") if isinstance(fields.get("current_weather"), dict) else {}
    if not location and not current_weather:
        return {}, ""
    return {
        "location": location,
        "current_weather": current_weather,
    }, ""


def _parse_structured_api_result(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _c_to_f(value: Any) -> str:
    try:
        celsius = float(value)
    except Exception:
        return "-"
    fahrenheit = (celsius * 9.0 / 5.0) + 32.0
    return f"{fahrenheit:.1f}"


def _format_weather_observed_local(value: Any, *, timezone_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        local_dt = dt.astimezone(ZoneInfo(timezone_name or "UTC"))
    except Exception:
        local_dt = dt.astimezone(timezone.utc)
    return local_dt.strftime("%Y-%m-%d %I:%M %p %Z")


def _format_local_event_dt(value: Optional[datetime], *, fallback: str) -> str:
    if value is None:
        return fallback
    return value.strftime("%Y-%m-%d %I:%M %p %Z")


def _resolve_headline_limit(*, task: Task, params: Dict[str, Any], briefing_mode: str) -> int:
    explicit = params.get("headline_limit")
    try:
        if explicit is not None:
            return max(1, min(int(explicit), 10))
    except Exception:
        pass
    lowered = f"{getattr(task, 'title', '')}\n{getattr(task, 'instruction', '')}".lower()
    word_to_int = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
    digit_match = re.search(r"\b(\d+)\s+(?:top\s+)?headlines?\b", lowered)
    if digit_match:
        return max(1, min(int(digit_match.group(1)), 10))
    for word, value in word_to_int.items():
        if re.search(rf"\b{word}\s+(?:top\s+)?headlines?\b", lowered):
            return value
    return _DEFAULT_HEADLINE_LIMIT if briefing_mode == "morning" else 6


def _display_section_name(name: str) -> str:
    aliases = _SECTION_ALIASES.get(name, ())
    return aliases[0] if aliases else name


def _has_section(text: str, name: str) -> bool:
    return any(alias in text for alias in _SECTION_ALIASES.get(name, ()))


def _count_headline_bullets(text: str) -> int:
    body = _section_body(text, "headlines")
    if not body:
        return 0
    return sum(1 for line in body.splitlines() if line.strip().startswith("- "))


def _headline_bullets_have_summaries(text: str) -> bool:
    body = _section_body(text, "headlines")
    if not body:
        return False
    bullets = [line.strip() for line in body.splitlines() if line.strip().startswith("- ")]
    if not bullets:
        return False
    for bullet in bullets:
        if bullet.count("—") < 2:
            return False
    return True


def _section_body(text: str, name: str) -> str:
    for alias in _SECTION_ALIASES.get(name, ()):
        pattern = rf"{re.escape(alias)}\s*\n(.*?)(?=\n##\s|\Z)"
        match = re.search(pattern, text, flags=re.DOTALL)
        if match:
            return match.group(1).strip()
    return ""
