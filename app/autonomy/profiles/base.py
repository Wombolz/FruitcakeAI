from __future__ import annotations

from abc import ABC
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


DefaultPlannerFn = Callable[..., Awaitable[List[Dict[str, Any]]]]


class TaskExecutionProfile(ABC):
    name = "default"

    async def plan_steps(
        self,
        *,
        goal: str,
        task_instruction: str,
        max_steps: int,
        notes: str,
        style: str,
        model_override: str | None,
        default_planner: DefaultPlannerFn,
    ) -> List[Dict[str, Any]]:
        return await default_planner(
            goal=goal,
            task_instruction=task_instruction,
            max_steps=max_steps,
            notes=notes,
            style=style,
            model_override=model_override,
        )

    async def prepare_run_context(
        self,
        *,
        db,
        user_id: int,
        task_run_id: Optional[int],
    ) -> Dict[str, Any]:
        return {}

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        return set()

    def augment_prompt(
        self,
        *,
        prompt_parts: list[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> None:
        return None

    def validate_finalize(
        self,
        *,
        result: str,
        prior_full_outputs: List[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        return result, None

    def artifact_payloads(
        self,
        *,
        final_markdown: str,
        run_debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        diagnostics = {
            "dataset_stats": run_debug.get("dataset_stats", {}),
            "refresh_stats": run_debug.get("refresh_stats", {}),
            "suppression_events": run_debug.get("tool_failure_suppressions", []),
        }
        out: List[Dict[str, Any]] = []
        if final_markdown:
            out.append({"artifact_type": "final_output", "content_text": final_markdown})
        out.append({"artifact_type": "run_diagnostics", "content_json": diagnostics})
        return out
