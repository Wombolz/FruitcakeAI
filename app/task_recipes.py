from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from app.autonomy.configured_executor import infer_configured_executor
from app.time_utils import is_valid_timezone_name

_RECIPE_FAMILIES = {
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
    if family == "daily_research_briefing":
        return _build_daily_research_briefing_recipe(
            title=title,
            instruction=instruction,
            task_type=task_type,
            requested_profile=requested_profile,
            params=params,
        )
    if family == "morning_briefing":
        return _build_morning_briefing_recipe(task_type=task_type)
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

    if family == "daily_research_briefing":
        topic = str(params.get("topic") or title).strip()
        path = str(params.get("path") or "").strip()
        window_hours = params.get("window_hours")
        summary = f"Created a {cadence} research briefing task, '{title}', for {topic}."
        parts = []
        if window_hours:
            parts.append(f"it will analyze the past {window_hours} hours")
        else:
            parts.append("it will analyze recent coverage")
        if path:
            parts.append(f"append the briefing to {path}")
        detail = " It will " + " and ".join(parts) + "."
        return summary + detail + _schedule_suffix(schedule)

    if family == "morning_briefing":
        return (
            f"Created a {cadence} morning briefing task, '{title}'."
            " It will prepare a daily agenda and headline summary."
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
    explicit_family = (recipe_family or "").strip().lower()
    if explicit_family in _RECIPE_FAMILIES:
        return explicit_family

    explicit_profile = (requested_profile or "").strip().lower()
    if explicit_profile in _RECIPE_FAMILIES:
        return explicit_profile

    preferred = (preferred_family or "").strip().lower()
    if preferred in _RECIPE_FAMILIES:
        return preferred

    lowered = f"{title}\n{instruction}".lower()
    if task_type == "recurring" and any(marker in lowered for marker in ("watch", "monitor", "track", "follow")) and any(
        marker in lowered for marker in ("news", "rss", "feed", "headline", "topic", "developments")
    ):
        return "topic_watcher"
    if "morning briefing" in lowered or (
        "calendar" in lowered and any(marker in lowered for marker in ("briefing", "headlines", "agenda"))
    ):
        return "morning_briefing"
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
        return "daily_research_briefing"
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
    sources = _extract_sources(instruction)
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
        title=_string_param(params, "title") or f"{topic} Watcher",
        instruction="\n".join(lines).strip(),
        task_type=task_type,
        profile="topic_watcher",
        params={"topic": topic, "threshold": threshold, "sources": sources},
        assumptions=assumptions,
    )


def _build_daily_research_briefing_recipe(
    *,
    title: str,
    instruction: str,
    task_type: str,
    requested_profile: Optional[str],
    params: dict[str, Any],
) -> NormalizedTaskRecipe | None:
    inferred = infer_configured_executor(
        title=title,
        instruction=instruction,
        task_type=task_type,
        requested_profile=requested_profile,
    )
    config = inferred.executor_config or {}
    input_config = config.get("input") or {}
    persistence = config.get("persistence") or {}
    topic = str(input_config.get("topic") or "").strip()
    path = str(persistence.get("path") or "").strip()
    if not topic or not path:
        return None
    window_hours = int(input_config.get("window_hours") or 24)
    assumptions: list[str] = []
    if "past" not in instruction.lower():
        assumptions.append("defaulted research window to 24 hours")
    normalized_instruction = (
        f"Analyze the news about {topic} from the past {window_hours} hours using cached RSS feeds and "
        f"append a daily research briefing to {path}."
    )
    return NormalizedTaskRecipe(
        family="daily_research_briefing",
        confidence="high",
        title=_string_param(params, "title") or f"Daily {topic} Briefing",
        instruction=normalized_instruction,
        task_type=task_type,
        profile=None,
        params={"topic": topic, "window_hours": window_hours, "path": path},
        assumptions=assumptions,
    )


def _build_morning_briefing_recipe(*, task_type: str) -> NormalizedTaskRecipe:
    return NormalizedTaskRecipe(
        family="morning_briefing",
        confidence="high",
        title="Morning Briefing",
        instruction=(
            "Prepare a morning briefing for today using my calendar and current headlines.\n"
            "Include today's schedule, notable headlines, and any important conflicts or priorities."
        ),
        task_type=task_type,
        profile="morning_briefing",
        params={},
        assumptions=[],
    )


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
    match = re.search(r"sources\s*:\s*(.+?)(?:\n|$)", instruction, flags=re.IGNORECASE)
    if not match:
        return []
    return [item.strip().lower() for item in str(match.group(1)).split(",") if item.strip()]


def _extract_timezone(instruction: str) -> str | None:
    match = re.search(r"timezone\s*=\s*([A-Za-z_/\-]+)", instruction, flags=re.IGNORECASE)
    if not match:
        return None
    candidate = str(match.group(1)).strip()
    return candidate if is_valid_timezone_name(candidate) else None


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


def _clean_topic(value: str) -> str:
    candidate = re.sub(r"\s+", " ", str(value or "").strip(" .,:;\"'`"))
    candidate = re.sub(r"\b(my|the|current|latest)\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(news|updates|headlines|developments)\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(for|with)\s+major\s*$", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(for|with)\s+(meaningful|important|significant)\s*$", "", candidate, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", candidate).strip(" .,:;\"'`")
