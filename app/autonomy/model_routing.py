"""
Task-mode model routing profile resolver.

Phase 5.4.x v1:
- Reads environment-backed settings only
- No persona/profile DB fields yet
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class TaskModelProfile:
    planning_model: str
    execution_model: str
    final_synthesis_model: str
    routing_enabled: bool
    large_retry_enabled: bool
    large_retry_max_attempts: int


def resolve_task_model_profile(task, user) -> TaskModelProfile:
    del user  # v1: config-only routing

    routing_enabled = bool(settings.task_model_routing_enabled)
    small_model = settings.task_small_model or settings.llm_model
    large_model = settings.task_large_model or settings.llm_model
    task_profile = (getattr(task, "profile", None) or "").strip().lower()

    if task_profile == "maintenance":
        chosen = small_model if routing_enabled else settings.llm_model
        return TaskModelProfile(
            planning_model=chosen,
            execution_model=chosen,
            final_synthesis_model=chosen,
            routing_enabled=routing_enabled,
            large_retry_enabled=False,
            large_retry_max_attempts=0,
        )

    planning_model = large_model if settings.task_force_large_for_planning else small_model
    final_model = large_model if settings.task_force_large_for_final_synthesis else small_model

    return TaskModelProfile(
        planning_model=planning_model if routing_enabled else settings.llm_model,
        execution_model=small_model if routing_enabled else settings.llm_model,
        final_synthesis_model=final_model if routing_enabled else settings.llm_model,
        routing_enabled=routing_enabled,
        large_retry_enabled=bool(settings.task_large_retry_enabled and routing_enabled),
        large_retry_max_attempts=max(0, int(settings.task_large_retry_max_attempts)),
    )
