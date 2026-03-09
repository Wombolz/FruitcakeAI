"""
FruitcakeAI v5 — Task planner utilities.

Creates/updates TaskStep plans for a task owned by a user.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import litellm
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.core import _litellm_kwargs
from app.config import settings
from app.db.models import Task, TaskStep
from app.metrics import metrics


async def create_task_plan_for_user(
    db: AsyncSession,
    *,
    task_id: int,
    user_id: int,
    goal: str,
    max_steps: int = 6,
    notes: str = "",
    style: str = "concise",
    model_override: str | None = None,
) -> Dict[str, Any]:
    """Generate and persist TaskStep rows for a task owned by the user."""
    result = await db.execute(select(Task).where(Task.id == task_id, Task.user_id == user_id))
    task = result.scalar_one_or_none()
    if task is None:
        raise ValueError("Task not found")

    bounded_steps = min(max(max_steps, 1), 12)
    steps = await _generate_plan_steps(
        goal=goal.strip() or task.title,
        task_instruction=task.instruction,
        max_steps=bounded_steps,
        notes=notes.strip(),
        style=style.strip() or "concise",
        model_override=model_override,
    )

    had_plan = bool(task.has_plan)
    await db.execute(delete(TaskStep).where(TaskStep.task_id == task.id))

    created: List[TaskStep] = []
    for idx, step in enumerate(steps, start=1):
        created.append(
            TaskStep(
                task_id=task.id,
                step_index=idx,
                title=str(step.get("title", f"Step {idx}")).strip()[:255] or f"Step {idx}",
                instruction=str(step.get("instruction", "")).strip() or f"Work on step {idx}",
                requires_approval=bool(step.get("requires_approval", False)),
                status="pending",
            )
        )
    db.add_all(created)

    task.has_plan = True
    task.current_step_index = 1 if created else None
    if created:
        task.plan_version = (task.plan_version or 1) + 1 if had_plan else (task.plan_version or 1)

    await db.flush()

    return {
        "task_id": task.id,
        "steps_created": len(created),
        "titles": [s.title for s in created],
        "plan_version": task.plan_version,
    }


async def _generate_plan_steps(
    *,
    goal: str,
    task_instruction: str,
    max_steps: int,
    notes: str,
    style: str,
    model_override: str | None,
) -> List[Dict[str, Any]]:
    """Generate a strict JSON step list with an LLM-first + deterministic fallback."""
    prompt = (
        "Return ONLY valid JSON (no markdown) as an array of step objects.\n"
        "Schema: [{\"title\": str, \"instruction\": str, \"requires_approval\": bool}]\n"
        f"Create at most {max_steps} steps.\n"
        f"Style: {style}\n"
        f"Goal: {goal}\n"
        f"Task instruction: {task_instruction}\n"
        f"Notes: {notes or 'None'}\n"
        "Keep steps actionable and ordered."
    )

    selected_model = model_override or (
        settings.task_large_model if settings.task_model_routing_enabled and settings.task_force_large_for_planning
        else settings.llm_model
    )

    try:
        if settings.task_model_routing_enabled and selected_model == settings.task_large_model:
            metrics.inc_task_model_planning_large_calls()
        resp = await litellm.acompletion(
            model=selected_model,
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            tool_choice=None,
            **_litellm_kwargs(),
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        normalized = _normalize_steps(parsed, max_steps)
        if normalized:
            return normalized
    except Exception:
        pass

    return _fallback_steps(goal=goal, task_instruction=task_instruction, max_steps=max_steps)


def _normalize_steps(data: Any, max_steps: int) -> List[Dict[str, Any]]:
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data[:max_steps]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        instruction = str(item.get("instruction", "")).strip()
        if not title or not instruction:
            continue
        out.append(
            {
                "title": title[:255],
                "instruction": instruction,
                "requires_approval": bool(item.get("requires_approval", False)),
            }
        )
    return out


def _fallback_steps(goal: str, task_instruction: str, max_steps: int) -> List[Dict[str, Any]]:
    steps = [
        {"title": "Clarify objective", "instruction": f"Restate success criteria for: {goal}", "requires_approval": False},
        {"title": "Gather context", "instruction": task_instruction, "requires_approval": False},
        {"title": "Execute core work", "instruction": f"Complete the main work needed to achieve: {goal}", "requires_approval": False},
        {"title": "Verify output", "instruction": "Check results for correctness and completeness.", "requires_approval": False},
        {"title": "Summarize result", "instruction": "Provide a concise final summary and next actions.", "requires_approval": False},
    ]
    return steps[:max_steps]
