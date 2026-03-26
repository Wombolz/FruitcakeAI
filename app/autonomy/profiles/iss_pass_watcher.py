from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.autonomy.profiles.base import TaskExecutionProfile
from app.autonomy.profiles.spec_loader import load_profile_spec_text


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
        contract = _build_contract(instruction)
        return {
            "api_contract": contract,
            "dataset_stats": {
                "service": contract["service"],
                "endpoint": contract["endpoint"],
                "query_params_present": sorted(contract["query_params"].keys()),
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
            )


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
    return str(match.group(1)).strip().lower()


def _build_contract(instruction: str) -> Dict[str, Any]:
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
    return {
        "service": "n2yo",
        "endpoint": "iss_visual_passes",
        "secret_name": _extract_secret_name(instruction),
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
