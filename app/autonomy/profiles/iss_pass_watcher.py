from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.spec_loader import load_profile_spec_text
from app.time_utils import format_local_and_utc_pair, is_valid_timezone_name


class ISSPassWatcherExecutionProfile(TaskExecutionProfile):
    name = "iss_pass_watcher"

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
                "title": "Check ISS passes",
                "instruction": (
                    "Use the prepared ISS API contract and the api_request tool only. "
                    "Summarize only new qualifying visible passes."
                ),
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
        contract = _build_contract(instruction, task_timezone=getattr(task, "active_hours_tz", None))
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
                "Prepared ISS API contract:\n"
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
    ) -> tuple[str, Optional[Dict[str, Any]]]:
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
        no_data_markers = (
            "no visible iss passes found",
            "no new iss pass changes",
            "no visible passes found",
        )
        structured_result = _parse_structured_api_result(api_result)
        api_failed = bool(api_result) and (
            api_result == "API request failed."
            or api_result.startswith("Tool api_request failed:")
            or "failed" in api_result.lower()
            or "require a named secret" in api_result.lower()
        )

        structured_override: str | None = None
        if structured_result:
            passes = structured_result.get("fields", {}).get("passes")
            if isinstance(passes, list):
                if not passes:
                    structured_override = "No visible ISS passes found in the requested window."
                elif not cleaned or any(marker in normalized for marker in fallback_markers):
                    structured_override = _format_structured_passes(
                        passes,
                        timezone_name=(run_context.get("api_contract") or {}).get("display_timezone"),
                    )

        if structured_override is not None:
            cleaned = structured_override
        elif api_result and (not cleaned or any(marker in normalized for marker in fallback_markers)):
            cleaned = api_result
        elif api_result and api_failed:
            cleaned = api_result
        elif api_result and any(marker in api_result.lower() for marker in no_data_markers):
            cleaned = api_result

        if cleaned.lower() == "api request failed." and api_result:
            cleaned = api_result

        report = {
            "fatal": False,
            "api_request_called": bool(api_result),
            "used_tool_result_fallback": cleaned == api_result and bool(api_result),
            "structured_api_result": bool(structured_result),
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
        return "n2yo_api_key"
    candidate = str(match.group(1)).strip().lower()
    if candidate != "n2yo_api_key":
        return "n2yo_api_key"
    return candidate


def _extract_timezone_name(text: str, *, default: Optional[str] = None) -> str:
    match = re.search(r"timezone\s*=\s*([A-Za-z_/\-]+)", text, flags=re.IGNORECASE)
    if match:
        candidate = str(match.group(1)).strip()
        if is_valid_timezone_name(candidate):
            return candidate
    if is_valid_timezone_name(default):
        return str(default).strip()
    return "UTC"


def _build_contract(instruction: str, *, task_timezone: Optional[str]) -> Dict[str, Any]:
    satellite_id = _extract_number(r"(?:NORAD|satellite)[^0-9]*(\d+)", instruction, cast=int, default=25544)
    lat = _extract_number(r"lat\s*=\s*(-?\d+(?:\.\d+)?)", instruction, cast=float, default=32.4485)
    lon = _extract_number(r"lon\s*=\s*(-?\d+(?:\.\d+)?)", instruction, cast=float, default=-81.7832)
    alt_meters = _extract_number(r"alt\s*=\s*(\d+)", instruction, cast=int, default=60)
    days = _extract_number(r"days\s*=\s*(\d+)", instruction, cast=int, default=1)
    min_visibility_seconds = _extract_number(
        r"minVisibility\s*=\s*(\d+)", instruction, cast=int, default=120
    )
    min_max_elevation = _extract_number(
        r"max elevation\s*>=\s*(\d+)", instruction, cast=int, default=30
    )
    display_timezone = _extract_timezone_name(instruction, default=task_timezone)
    return {
        "service": "n2yo",
        "endpoint": "iss_visual_passes",
        "secret_name": _extract_secret_name(instruction),
        "display_timezone": display_timezone,
        "response_fields": {
            "passes": "passes",
        },
        "query_params": {
            "satellite_id": satellite_id,
            "lat": lat,
            "lon": lon,
            "alt_meters": alt_meters,
            "days": days,
            "min_visibility_seconds": min_visibility_seconds,
            "min_max_elevation_deg": min_max_elevation,
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


def _format_structured_passes(passes: List[Dict[str, Any]], *, timezone_name: Optional[str]) -> str:
    lines = ["ISS visible pass results:", ""]
    for idx, item in enumerate(passes, start=1):
        start_utc = str(item.get("start_utc") or "").strip() or "unknown"
        start_local = "unknown"
        try:
            if start_utc != "unknown":
                utc_dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
                start_local, start_utc = format_local_and_utc_pair(utc_dt, timezone_name=timezone_name)
        except Exception:
            start_local = "unknown"
        duration = item.get("duration_seconds")
        max_el = item.get("max_elevation_deg")
        lines.append(
            f"[{idx}] start_local={start_local} | start_utc={start_utc} | "
            f"duration_seconds={duration} | max_elevation_deg={float(max_el or 0.0):.1f}"
        )
    return "\n".join(lines)
