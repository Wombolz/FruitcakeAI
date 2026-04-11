from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from app.autonomy.configured_executor import infer_configured_executor
from app.time_utils import is_valid_timezone_name

_RECIPE_FAMILIES = {
    "agent",
    "briefing",
    "topic_watcher",
    "daily_research_briefing",
    "morning_briefing",
    "iss_pass_watcher",
    "weather_conditions",
    "maintenance",
}


@dataclass(frozen=True)
class NormalizedTaskRecipe:
    family: str
    confidence: str
    title: str
    instruction: str
    task_type: str
    profile: str | None
    params: dict[str, Any]
    assumptions: list[str]


def normalize_task_recipe(
    *,
    title: str,
    instruction: str,
    task_type: str,
    requested_profile: Optional[str],
    recipe_family: Optional[str] = None,
    recipe_params: Optional[dict[str, Any]] = None,
    preferred_family: Optional[str] = None,
) -> NormalizedTaskRecipe | None:
    family = _resolve_recipe_family(
        title=title,
        instruction=instruction,
        task_type=task_type,
        requested_profile=requested_profile,
        recipe_family=recipe_family,
        preferred_family=preferred_family,
    )
    if not family:
        return None
    params = dict(recipe_params or {})
    if family == "topic_watcher":
        return _build_topic_watcher_recipe(title=title, instruction=instruction, task_type=task_type, params=params)
    if family == "agent":
        return _build_agent_recipe(title=title, instruction=instruction, task_type=task_type, params=params)
    if family in {"briefing", "daily_research_briefing", "morning_briefing"}:
        return _build_briefing_recipe(
            title=title,
            instruction=instruction,
            task_type=task_type,
            requested_profile=requested_profile,
            params=params,
            requested_family=family,
        )
    if family == "iss_pass_watcher":
        return _build_iss_recipe(title=title, instruction=instruction, task_type=task_type, params=params)
    if family == "weather_conditions":
        return _build_weather_recipe(title=title, instruction=instruction, task_type=task_type, params=params)
    if family == "maintenance":
        return _build_maintenance_recipe(task_type=task_type, params=params)
    return None


def build_task_recipe_metadata(
    recipe: NormalizedTaskRecipe | None,
    *,
    selected_profile: str | None,
    executor_config: dict[str, Any] | None,
) -> dict[str, Any]:
    if recipe is None:
        return {}
    return {
        "version": 1,
        "source": "chat_task_recipe_normalization",
        "family": recipe.family,
        "confidence": recipe.confidence,
        "params": recipe.params,
        "assumptions": recipe.assumptions,
        "selected_profile": selected_profile,
        "selected_executor_kind": str((executor_config or {}).get("kind") or "") or None,
        "instruction_style": "recipe_v1",
    }


def build_task_recipe_summary(
    *,
    title: str,
    task_type: str,
    schedule: str | None,
    task_recipe: dict[str, Any] | None,
    profile: str | None,
) -> str:
    parts = [f"Task '{title}'", f"type={task_type}"]
    family = str((task_recipe or {}).get("family") or "").strip()
    if family:
        parts.append(f"family={family}")
    elif profile:
        parts.append(f"profile={profile}")
    if schedule:
        parts.append(f"schedule={schedule}")
    return " | ".join(parts)


def build_task_confirmation_text(
    *,
    title: str,
    task_type: str,
    schedule: str | None,
    task_recipe: dict[str, Any] | None,
    profile: str | None,
) -> str:
    recipe = task_recipe or {}
    family = str(recipe.get("family") or "").strip().lower()
    params = recipe.get("params") if isinstance(recipe.get("params"), dict) else {}
    cadence = "recurring" if task_type == "recurring" else "saved"

    if family == "topic_watcher":
        topic = str(params.get("topic") or title).strip()
        threshold = str(params.get("threshold") or "medium").strip()
        summary = f"Created a {cadence} topic watcher, '{title}', for {topic}."
        detail = f" It will watch your feeds for meaningful changes at {threshold} sensitivity."
        return summary + detail + _schedule_suffix(schedule)

    if family == "briefing":
        mode = str(params.get("briefing_mode") or "morning").strip().lower() or "morning"
        topic = str(params.get("topic") or title).strip()
        path = str(params.get("path") or "").strip()
        window_hours = params.get("window_hours")
        if path and topic:
            summary = f"Created a {cadence} {mode} briefing task, '{title}', for {topic}."
            parts = []
            if window_hours:
                parts.append(f"analyze the past {window_hours} hours")
            else:
                parts.append("analyze recent coverage")
            parts.append(f"append the briefing to {path}")
            return summary + " It will " + " and ".join(parts) + "." + _schedule_suffix(schedule)
        framing = "start-of-day" if mode == "morning" else "end-of-day"
        return (
            f"Created a {cadence} {mode} briefing task, '{title}'."
            f" It will prepare a {framing} agenda and headline summary."
            + _schedule_suffix(schedule)
        )

    if family == "agent":
        agent_role = str(params.get("agent_role") or "general_agent").strip()
        return (
            f"Created a {cadence} agent-style task, '{title}', for role '{agent_role}'."
            " It will keep the task objective as freeform delegated work rather than a built-in profile recipe."
            + _schedule_suffix(schedule)
        )

    if family == "iss_pass_watcher":
        lat = params.get("lat")
        lon = params.get("lon")
        timezone_name = str(params.get("timezone") or "UTC").strip()
        location = f"lat={lat}, lon={lon}" if lat is not None and lon is not None else "the configured location"
        return (
            f"Created a {cadence} ISS pass watcher, '{title}', for {location}."
            f" It will report results in {timezone_name} time."
            + _schedule_suffix(schedule)
        )

    if family == "weather_conditions":
        lat = params.get("lat")
        lon = params.get("lon")
        timezone_name = str(params.get("timezone") or "UTC").strip()
        location = f"lat={lat}, lon={lon}" if lat is not None and lon is not None else "the configured location"
        return (
            f"Created a {cadence} weather task, '{title}', for {location}."
            f" It will report current conditions in {timezone_name} time."
            + _schedule_suffix(schedule)
        )

    if family == "maintenance":
        return (
            f"Created a {cadence} maintenance task, '{title}'."
            " It will run the configured upkeep action."
            + _schedule_suffix(schedule)
        )

    summary = build_task_recipe_summary(
        title=title,
        task_type=task_type,
        schedule=schedule,
        task_recipe=task_recipe,
        profile=profile,
    )
    return f"Created task summary: {summary}."


def build_task_draft_confirmation_text(
    *,
    title: str,
    task_type: str,
    schedule: str | None,
    task_recipe: dict[str, Any] | None,
    profile: str | None,
) -> str:
    created_text = build_task_confirmation_text(
        title=title,
        task_type=task_type,
        schedule=schedule,
        task_recipe=task_recipe,
        profile=profile,
    )
    if created_text.startswith("Created "):
        return "Draft ready: " + created_text[len("Created ") :]
    if created_text.startswith("Created task summary:"):
        return created_text.replace("Created task summary:", "Draft summary:", 1)
    return created_text


def _schedule_suffix(schedule: str | None) -> str:
    return f" Schedule: {schedule}." if schedule else ""


def _resolve_recipe_family(
    *,
    title: str,
    instruction: str,
    task_type: str,
    requested_profile: Optional[str],
    recipe_family: Optional[str],
    preferred_family: Optional[str],
) -> str | None:
    explicit_family = _canonical_recipe_family(recipe_family)
    if recipe_family is not None and explicit_family == "":
        return None
    if explicit_family in _RECIPE_FAMILIES:
        return explicit_family

    explicit_profile = _canonical_recipe_family(requested_profile)
    if explicit_profile in _RECIPE_FAMILIES:
        return explicit_profile

    preferred = _canonical_recipe_family(preferred_family)
    if preferred in _RECIPE_FAMILIES:
        return preferred

    lowered = f"{title}\n{instruction}".lower()
    if "morning briefing" in lowered or (
        "calendar" in lowered and any(marker in lowered for marker in ("briefing", "headlines", "agenda"))
    ):
        return "briefing"
    if "evening briefing" in lowered or ("tonight" in lowered and "briefing" in lowered):
        return "briefing"
    if "iss" in lowered and any(marker in lowered for marker in ("pass", "passes", "visual")):
        return "iss_pass_watcher"
    if any(marker in lowered for marker in ("weather", "current conditions")) and (
        re.search(r"lat(?:itude)?\s*=", lowered) or re.search(r"lon(?:gitude)?\s*=", lowered)
    ):
        return "weather_conditions"
    if "refresh_rss_cache" in lowered or ("refresh" in lowered and "rss" in lowered and "cache" in lowered):
        return "maintenance"
    inferred = infer_configured_executor(
        title=title,
        instruction=instruction,
        task_type=task_type,
        requested_profile=requested_profile,
    )
    if inferred.executor_config:
        return "briefing"
    if any(marker in lowered for marker in ("daily briefing", "daily summary", "daily analysis")) and any(
        marker in lowered for marker in ("cached", "rss", "feed", "feeds", "last 24 hours", "past 24 hours", "previous 24 hours")
    ):
        return "briefing"
    if task_type == "recurring" and any(marker in lowered for marker in ("watch", "monitor", "track", "follow")) and any(
        marker in lowered for marker in ("news", "rss", "feed", "headline", "topic", "developments")
    ):
        return "topic_watcher"
    return None


def _build_topic_watcher_recipe(
    *,
    title: str,
    instruction: str,
    task_type: str,
    params: dict[str, Any],
) -> NormalizedTaskRecipe | None:
    topic = _string_param(params, "topic") or _extract_topic_from_watcher_text(title, instruction)
    if not topic:
        return None
    threshold = _string_param(params, "threshold") or _extract_threshold(instruction) or "medium"
    sources = _string_list_param(params, "sources") or _extract_sources(instruction)
    assumptions: list[str] = []
    if not _string_param(params, "threshold") and "threshold:" not in instruction.lower():
        assumptions.append("defaulted watcher threshold to medium")
    lines = [f"topic: {topic}", f"threshold: {threshold}"]
    if sources:
        lines.append(f"sources: {', '.join(sources)}")
    lines.extend(
        [
            "",
            f"Watch my curated RSS feeds for significant updates about {topic}.",
            "Return NOTHING_NEW when there is no meaningful change.",
        ]
    )
    return NormalizedTaskRecipe(
        family="topic_watcher",
        confidence="high",
        title=_string_param(params, "title") or _build_topic_watcher_title(topic),
        instruction="\n".join(lines).strip(),
        task_type=task_type,
        profile="topic_watcher",
        params={"topic": topic, "threshold": threshold, "sources": sources},
        assumptions=assumptions,
    )


def _build_briefing_recipe(
    *,
    title: str,
    instruction: str,
    task_type: str,
    requested_profile: Optional[str],
    params: dict[str, Any],
    requested_family: str,
) -> NormalizedTaskRecipe | None:
    briefing_mode = _resolve_briefing_mode(
        title=title,
        instruction=instruction,
        params=params,
        requested_family=requested_family,
    )
    topic = _string_param(params, "topic")
    path = _string_param(params, "path")
    window_hours = _int_param(params, "window_hours")
    market_symbol = _normalize_market_symbol(_string_param(params, "market_symbol")) or "KO"
    custom_guidance = _extract_briefing_custom_guidance(
        instruction=instruction,
        params=params,
        briefing_mode=briefing_mode,
    )

    ingredients = _string_list_param(params, "ingredients") or _default_briefing_ingredients(
        briefing_mode=briefing_mode,
        topic=topic,
        path=path,
    )
    sections = _string_list_param(params, "sections") or _default_briefing_sections(briefing_mode=briefing_mode, topic=topic, path=path)

    if not topic or not path:
        inferred = infer_configured_executor(
            title=title,
            instruction=instruction,
            task_type=task_type,
            requested_profile=requested_profile,
        )
        config = inferred.executor_config or {}
        input_config = config.get("input") or {}
        persistence = config.get("persistence") or {}
        topic = topic or str(input_config.get("topic") or "").strip()
        path = path or str(persistence.get("path") or "").strip()
        window_hours = window_hours or int(input_config.get("window_hours") or 24)
        briefing_mode = str(input_config.get("briefing_mode") or briefing_mode or "morning").strip().lower() or "morning"

    assumptions: list[str] = []
    normalized_params: dict[str, Any] = {
        "briefing_mode": briefing_mode,
        "ingredients": ingredients,
        "sections": sections,
        "market_symbol": market_symbol,
    }

    if topic and path:
        window_hours = window_hours or 24
        if "past" not in instruction.lower() and _int_param(params, "window_hours") is None:
            assumptions.append("defaulted briefing window to 24 hours")
        normalized_instruction = (
            f"Analyze the news about {topic} from the past {window_hours} hours using cached RSS feeds and "
            f"append a {briefing_mode} briefing to {path}."
        )
        if custom_guidance:
            normalized_instruction = f"{normalized_instruction}\nAdditional guidance: {custom_guidance}"
        normalized_params.update(
            {
                "topic": topic,
                "window_hours": window_hours,
                "path": path,
            }
        )
        if custom_guidance:
            normalized_params["custom_guidance"] = custom_guidance
        return NormalizedTaskRecipe(
            family="briefing",
            confidence="high",
            title=_string_param(params, "title") or _default_briefing_title(briefing_mode=briefing_mode, topic=topic),
            instruction=normalized_instruction,
            task_type=task_type,
            profile=None,
            params=normalized_params,
            assumptions=assumptions,
        )

    base_instruction = _briefing_profile_instruction(briefing_mode=briefing_mode)
    if custom_guidance:
        normalized_instruction = f"{base_instruction}\nAdditional guidance: {custom_guidance}"
    else:
        normalized_instruction = base_instruction
    if custom_guidance:
        normalized_params["custom_guidance"] = custom_guidance
    return NormalizedTaskRecipe(
        family="briefing",
        confidence="high",
        title=_string_param(params, "title") or title or _default_briefing_title(briefing_mode=briefing_mode, topic=None),
        instruction=normalized_instruction,
        task_type=task_type,
        profile="briefing",
        params=normalized_params,
        assumptions=assumptions,
    )


def _build_agent_recipe(
    *,
    title: str,
    instruction: str,
    task_type: str,
    params: dict[str, Any],
) -> NormalizedTaskRecipe | None:
    normalized_instruction = str(instruction or "").strip()
    if not normalized_instruction:
        return None
    agent_role = _string_param(params, "agent_role") or "general_agent"
    source_context_hint = _string_param(params, "source_context_hint")
    normalized_params: dict[str, Any] = {
        key: _json_safe_param_value(value)
        for key, value in params.items()
        if _json_safe_param_value(value) is not None
    }
    normalized_params["agent_role"] = agent_role
    context_paths = _string_list_param(params, "context_paths")
    if source_context_hint:
        normalized_params["source_context_hint"] = source_context_hint
    if context_paths:
        normalized_params["context_paths"] = context_paths
    return NormalizedTaskRecipe(
        family="agent",
        confidence="high",
        title=_string_param(params, "title") or title or "Agent Task",
        instruction=normalized_instruction,
        task_type=task_type,
        profile=None,
        params=normalized_params,
        assumptions=[],
    )


def _json_safe_param_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        out = []
        for item in value:
            normalized = _json_safe_param_value(item)
            if normalized is not None:
                out.append(normalized)
        return out
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            normalized = _json_safe_param_value(item)
            if normalized is not None:
                out[str(key)] = normalized
        return out
    return str(value)


def _build_iss_recipe(
    *,
    title: str,
    instruction: str,
    task_type: str,
    params: dict[str, Any],
) -> NormalizedTaskRecipe:
    lat = _float_param(params, "lat") or _extract_float(instruction, r"lat(?:itude)?\s*=\s*(-?\d+(?:\.\d+)?)") or 32.4485
    lon = _float_param(params, "lon") or _extract_float(instruction, r"lon(?:gitude)?\s*=\s*(-?\d+(?:\.\d+)?)") or -81.7832
    days = _int_param(params, "days") or _extract_int(instruction, r"days\s*=\s*(\d+)") or 1
    min_visibility = _int_param(params, "min_visibility_seconds") or _extract_int(instruction, r"minVisibility\s*=\s*(\d+)") or 120
    max_elevation = _int_param(params, "min_max_elevation_deg") or _extract_int(instruction, r"max elevation\s*>=\s*(\d+)") or 30
    timezone_name = _timezone_param(params, "timezone") or _extract_timezone(instruction) or "UTC"
    assumptions: list[str] = []
    if "timezone=" not in instruction.lower() and not _timezone_param(params, "timezone"):
        assumptions.append(f"defaulted ISS display timezone to {timezone_name}")
    normalized_instruction = (
        f"Check ISS visible passes for lat={lat:.4f} lon={lon:.4f} days={days} "
        f"minVisibility={min_visibility} max elevation>={max_elevation} timezone={timezone_name}."
    )
    return NormalizedTaskRecipe(
        family="iss_pass_watcher",
        confidence="high",
        title=_string_param(params, "title") or "ISS Pass Watcher",
        instruction=normalized_instruction,
        task_type=task_type,
        profile="iss_pass_watcher",
        params={
            "lat": lat,
            "lon": lon,
            "days": days,
            "min_visibility_seconds": min_visibility,
            "min_max_elevation_deg": max_elevation,
            "timezone": timezone_name,
        },
        assumptions=assumptions,
    )


def _build_weather_recipe(
    *,
    title: str,
    instruction: str,
    task_type: str,
    params: dict[str, Any],
) -> NormalizedTaskRecipe:
    lat = _float_param(params, "lat") or _extract_float(instruction, r"lat(?:itude)?\s*=\s*(-?\d+(?:\.\d+)?)") or 32.4485
    lon = _float_param(params, "lon") or _extract_float(instruction, r"lon(?:gitude)?\s*=\s*(-?\d+(?:\.\d+)?)") or -81.7832
    timezone_name = _timezone_param(params, "timezone") or _extract_timezone(instruction) or "UTC"
    assumptions: list[str] = []
    if "timezone=" not in instruction.lower() and not _timezone_param(params, "timezone"):
        assumptions.append(f"defaulted weather display timezone to {timezone_name}")
    normalized_instruction = (
        f"Check current conditions at lat={lat:.4f} lon={lon:.4f} timezone={timezone_name}.\n"
        "Return the current weather conditions only."
    )
    return NormalizedTaskRecipe(
        family="weather_conditions",
        confidence="high",
        title=_string_param(params, "title") or "Current Weather Conditions",
        instruction=normalized_instruction,
        task_type=task_type,
        profile="weather_conditions",
        params={"lat": lat, "lon": lon, "timezone": timezone_name},
        assumptions=assumptions,
    )


def _build_maintenance_recipe(*, task_type: str, params: dict[str, Any]) -> NormalizedTaskRecipe:
    max_items = _int_param(params, "max_items_per_source") or 20
    args = {"max_items_per_source": max_items}
    return NormalizedTaskRecipe(
        family="maintenance",
        confidence="high",
        title=_string_param(params, "title") or "Refresh RSS Cache",
        instruction=f'tool: refresh_rss_cache\nargs: {json.dumps(args)}',
        task_type=task_type,
        profile="maintenance",
        params={"tool": "refresh_rss_cache", "args": args},
        assumptions=["defaulted refresh_rss_cache max_items_per_source to 20"] if "max_items_per_source" not in params else [],
    )


def _extract_topic_from_watcher_text(title: str, instruction: str) -> str:
    cleaned_title = _clean_topic(re.sub(r"\b(watch|watcher|monitor|tracking|track)\b", "", title, flags=re.IGNORECASE))
    if cleaned_title and len(cleaned_title.split()) <= 5:
        return cleaned_title
    text = re.sub(r"\s+", " ", instruction).strip()
    for pattern in (
        r"(?:watch|monitor|track|follow)\s+(?:news|updates|headlines|developments)?\s*(?:about|for|on)?\s+(.+?)(?:\s+every|\s+from|\s+for\s+major|\s*$)",
        r"topic\s*:\s*(.+?)(?:\n|$)",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = _clean_topic(match.group(1))
            if candidate:
                return candidate
    return cleaned_title


def _extract_threshold(instruction: str) -> str | None:
    match = re.search(r"threshold\s*:\s*([A-Za-z_]+)", instruction, flags=re.IGNORECASE)
    if not match:
        return None
    candidate = str(match.group(1)).strip().lower()
    return candidate if candidate in {"low", "medium", "high"} else None


def _extract_sources(instruction: str) -> list[str]:
    block_match = re.search(
        r"sources(?:\s*\([^)]+\))?(?:\s*:)?\s*(.*?)(?:\n\s*\n|\n[A-Z][A-Za-z /()]+(?:\s*:)?\s*|$)",
        instruction,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if block_match:
        raw = str(block_match.group(1)).strip()
    else:
        match = re.search(r"sources(?:\s*\([^)]+\))?\s*:\s*(.+?)(?:\n|$)", instruction, flags=re.IGNORECASE)
        if not match:
            return []
        raw = str(match.group(1)).strip()
    raw = re.split(r"\b(optional add-ons|major update criteria|behavior)\s*:", raw, flags=re.IGNORECASE)[0].strip()
    values: list[str] = []
    bullet_lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullet_lines.append(stripped[2:].strip())
    items = bullet_lines if bullet_lines else raw.split(",")
    for item in items:
        candidate = re.sub(r"\s+", " ", item).strip(" .,:;\"'`").lower()
        candidate = re.sub(r":\s*https?://\S+$", "", candidate)
        if not candidate:
            continue
        if len(candidate.split()) > 8:
            continue
        if candidate not in values:
            values.append(candidate)
    return values


def _build_topic_watcher_title(topic: str) -> str:
    return f"{topic} Watcher"


def _canonical_recipe_family(value: Optional[str]) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in {"morning_briefing", "daily_research_briefing"}:
        return "briefing"
    return candidate


def _resolve_briefing_mode(
    *,
    title: str,
    instruction: str,
    params: dict[str, Any],
    requested_family: str,
) -> str:
    explicit = _string_param(params, "briefing_mode")
    if explicit:
        value = explicit.strip().lower()
        if value in {"morning", "evening"}:
            return value
    if requested_family == "morning_briefing":
        return "morning"
    lowered = f"{title}\n{instruction}".lower()
    if "evening" in lowered or "tonight" in lowered:
        return "evening"
    return "morning"


def _default_briefing_ingredients(*, briefing_mode: str, topic: str | None, path: str | None) -> list[str]:
    if topic and path:
        return ["rss_news"]
    if briefing_mode == "evening":
        return ["calendar", "rss_news", "market_snapshot", "weather", "history", "tomorrow_prep"]
    return ["calendar", "rss_news", "market_snapshot", "weather", "history", "tomorrow_prep"]


def _default_briefing_sections(*, briefing_mode: str, topic: str | None, path: str | None) -> list[str]:
    if topic and path:
        if briefing_mode == "evening":
            return ["headline_bullets", "day_in_review", "watch_tomorrow", "links"]
        return ["headline_bullets", "today_context", "watch_today", "links"]
    if briefing_mode == "evening":
        return ["day_in_review", "market_snapshot", "weather", "history", "headlines", "worth_attention", "tomorrow_at_a_glance"]
    return ["today_at_a_glance", "market_snapshot", "weather", "history", "headlines", "worth_attention", "tomorrow_at_a_glance"]


def _default_briefing_title(*, briefing_mode: str, topic: str | None) -> str:
    if topic:
        label = "Evening" if briefing_mode == "evening" else "Daily"
        return f"{label} {topic} Briefing"
    return "Evening Briefing" if briefing_mode == "evening" else "Morning Briefing"


def _briefing_profile_instruction(*, briefing_mode: str) -> str:
    if briefing_mode == "evening":
        return (
            "Prepare an evening briefing using today's calendar context and current headlines.\n"
            "Include a short day-in-review, a compact market snapshot for the configured symbol, a weather note, a today-in-history note, current headlines with one-line summaries, and tomorrow's schedule preview.\n"
            "If today's calendar is empty, still include `## Day in review` with a clear empty-state stub. If tomorrow's calendar is empty, still include `## Tomorrow at a glance` with a clear empty-state stub."
        )
    return (
        "Prepare a morning briefing for today using my calendar and current headlines.\n"
        "Include today's schedule, a compact market snapshot for the configured symbol, a weather note, a today-in-history note, five top headlines with one-line summaries, and tomorrow's schedule preview."
    )


def _normalize_market_symbol(value: str | None) -> str | None:
    candidate = str(value or "").strip().upper()
    if not candidate:
        return None
    candidate = re.sub(r"[^A-Z0-9._-]", "", candidate)
    return candidate or None


def _extract_timezone(instruction: str) -> str | None:
    match = re.search(r"timezone\s*=\s*([A-Za-z_/\-]+)", instruction, flags=re.IGNORECASE)
    if not match:
        return None
    candidate = str(match.group(1)).strip()
    return candidate if is_valid_timezone_name(candidate) else None


def _extract_briefing_custom_guidance(
    *,
    instruction: str,
    params: dict[str, Any],
    briefing_mode: str,
) -> str:
    explicit = _string_param(params, "custom_guidance")
    if explicit:
        return explicit

    cleaned = (instruction or "").strip()
    if not cleaned:
        return ""

    base_instruction = _briefing_profile_instruction(briefing_mode=briefing_mode)
    cleaned_lower = cleaned.lower()
    base_lower = base_instruction.lower()
    if cleaned_lower == base_lower:
        return ""

    remainder = cleaned
    if cleaned_lower.startswith(base_lower):
        remainder = cleaned[len(base_instruction):].strip()
    elif briefing_mode == "morning" and cleaned_lower.startswith("prepare a morning briefing"):
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if len(lines) > 1:
            remainder = "\n".join(lines[1:]).strip()
    elif briefing_mode == "evening" and cleaned_lower.startswith("prepare an evening briefing"):
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if len(lines) > 1:
            remainder = "\n".join(lines[1:]).strip()
    remainder = remainder.lstrip(":- \n")
    match = re.search(r"additional guidance:\s*(.+)$", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return str(match.group(1)).strip()
    return remainder.strip()


def _extract_float(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _extract_int(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _string_param(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    candidate = str(value).strip()
    return candidate or None


def _int_param(params: dict[str, Any], key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _float_param(params: dict[str, Any], key: str) -> float | None:
    value = params.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _timezone_param(params: dict[str, Any], key: str) -> str | None:
    candidate = _string_param(params, key)
    if not candidate or not is_valid_timezone_name(candidate):
        return None
    return candidate


def _string_list_param(params: dict[str, Any], key: str) -> list[str]:
    value = params.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    values: list[str] = []
    for item in items:
        candidate = str(item or "").strip()
        if candidate and candidate not in values:
            values.append(candidate)
    return values


def _clean_topic(value: str) -> str:
    candidate = re.sub(r"\s+", " ", str(value or "").strip(" .,:;\"'`"))
    candidate = re.sub(r"\bmajor updates?\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bsignificant updates?\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bimportant updates?\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bmeaningful changes?\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bupdates?\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(my|the|current|latest)\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(news|updates|headlines|developments)\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(for|with)\s+major\s*$", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(for|with)\s+(meaningful|important|significant)\s*$", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(major|significant|important|meaningful)\s*$", "", candidate, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", candidate).strip(" .,:;\"'`")
