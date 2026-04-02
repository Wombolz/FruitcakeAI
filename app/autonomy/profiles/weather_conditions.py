from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.spec_loader import load_profile_spec_text


class WeatherConditionsExecutionProfile(TaskExecutionProfile):
    name = "weather_conditions"

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
                "title": "Check Current Weather",
                "instruction": "Use the prepared weather API contract and the api_request tool only. Summarize the current conditions.",
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
        del user_id, task_run_id
        from app.db.models import Task

        task = await db.get(Task, task_id)
        instruction = str(getattr(task, "instruction", "") or "")
        contract = _build_contract(instruction)
        return {
            "api_contract": contract,
            "dataset_stats": {
                "service": contract["service"],
                "endpoint": contract["endpoint"],
                "query_params_present": sorted(contract["query_params"].keys()),
                "response_fields_present": sorted(contract["response_fields"].keys()),
            },
        }

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        del run_context
        return {
            "web_search",
            "fetch_page",
            "search_places",
            "create_task",
            "update_task",
            "create_task_plan",
            "create_and_run_task_plan",
            "create_memory",
            "search_memory_graph",
        }

    def effective_allowed_tools(self, *, run_context: Dict[str, Any]) -> Optional[set[str]]:
        del run_context
        return {"api_request"}

    def allow_skill_injection(self, *, run_context: Dict[str, Any]) -> bool:
        del run_context
        return False

    def augment_prompt(
        self,
        *,
        prompt_parts: list[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> None:
        del is_final_step
        prompt_parts.append(load_profile_spec_text(self.name))
        contract = run_context.get("api_contract") or {}
        if contract:
            prompt_parts.append(
                "Prepared weather API contract:\n"
                f"- service: {contract.get('service')}\n"
                f"- endpoint: {contract.get('endpoint')}\n"
                f"- secret_name: {contract.get('secret_name')}\n"
                f"- query_params: {contract.get('query_params')}\n"
                f"- response_fields: {contract.get('response_fields')}\n"
            )

    def validate_finalize(
        self,
        *,
        result: str,
        prior_full_outputs: List[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        del prior_full_outputs, is_final_step
        tool_records = list(run_context.get("last_tool_records") or [])
        api_result = ""
        for record in tool_records:
            if str(record.get("tool") or "") == "api_request":
                api_result = str(record.get("result_summary") or "").strip()

        cleaned = str(result or "").strip()
        normalized = cleaned.lower()
        fallback_markers = (
            "api request failed",
            "i tried to check",
            "would you like me to",
            "tell me which option",
            "it seems there was an issue",
            "let's try again",
            "or would you prefer i stop",
        )
        structured_result = _parse_structured_api_result(api_result)
        structured_override: str | None = None
        if structured_result:
            fields = structured_result.get("fields", {})
            current_weather = fields.get("current_weather") if isinstance(fields, dict) else None
            if isinstance(current_weather, dict):
                if not current_weather:
                    structured_override = "No current weather conditions found for the requested location."
                elif not cleaned or any(marker in normalized for marker in fallback_markers):
                    structured_override = _format_current_weather(current_weather, fields.get("location"))

        if structured_override is not None:
            cleaned = structured_override
        elif api_result and (not cleaned or any(marker in normalized for marker in fallback_markers)):
            cleaned = api_result
        elif cleaned.lower() == "api request failed." and api_result:
            cleaned = api_result

        report = {
            "fatal": False,
            "api_request_called": bool(api_result),
            "structured_api_result": bool(structured_result),
            "used_tool_result_fallback": cleaned == api_result and bool(api_result),
        }
        return cleaned, report


def _extract_number(pattern: str, text: str, *, cast=float, default: Any = None) -> Any:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return default
    try:
        return cast(match.group(1))
    except Exception:
        return default


def _extract_secret_name(text: str) -> str:
    match = re.search(r"secret\s+([A-Za-z0-9_]+)", text, flags=re.IGNORECASE)
    if not match:
        return "openweathermap_api_key"
    candidate = str(match.group(1)).strip().lower()
    if candidate not in {"openweathermap_api_key", "weather_api_key"}:
        return "openweathermap_api_key"
    return candidate
def _build_contract(instruction: str) -> Dict[str, Any]:
    latitude = _extract_number(r"lat(?:itude)?\s*=\s*(-?\d+(?:\.\d+)?)", instruction, cast=float, default=32.4485)
    longitude = _extract_number(r"lon(?:gitude)?\s*=\s*(-?\d+(?:\.\d+)?)", instruction, cast=float, default=-81.7832)
    return {
        "service": "weather",
        "endpoint": "current_conditions",
        "secret_name": _extract_secret_name(instruction),
        "response_fields": {
            "location": "location",
            "current_weather": "current_weather",
        },
        "query_params": {
            "latitude": latitude,
            "longitude": longitude,
        },
    }


def _parse_structured_api_result(text: str) -> Dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    fields = parsed.get("fields")
    if not isinstance(fields, dict):
        return None
    return parsed


def _format_current_weather(current_weather: Dict[str, Any], location: Dict[str, Any] | None) -> str:
    lines = ["Weather briefing:", ""]
    if isinstance(location, dict):
        city_name = str(location.get("city_name") or "").strip()
        country = str(location.get("country") or "").strip()
        if city_name or country:
            location_label = ", ".join([part for part in [city_name, country] if part])
            lines.append(f"location={location_label}")
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is not None and longitude is not None:
            lines.append(f"coordinates={float(latitude):.4f}, {float(longitude):.4f}")
    time_value = str(current_weather.get("observed_at_utc") or "").strip()
    if time_value:
        try:
            utc_dt = datetime.fromisoformat(time_value.replace("Z", "+00:00"))
            local_dt = utc_dt.astimezone(ZoneInfo("America/New_York"))
            lines.append(f"time_local={local_dt.isoformat()}")
            lines.append(f"time_utc={utc_dt.astimezone(timezone.utc).isoformat()}")
        except Exception:
            lines.append(f"time={time_value}")
    if current_weather.get("temperature_c") is not None:
        lines.append(f"temperature_c={float(current_weather['temperature_c']):.1f}")
    if current_weather.get("feels_like_c") is not None:
        lines.append(f"feels_like_c={float(current_weather['feels_like_c']):.1f}")
    if current_weather.get("humidity_percent") is not None:
        lines.append(f"humidity_percent={int(current_weather['humidity_percent'])}")
    if current_weather.get("pressure_hpa") is not None:
        lines.append(f"pressure_hpa={int(current_weather['pressure_hpa'])}")
    if current_weather.get("wind_speed_mps") is not None:
        lines.append(f"wind_speed_mps={float(current_weather['wind_speed_mps']):.1f}")
    if current_weather.get("wind_direction_deg") is not None:
        lines.append(f"wind_direction_deg={float(current_weather['wind_direction_deg']):.0f}")
    if current_weather.get("weather_code") is not None:
        lines.append(f"weather_code={int(current_weather['weather_code'])}")
    if current_weather.get("weather_main"):
        lines.append(f"weather_main={current_weather['weather_main']}")
    if current_weather.get("description"):
        lines.append(f"description={current_weather['description']}")
    if current_weather.get("is_day") is not None:
        lines.append(f"is_day={bool(current_weather['is_day'])}")
    return "\n".join(lines)
