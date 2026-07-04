from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.autonomy.profiles.base import DefaultPlannerFn, TaskExecutionProfile
from app.mcp.services import rss_sources

_SUPPORTED_JOBS = {
    "nightly_memory_extraction",
    "refresh_rss_cache",
}


def _clamp_since_hours(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 24
    return max(1, min(168, parsed))


def _clamp_max_items_per_source(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 20
    return max(1, min(50, parsed))


def _parse_system_job_recipe(task_recipe: dict[str, Any]) -> Dict[str, Any]:
    recipe = task_recipe if isinstance(task_recipe, dict) else {}
    params = recipe.get("params") if isinstance(recipe.get("params"), dict) else {}
    job_name = str(params.get("job_name") or "").strip().lower()
    since_hours = _clamp_since_hours(params.get("since_hours"))
    max_items_per_source = _clamp_max_items_per_source(params.get("max_items_per_source"))
    category = str(params.get("category") or "").strip() or None
    errors: List[str] = []
    if not job_name:
        errors.append("Missing required system job param 'job_name'.")
    elif job_name not in _SUPPORTED_JOBS:
        allowed = ", ".join(sorted(_SUPPORTED_JOBS))
        errors.append(f"Unsupported system job '{job_name}'. Allowed: {allowed}.")
    return {
        "job_name": job_name,
        "since_hours": since_hours,
        "max_items_per_source": max_items_per_source,
        "category": category,
        "errors": errors,
    }


class SystemJobExecutionProfile(TaskExecutionProfile):
    name = "system_job"

    def execution_backend(self, *, run_context: Dict[str, Any]) -> str:
        del run_context
        return "deterministic_job"

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
        default_planner: DefaultPlannerFn,
    ) -> List[Dict[str, Any]]:
        del goal, user_id, task_id, max_steps, notes, style, model_override, default_planner
        parsed = _parse_system_job_recipe({"params": _extract_params_from_instruction(task_instruction)})
        job_name = parsed.get("job_name") or "system_job"
        return [
            {
                "title": "Execute System Job",
                "instruction": f"Run deterministic system job '{job_name}' with no LLM planning.",
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
        parsed = _parse_system_job_recipe(getattr(task, "task_recipe", {}) or {})
        return {"system_job_config": parsed}

    def allow_skill_injection(self, *, run_context: Dict[str, Any]) -> bool:
        del run_context
        return False

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        del run_context
        return set()

    def effective_allowed_tools(self, *, run_context: Dict[str, Any]) -> Optional[set[str]]:
        del run_context
        return set()

    def augment_prompt(
        self,
        *,
        prompt_parts: list[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> None:
        del prompt_parts, run_context, is_final_step
        return None

    async def execute_non_agent(
        self,
        *,
        db,
        task,
        user,
        run_context: Dict[str, Any],
        task_run_id: Optional[int],
    ) -> Tuple[str, Dict[str, Any]]:
        del task_run_id
        config = run_context.get("system_job_config") if isinstance(run_context, dict) else {}
        config = config if isinstance(config, dict) else {}
        errors = list(config.get("errors") or [])
        if errors:
            raise ValueError(errors[0])

        job_name = str(config.get("job_name") or "").strip().lower()
        since_hours = _clamp_since_hours(config.get("since_hours"))
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if job_name == "nightly_memory_extraction":
            from app.memory.extraction import run_nightly_memory_extraction

            totals = await run_nightly_memory_extraction(db, since_hours=since_hours)
            result = (
                f"Nightly memory extraction completed at {now}.\n"
                f"- Users scanned: {int(totals.get('users') or 0)}\n"
                f"- Candidates: {int(totals.get('candidates') or 0)}\n"
                f"- Queued for review: {int(totals.get('queued') or 0)}\n"
                f"- Auto-approved: {int(totals.get('auto_approved') or 0)}\n"
                f"- Skipped duplicates: {int(totals.get('skipped_duplicates') or 0)}"
            )
            run_debug = {
                "profile": self.name,
                "system_job": {
                    "job_name": job_name,
                    "since_hours": since_hours,
                    "totals": totals,
                    "executed_for_user_id": getattr(user, "id", None),
                    "task_id": getattr(task, "id", None),
                },
                "grounding_report": {
                    "job_name": job_name,
                    "since_hours": since_hours,
                    "totals": totals,
                    "deterministic": True,
                },
                "active_skills": [],
                "skill_selection_mode": "disabled_for_system_job",
                "skill_injection_events": [],
            }
            return result, run_debug

        if job_name == "refresh_rss_cache":
            max_items_per_source = _clamp_max_items_per_source(config.get("max_items_per_source"))
            category = str(config.get("category") or "").strip() or None
            refreshed = await rss_sources.refresh_active_sources_cache(
                db,
                user_id=getattr(user, "id", None),
                category=category,
                max_items_per_source=max_items_per_source,
            )
            result = (
                "RSS_REFRESH_OK\n"
                f"sources_refreshed: {int(refreshed.get('sources') or 0)}\n"
                f"items_seen: {int(refreshed.get('items') or 0)}\n"
                f"timestamp_utc: {now}"
            )
            run_debug = {
                "profile": self.name,
                "system_job": {
                    "job_name": job_name,
                    "category": category,
                    "max_items_per_source": max_items_per_source,
                    "totals": refreshed,
                    "executed_for_user_id": getattr(user, "id", None),
                    "task_id": getattr(task, "id", None),
                },
                "grounding_report": {
                    "job_name": job_name,
                    "category": category,
                    "max_items_per_source": max_items_per_source,
                    "totals": refreshed,
                    "deterministic": True,
                },
                "active_skills": [],
                "skill_selection_mode": "disabled_for_system_job",
                "skill_injection_events": [],
            }
            return result, run_debug

        raise ValueError(f"Unsupported system job '{job_name}'.")


def _extract_params_from_instruction(task_instruction: str) -> Dict[str, Any]:
    lines = (task_instruction or "").splitlines()
    params: Dict[str, Any] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "job_name":
            params["job_name"] = value
        elif key == "since_hours":
            params["since_hours"] = value
        elif key == "max_items_per_source":
            params["max_items_per_source"] = value
        elif key == "category":
            params["category"] = value
    return params


__all__ = [
    "SystemJobExecutionProfile",
]
