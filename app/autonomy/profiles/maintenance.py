from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from app.autonomy.profiles.base import DefaultPlannerFn, TaskExecutionProfile

_MAINTENANCE_ALLOWED_TOOLS = {"refresh_rss_cache"}


def _parse_maintenance_instruction(task_instruction: str) -> Dict[str, Any]:
    lines = (task_instruction or "").splitlines()
    tool_name = ""
    args: Dict[str, Any] = {}
    errors: List[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "tool":
            tool_name = value
        elif key == "args":
            if not value:
                args = {}
                continue
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                errors.append(f"Malformed maintenance args JSON: {exc.msg}.")
                continue
            if not isinstance(parsed, dict):
                errors.append("Maintenance args must be a JSON object.")
                continue
            args = parsed

    tool_name = tool_name.strip()
    if not tool_name:
        errors.append("Missing required maintenance header 'tool:'.")
    elif tool_name not in _MAINTENANCE_ALLOWED_TOOLS:
        allowed = ", ".join(sorted(_MAINTENANCE_ALLOWED_TOOLS))
        errors.append(f"Unsupported maintenance tool '{tool_name}'. Allowed: {allowed}.")

    return {
        "tool": tool_name,
        "args": args,
        "errors": errors,
    }


class MaintenanceExecutionProfile(TaskExecutionProfile):
    name = "maintenance"

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
        parsed = _parse_maintenance_instruction(task_instruction)
        tool_name = parsed.get("tool") or "maintenance_tool"
        return [
            {
                "title": "Execute Maintenance Action",
                "instruction": (
                    "Call the declared maintenance tool exactly once with the declared args. "
                    "Return the exact tool output with no extra text.\n\n"
                    f"Declared tool: {tool_name}\n"
                    f"Declared args: {json.dumps(parsed.get('args') or {}, sort_keys=True)}"
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
        parsed = _parse_maintenance_instruction(getattr(task, "instruction", "") or "")
        return {
            "maintenance_config": parsed,
        }

    def effective_blocked_tools(self, *, run_context: Dict[str, Any]) -> set[str]:
        del run_context
        return {
            "create_memory",
            "search_memory_graph",
            "open_memory_graph_nodes",
            "create_memory_entities",
            "create_memory_relations",
            "add_memory_observations",
        }

    def effective_allowed_tools(self, *, run_context: Dict[str, Any]) -> Optional[set[str]]:
        config = run_context.get("maintenance_config") or {}
        tool_name = str(config.get("tool") or "").strip()
        if not tool_name:
            return set()
        return {tool_name}

    def augment_prompt(
        self,
        *,
        prompt_parts: list[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> None:
        del is_final_step
        config = run_context.get("maintenance_config") or {}
        tool_name = str(config.get("tool") or "").strip()
        args = config.get("args") or {}
        prompt_parts.append(
            "Maintenance contract:\n"
            "- Use only the declared maintenance tool.\n"
            "- Call it exactly once.\n"
            "- Return the exact tool output with no extra text.\n"
            f"- Declared tool: {tool_name or 'MISSING'}\n"
            f"- Declared args: {json.dumps(args, sort_keys=True)}"
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
        config = run_context.get("maintenance_config") or {}
        errors = list(config.get("errors") or [])
        tool_name = str(config.get("tool") or "").strip()
        declared_args = config.get("args") or {}
        tool_records = list(run_context.get("last_tool_records") or [])

        called_names = [str(record.get("tool") or "") for record in tool_records if record.get("tool")]
        declared_tool_called = called_names.count(tool_name) == 1
        unexpected_tool_calls = [name for name in called_names if name != tool_name]
        exact_tool_output = ""
        if tool_name:
            for record in tool_records:
                if str(record.get("tool") or "") == tool_name:
                    exact_tool_output = str(record.get("result_summary") or "")
                    break
        exact_output_match = (result or "").strip() == exact_tool_output.strip()

        report: Dict[str, Any] = {
            "fatal": False,
            "declared_tool": tool_name,
            "declared_args": declared_args,
            "declared_tool_called": declared_tool_called,
            "unexpected_tool_calls": unexpected_tool_calls,
            "exact_output_match": exact_output_match,
        }

        if errors:
            report["fatal"] = True
            report["fatal_reason"] = errors[0]
            report["config_errors"] = errors
            return result, report

        if not declared_tool_called:
            report["fatal"] = True
            report["fatal_reason"] = (
                f"Maintenance run did not call declared tool '{tool_name}' exactly once."
            )
            return result, report

        if unexpected_tool_calls:
            report["fatal"] = True
            report["fatal_reason"] = "Maintenance run called unexpected tools."
            return result, report

        if not exact_output_match:
            report["tool_output"] = exact_tool_output
            report["output_replaced_with_tool_result"] = True
            return exact_tool_output, report

        return result, report

    def artifact_payloads(
        self,
        *,
        final_markdown: str,
        run_debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        diagnostics = {
            "declared_tool": ((run_debug.get("grounding_report") or {}).get("declared_tool")),
            "declared_args": ((run_debug.get("grounding_report") or {}).get("declared_args") or {}),
            "declared_tool_called": bool(
                (run_debug.get("grounding_report") or {}).get("declared_tool_called")
            ),
            "unexpected_tool_calls": list(
                (run_debug.get("grounding_report") or {}).get("unexpected_tool_calls") or []
            ),
            "exact_output_match": bool(
                (run_debug.get("grounding_report") or {}).get("exact_output_match")
            ),
            "dataset_stats": run_debug.get("dataset_stats", {}),
            "refresh_stats": run_debug.get("refresh_stats", {}),
            "suppression_events": run_debug.get("tool_failure_suppressions", []),
            "active_skills": run_debug.get("active_skills", []),
            "skill_selection_mode": run_debug.get("skill_selection_mode", ""),
            "skill_injection_events": run_debug.get("skill_injection_events", []),
        }
        out: List[Dict[str, Any]] = []
        if final_markdown:
            out.append({"artifact_type": "final_output", "content_text": final_markdown})
        grounding = run_debug.get("grounding_report")
        if grounding:
            out.append({"artifact_type": "validation_report", "content_json": grounding})
        out.append({"artifact_type": "run_diagnostics", "content_json": diagnostics})
        return out


__all__ = [
    "MaintenanceExecutionProfile",
    "_parse_maintenance_instruction",
]
