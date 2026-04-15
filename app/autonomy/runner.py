"""
FruitcakeAI v5 — TaskRunner (Phase 4)

Executes scheduled tasks in isolated agent sessions.

Design:
- Each task runs in a throwaway ChatSession (is_task_session=True, hidden from chat UI)
- Memories are retrieved from MemoryService and injected into the prompt
- The agent uses all available tools (calendar, web_search, etc.) naturally
- APPROVAL_REQUIRED_TOOLS pause the task at waiting_approval before executing
- Exponential retry on transient failures: [30, 60, 300, 900, 3600] seconds
- Max 2 concurrent task runs enforced by asyncio.Semaphore
- APNs push is a stub; Sprint 4.3 wires the real APNsPusher
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import structlog
from sqlalchemy import delete, select, update

from app.autonomy.approval import ApprovalRequired, _approval_armed
from app.autonomy.configured_executor import build_preserved_runtime_state, resolve_task_execution_contract
from app.autonomy.model_routing import TaskModelProfile, resolve_task_model_profile
from app.config import settings
from app.db.models import Task, TaskRun, TaskRunArtifact, TaskStep, User
from app.llm_usage import bind_llm_usage_context, reset_llm_usage_context
from app.metrics import metrics
from app.task_service import compute_next_run_at
from app.agent.tools import (
    get_tool_execution_records,
    reset_tool_execution_records,
    restore_tool_execution_records,
)

log = structlog.get_logger(__name__)

# Exponential retry delays (seconds) for transient errors
RETRY_DELAYS = [30, 60, 300, 900, 3600]
REPEATED_TOOL_ERROR_THRESHOLD = 2
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAX_REQUIRED_CONTEXT_FILES = 4
_MAX_REQUIRED_CONTEXT_FILE_CHARS = 5000
_ESTIMATED_CHARS_PER_TOKEN = 4

# Limit concurrent task-mode agent loops
_semaphore = asyncio.Semaphore(2)


def _load_required_context_text(sources: list[str] | tuple[str, ...]) -> tuple[str, list[str]]:
    loaded_paths: list[str] = []
    blocks: list[str] = []
    for raw in list(sources)[:_MAX_REQUIRED_CONTEXT_FILES]:
        rel = str(raw or "").strip()
        if not rel:
            continue
        candidate = Path(rel).expanduser()
        if candidate.is_absolute():
            path = candidate.resolve()
            display_path = str(path)
        else:
            path = (_REPO_ROOT / rel).resolve()
            try:
                path.relative_to(_REPO_ROOT)
            except ValueError:
                continue
            display_path = rel
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not text:
            continue
        snippet = text[:_MAX_REQUIRED_CONTEXT_FILE_CHARS].strip()
        loaded_paths.append(display_path)
        blocks.append(f"[Source: {display_path}]\n{snippet}")
    if not blocks:
        return "", loaded_paths
    preloaded = (
        "Preloaded required context sources. Treat these files as already available context before falling back to search.\n"
        "Do not spend turns searching for or reopening these same files unless you specifically need content beyond the excerpt provided below.\n\n"
        + "\n\n---\n\n".join(blocks)
    )
    return preloaded, loaded_paths


def _estimate_text_tokens(text: str) -> int:
    chars = len(str(text or ""))
    return max(1, chars // _ESTIMATED_CHARS_PER_TOKEN) if chars else 0


def _truncate_text_to_estimated_tokens(text: str, *, max_tokens: int) -> str:
    value = str(text or "")
    budget = max(1, int(max_tokens))
    if _estimate_text_tokens(value) <= budget:
        return value
    max_chars = max(1, budget * _ESTIMATED_CHARS_PER_TOKEN)
    truncated = value[:max_chars]
    while truncated and _estimate_text_tokens(truncated) > budget:
        truncated = truncated[:-1]
    return truncated.rstrip() + "…"


class TaskRunner:
    """
    Runs a Task in an isolated agent session.

    Call execute(task) from the scheduler tick or from the manual-trigger
    API endpoint. The semaphore ensures at most 2 tasks run concurrently.
    """

    def __init__(self) -> None:
        self._active_runs: dict[int, asyncio.Task] = {}
        self._active_lock = asyncio.Lock()

    async def execute(self, task: Task, *, trigger_source: str = "direct") -> None:
        expected_next_run_at = getattr(task, "next_run_at", None)
        if expected_next_run_at is None:
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                task_row = await db.get(Task, task.id)
                expected_next_run_at = task_row.next_run_at if task_row is not None else None
        async with _semaphore:
            await self._run(
                task.id,
                expected_next_run_at=expected_next_run_at,
                trigger_source=trigger_source,
            )

    async def request_stop(self, task_id: int) -> bool:
        """
        Request cancellation of an actively running task coroutine.

        Returns True when a live run was found and cancellation was signaled.
        """
        async with self._active_lock:
            run_task = self._active_runs.get(task_id)
        if run_task is None or run_task.done():
            return False
        run_task.cancel()
        return True

    # ------------------------------------------------------------------
    # Core execution pipeline
    # ------------------------------------------------------------------

    async def _run(
        self,
        task_id: int,
        *,
        expected_next_run_at: datetime | None = None,
        trigger_source: str = "direct",
    ) -> None:
        from sqlalchemy import select
        from app.db.session import AsyncSessionLocal

        # Phase 1: Re-fetch and mark running
        # Extract title and deliver before session closes to avoid DetachedInstanceError
        task_title: Optional[str] = None
        task_deliver: bool = False
        task_run_id: Optional[int] = None
        run_debug: dict[str, object] = {}

        current = asyncio.current_task()
        async with self._active_lock:
            existing = self._active_runs.get(task_id)
            if existing is not None and not existing.done():
                log.info("task.skip_duplicate_active_coroutine", task_id=task_id)
                return
            if current is not None:
                self._active_runs[task_id] = current

        try:
            preflight = await _preflight_llm_dispatch()
            if preflight is not None:
                cooldown_seconds, reason = preflight
                await self._pause_for_llm_unavailable(
                    task_id=task_id,
                    cooldown_seconds=cooldown_seconds,
                    reason=reason,
                )
                return

            async with AsyncSessionLocal() as db:
                claim = await db.execute(
                    update(Task)
                    .where(
                        Task.id == task_id,
                        Task.status == "pending",
                        Task.next_run_at == expected_next_run_at,
                    )
                    .values(
                        status="running",
                        error=None,
                        next_retry_at=None,
                    )
                )
                if (claim.rowcount or 0) == 0:
                    task = await db.get(Task, task_id)
                    status = task.status if task is not None else "missing"
                    current_next_run_at = getattr(task, "next_run_at", None) if task is not None else None
                    if task is not None and status == "pending" and current_next_run_at != expected_next_run_at:
                        log.info(
                            "task.skip_stale_queued_dispatch",
                            task_id=task_id,
                            status=status,
                            trigger_source=trigger_source,
                            expected_next_run_at=expected_next_run_at.isoformat()
                            if expected_next_run_at is not None
                            else None,
                            current_next_run_at=current_next_run_at.isoformat()
                            if current_next_run_at is not None
                            else None,
                        )
                    else:
                        log.info(
                            "task.skip_non_pending",
                            task_id=task_id,
                            status=status,
                            trigger_source=trigger_source,
                            expected_next_run_at=expected_next_run_at.isoformat()
                            if expected_next_run_at is not None
                            else None,
                        )
                    await db.rollback()
                    return

                task = await db.get(Task, task_id)
                if task is None:
                    log.warning("task.not_found", task_id=task_id)
                    await db.rollback()
                    return
                recipe = task.task_recipe if isinstance(task.task_recipe, dict) else {}
                recipe_family = str(recipe.get("family") or "").strip().lower()
                recipe_params = recipe.get("params") if isinstance(recipe.get("params"), dict) else {}
                resolved_agent_role = None
                resolved_run_kind = "task"
                if recipe_family == "agent":
                    resolved_run_kind = "agent"
                    role = str(recipe_params.get("agent_role") or "").strip()
                    resolved_agent_role = role or None
                active_run_rows = await db.execute(
                    select(TaskRun)
                    .where(
                        TaskRun.task_id == task.id,
                        TaskRun.status.in_(["running", "waiting_approval"]),
                    )
                    .order_by(TaskRun.id.desc())
                )
                active_run = active_run_rows.scalars().first()
                if active_run is not None:
                    if active_run.status == "waiting_approval":
                        active_run.status = "running"
                        active_run.finished_at = None
                        active_run.error = None
                        if resolved_run_kind == "agent":
                            active_run.run_kind = "agent"
                            active_run.agent_role = resolved_agent_role
                        task_run_id = active_run.id
                    else:
                        task.status = "running"
                        await db.commit()
                        log.info(
                            "task.skip_duplicate_active_run",
                            task_id=task_id,
                            active_run_id=active_run.id,
                            active_status=active_run.status,
                        )
                        return
                task_title = task.title
                task_deliver = task.deliver
                if task_run_id is None:
                    run = TaskRun(
                        task_id=task.id,
                        status="running",
                        run_kind=resolved_run_kind,
                        agent_role=resolved_agent_role,
                    )
                    db.add(run)
                    await db.flush()
                    task_run_id = run.id
                await db.commit()

            log.info("task.started", task_id=task_id, title=task_title)

            result, run_debug = await self._execute_agent(task_id, task_run_id=task_run_id)
            result = _format_result_for_inbox(result)

            # Phase 6a: Success — update task record
            async with AsyncSessionLocal() as db:
                from app.db.models import ChatMessage
                task = await db.get(Task, task_id)
                user = await db.get(User, task.user_id) if task is not None else None
                task.status = "completed"
                task.result = result
                task.error = None
                task.last_run_at = datetime.now(timezone.utc)
                task.retry_count = 0

                recurring_snapshot = ""
                if task.task_type == "recurring" and task.schedule:
                    recurring_snapshot = await self._reset_steps_for_new_run(
                        db,
                        task.id,
                        run_debug=run_debug,
                    )
                    task.next_run_at = compute_next_run_at(
                        task.schedule,
                        task_timezone=task.active_hours_tz,
                        user_timezone=getattr(user, "active_hours_tz", None),
                    )
                    task.status = "pending"   # re-queue for next run

                if task_run_id:
                    run = await db.get(TaskRun, task_run_id)
                    if run is not None:
                        run.status = "completed"
                        run.finished_at = datetime.now(timezone.utc)
                        run.error = None
                        diagnostics = _format_run_diagnostics(run_debug)
                        if recurring_snapshot:
                            merged = f"{(result or '').strip()}\n\n{recurring_snapshot}".strip()
                            if diagnostics:
                                merged = f"{merged}\n\n{diagnostics}".strip()
                            run.summary = merged[:4000]
                        else:
                            merged = (result or "").strip()
                            if diagnostics:
                                merged = f"{merged}\n\n{diagnostics}".strip()
                            run.summary = merged[:4000]
                    await _persist_run_artifacts(
                        db,
                        task_run_id=task_run_id,
                        final_markdown=result,
                        run_debug=run_debug,
                    )

                # Save instruction + result as messages so the session is browsable
                # and "Reply in Chat" can load real context.
                if task.last_session_id:
                    db.add(ChatMessage(
                        session_id=task.last_session_id,
                        role="user",
                        content=task.instruction,
                    ))
                    db.add(ChatMessage(
                        session_id=task.last_session_id,
                        role="assistant",
                        content=result,
                    ))

                await db.commit()

            log.info("task.completed", task_id=task_id, result_len=len(result or ""))

            # Phase 7: Deliver result (APNs stub — real push wired in Sprint 4.3)
            suppress_push = bool((run_debug.get("grounding_report") or {}).get("suppress_push"))
            if not suppress_push and str(run_debug.get("profile") or "") == "topic_watcher":
                suppress_push = (result or "").strip() == "NOTHING_NEW"
            if task_deliver and result and not suppress_push:
                await self._push(task_id, result)

        except asyncio.CancelledError:
            await self._handle_cancelled(task_id, task_run_id=task_run_id)

        except ApprovalRequired as exc:
            # The approval gate was triggered — persist waiting_approval state
            async with AsyncSessionLocal() as db:
                task = await db.get(Task, task_id)
                task.status = "waiting_approval"
                task.last_run_at = datetime.now(timezone.utc)
                if task.current_step_index is not None:
                    rows = await db.execute(
                        select(TaskStep).where(
                            TaskStep.task_id == task.id,
                            TaskStep.step_index == task.current_step_index,
                        )
                    )
                    step = rows.scalar_one_or_none()
                    if step is not None:
                        step.status = "waiting_approval"
                        step.waiting_approval_tool = str(exc)
                if task_run_id:
                    run = await db.get(TaskRun, task_run_id)
                    if run is not None:
                        run.status = "waiting_approval"
                        run.finished_at = datetime.now(timezone.utc)
                        run.error = str(exc)
                await db.commit()
            log.info("task.waiting_approval", task_id=task_id, blocked_tool=str(exc))

        except Exception as exc:
            await self._handle_error(task_id, exc, task_run_id=task_run_id)
        finally:
            async with self._active_lock:
                if self._active_runs.get(task_id) is current:
                    self._active_runs.pop(task_id, None)

    # ------------------------------------------------------------------
    # Agent execution in isolated session
    # ------------------------------------------------------------------

    async def _execute_agent(
        self,
        task_id: int,
        task_run_id: Optional[int] = None,
    ) -> tuple[str, dict[str, object]]:
        from sqlalchemy import select
        from app.db.session import AsyncSessionLocal
        from app.db.models import ChatSession, User
        from app.agent.core import run_agent
        from app.autonomy.planner import create_task_plan_for_user
        from app.memory.service import get_memory_service

        # Load task + user; extract values before session closes to avoid DetachedInstanceError
        persona_name: Optional[str] = None
        agent_role: Optional[str] = None
        session_id: Optional[int] = None
        task_user_id: Optional[int] = None
        task_instruction: Optional[str] = None
        task_title: Optional[str] = None
        task_requires_approval: bool = False
        has_plan = False
        pre_approved = False
        model_profile: TaskModelProfile | None = None
        task_profile = None
        preloaded_required_context = ""
        loaded_required_context_sources: list[str] = []

        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            user = await db.get(User, task.user_id)
            if user is None:
                raise ValueError(f"User {task.user_id} not found for task {task_id}")

            # Check pre-approval flag (set by PATCH /tasks/{id} {"approved": true})
            pre_approved = bool(task.pre_approved)
            if pre_approved:
                task.pre_approved = False
                await db.commit()

            from app.autonomy.execution_profile import resolve_execution_profile
            resolved = resolve_execution_profile(task, user)
            model_profile = resolve_task_model_profile(task, user)
            task_profile = resolve_task_execution_contract(task, user)
            persona_name = resolved.persona
            task_user_id = task.user_id
            task_instruction = task.instruction
            task_title = task.title
            task_requires_approval = task.requires_approval
            has_plan = bool(task.has_plan)
            recipe = task.task_recipe if isinstance(task.task_recipe, dict) else {}
            if str(recipe.get("family") or "").strip().lower() == "agent":
                params = recipe.get("params") if isinstance(recipe.get("params"), dict) else {}
                agent_role = str(params.get("agent_role") or "").strip() or None
                task_context_paths = [
                    str(item).strip()
                    for item in (params.get("context_paths") or [])
                    if str(item).strip()
                ]
            else:
                task_context_paths = []
            # Lazy backfill: persist inferred/defaulted persona for legacy tasks.
            if not task.persona:
                task.persona = persona_name
                await db.flush()
            log.info(
                "task.execution_profile",
                task_id=task.id,
                persona=persona_name,
                allowed_tools_count=len(resolved.allowed_tools),
                blocked_tools_count=len(resolved.blocked_tools),
            )

            if _is_agent_recipe_task(task) and has_plan:
                await db.execute(delete(TaskStep).where(TaskStep.task_id == task.id))
                task.has_plan = False
                task.current_step_index = None
                has_plan = False
                log.info("task.cleared_stale_agent_plan", task_id=task.id)

            # Scheduled/autonomous tasks without a plan get an explicit plan once.
            if not has_plan and self._should_auto_plan(task):
                plan_goal = (task.title or task.instruction or "").strip() or "Scheduled task"
                await create_task_plan_for_user(
                    db,
                    task_id=task.id,
                    user_id=task.user_id,
                    goal=plan_goal,
                    max_steps=settings.task_plan_default_steps,
                    notes=task.instruction or "",
                    style="concise",
                    model_override=model_profile.planning_model,
                )
                has_plan = bool(task.has_plan)
                log.info("task.auto_planned", task_id=task.id, has_plan=has_plan)

            # Reuse existing task session when resuming from waiting_approval.
            if task.last_session_id and task.status == "pending":
                existing = await db.get(ChatSession, task.last_session_id)
                session_id = existing.id if existing else None

            if not session_id:
                session = ChatSession(
                    user_id=task.user_id,
                    title=f"[Task] {task_title}",
                    persona=persona_name,
                    is_task_session=True,
                )
                db.add(session)
                await db.flush()
                session_id = session.id
                task.last_session_id = session_id
            if task_run_id:
                run = await db.get(TaskRun, task_run_id)
                if run is not None:
                    run.session_id = session_id
            await db.commit()

        # Build UserContext (re-fetch user while session open so from_user can read attributes)
        from app.agent.context import UserContext
        from app.agent.definition_loader import get_agent_preset
        from app.skills.service import hydrate_user_context

        async with AsyncSessionLocal() as db:
            user = await db.get(User, task_user_id)
            if user is None:
                raise ValueError(f"User {task_user_id} not found for task {task_id}")
            user_context = UserContext.from_user(user, persona_name=persona_name)
            user_context.allowed_tool_cap = list(resolved.allowed_tools)
            user_context = await hydrate_user_context(
                db,
                user_context,
                query=(task_instruction or task_title or ""),
            )
        if agent_role:
            preset = get_agent_preset(agent_role)
            combined_context_sources: list[str] = []
            for item in task_context_paths:
                if item not in combined_context_sources:
                    combined_context_sources.append(item)
            if preset:
                for item in preset.required_context_sources:
                    if item not in combined_context_sources:
                        combined_context_sources.append(item)
            if preset and preset.behavior_instructions:
                merged = list(user_context.behavior_instructions)
                for instruction in preset.behavior_instructions:
                    if instruction not in merged:
                        merged.append(instruction)
                user_context.behavior_instructions = merged
            if combined_context_sources:
                preloaded_required_context, loaded_required_context_sources = _load_required_context_text(
                    combined_context_sources
                )
        user_context.session_id = session_id
        user_context.task_id = task_id

        # Planned mode: execute TaskStep graph.
        if has_plan:
            async with AsyncSessionLocal() as db:
                count_rows = await db.execute(
                    select(TaskStep.id).where(TaskStep.task_id == task_id).limit(1)
                )
                if count_rows.scalar_one_or_none() is not None:
                    return await self._execute_planned_steps(
                        task_id=task_id,
                        user_id=task_user_id,
                        task_title=task_title or "",
                        task_instruction=task_instruction or "",
                        session_id=session_id,
                        user_context=user_context,
                        task_requires_approval=task_requires_approval,
                        pre_approved=pre_approved,
                        model_profile=model_profile,
                        task_run_id=task_run_id,
                        task_profile=task_profile,
                        preloaded_required_context=preloaded_required_context,
                        loaded_required_context_sources=loaded_required_context_sources,
                    )

        # Back-compat mode: single instruction task behavior.
        recalled_memory_ids: set[int] = set()
        async with AsyncSessionLocal() as db:
            svc = get_memory_service()
            memories = await svc.retrieve_for_context(db, task_user_id, query=task_instruction)
            recalled_memory_ids.update(int(m.id) for m in memories)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts: list[str] = []
        if memories:
            parts.append(svc.format_for_prompt(memories))
        if preloaded_required_context:
            parts.append(preloaded_required_context)
        parts.append(f"[Task: {task_title}]")
        parts.append(task_instruction or "")
        parts.append(f"\nCurrent time: {now_str}")

        arm_approval = task_requires_approval and not pre_approved
        token = _approval_armed.set(arm_approval)
        usage_token = bind_llm_usage_context(
            user_id=task_user_id,
            session_id=session_id,
            task_id=task_id,
            task_run_id=task_run_id,
            source="task_runner",
        )
        from app.agent.core import (
            get_agent_loop_diagnostics,
            reset_agent_loop_diagnostics,
            restore_agent_loop_diagnostics,
        )
        diagnostics_token = reset_agent_loop_diagnostics()
        agent_loop_diagnostics: dict[str, object] = {}
        try:
            try:
                result = await run_agent(
                    [{"role": "user", "content": "\n\n".join(parts)}],
                    user_context,
                    mode="task",
                    model_override=model_profile.final_synthesis_model if model_profile else None,
                    stage="task_single_stage",
                )
            finally:
                agent_loop_diagnostics = get_agent_loop_diagnostics()
                restore_agent_loop_diagnostics(diagnostics_token)
            run_debug = {
                "profile": getattr(task_profile, "name", "default"),
                "required_context_sources": loaded_required_context_sources,
                "active_skills": list(user_context.active_skill_slugs or []),
                "skill_selection_mode": user_context.skill_selection_mode or "",
                "skill_injection_events": [
                    {
                        "stage": "task_single_stage",
                        "active_skills": list(user_context.active_skill_slugs or []),
                        "selection_mode": user_context.skill_selection_mode or "",
                        "details": list(user_context.skill_injection_details or []),
                    }
                ],
            }
            _append_agent_context_budgeting(
                run_debug,
                stage="task_single_stage",
                model=model_profile.final_synthesis_model if model_profile else settings.llm_model,
                diagnostics=agent_loop_diagnostics,
            )
            if recalled_memory_ids:
                await svc.mark_accessed(sorted(recalled_memory_ids), mode="task_materialized")
            return result, run_debug
        finally:
            reset_llm_usage_context(usage_token)
            _approval_armed.reset(token)

    @staticmethod
    def _should_auto_plan(task: Task) -> bool:
        if _is_agent_recipe_task(task):
            return False
        return bool(task.schedule) or task.task_type == "recurring"

    async def _execute_planned_steps(
        self,
        *,
        task_id: int,
        user_id: int,
        task_title: str,
        task_instruction: str,
        session_id: int,
        user_context,
        task_requires_approval: bool,
        pre_approved: bool,
        model_profile: TaskModelProfile,
        task_run_id: Optional[int] = None,
        task_profile=None,
        preloaded_required_context: str = "",
        loaded_required_context_sources: list[str] | None = None,
    ) -> tuple[str, dict[str, object]]:
        """Execute task steps sequentially from current_step_index."""
        from sqlalchemy import select
        from app.db.session import AsyncSessionLocal
        from app.agent.core import run_agent
        from app.memory.service import get_memory_service
        from app.skills.service import hydrate_user_context

        final_result = ""
        repeated_error_counts: dict[str, int] = {}
        suppression_events: list[dict[str, str | int]] = []
        step_user_context = user_context
        run_context: dict[str, object] = {}
        grounding_report: dict[str, object] | None = None
        recalled_memory_ids: set[int] = set()
        run_debug: dict[str, object] = {
            "profile": getattr(task_profile, "name", "default"),
            "tool_failure_suppressions": suppression_events,
            "active_skills": list(step_user_context.active_skill_slugs or []),
            "skill_selection_mode": step_user_context.skill_selection_mode or "",
            "skill_injection_events": [],
            "required_context_sources": list(loaded_required_context_sources or []),
        }

        if task_profile and task_run_id:
            async with AsyncSessionLocal() as db:
                run_context = await task_profile.prepare_run_context(
                    db=db,
                    user_id=user_id,
                    task_id=task_id,
                    task_run_id=task_run_id,
                )
                await db.commit()
            if isinstance(run_context, dict):
                for key in ("dataset", "dataset_stats", "refresh_stats", "watcher_config", "config_warnings", "executor_config"):
                    if key in run_context:
                        run_debug[key] = run_context[key]
                if "executor_config" in run_context:
                    executor_config = run_context.get("executor_config") or {}
                    if isinstance(executor_config, dict):
                        run_debug["runtime_contract"] = {
                            "kind": executor_config.get("kind"),
                            "input_mode": executor_config.get("input_mode"),
                            "tool_policy": executor_config.get("tool_policy"),
                            "output_mode": executor_config.get("output_mode"),
                            "persistence_mode": executor_config.get("persistence_mode"),
                            "validation_mode": executor_config.get("validation_mode"),
                            "notify_mode": executor_config.get("notify_mode"),
                            "no_update_policy": executor_config.get("no_update_policy"),
                        }
            blocked = set(step_user_context.blocked_tools or [])
            blocked.update(task_profile.effective_blocked_tools(run_context=run_context))
            allowed_cap = list(step_user_context.allowed_tool_cap or [])
            profile_allowed = task_profile.effective_allowed_tools(run_context=run_context)
            if profile_allowed is not None:
                if allowed_cap:
                    allowed_cap = sorted(set(allowed_cap).intersection(profile_allowed))
                else:
                    allowed_cap = sorted(profile_allowed)
            step_user_context = replace(
                step_user_context,
                blocked_tools=sorted(blocked),
                allowed_tool_cap=allowed_cap,
            )
        skills_enabled = not task_profile or task_profile.allow_skill_injection(run_context=run_context)
        if not skills_enabled:
            step_user_context = replace(
                step_user_context,
                active_skill_slugs=[],
                skill_selection_mode="",
                skill_injection_details=[],
            )
            run_debug["active_skills"] = []
            run_debug["skill_selection_mode"] = ""

        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            start_index = task.current_step_index or 1
            rows = await db.execute(
                select(TaskStep)
                .where(TaskStep.task_id == task_id)
                .order_by(TaskStep.step_index)
            )
            steps = rows.scalars().all()

        for step in steps:
            if step.step_index < start_index or step.status == "succeeded":
                continue

            # Mark running and clear stale error state.
            async with AsyncSessionLocal() as db:
                task = await db.get(Task, task_id)
                step_row = await db.get(TaskStep, step.id)
                task.current_step_index = step_row.step_index
                step_row.status = "running"
                step_row.error = None
                step_row.waiting_approval_tool = None
                await db.commit()

            async with AsyncSessionLocal() as db:
                svc = get_memory_service()
                memories = await svc.retrieve_for_context(db, user_id, query=step.instruction)
                recalled_memory_ids.update(int(m.id) for m in memories)
                step_context = replace(step_user_context)
                if skills_enabled:
                    step_context = await hydrate_user_context(
                        db,
                        step_context,
                        query=step.instruction,
                    )
                skill_events = run_debug.setdefault("skill_injection_events", [])
                if isinstance(skill_events, list):
                    skill_events.append(
                        {
                            "stage": f"step_{step.step_index}",
                            "active_skills": list(step_context.active_skill_slugs or []),
                            "selection_mode": step_context.skill_selection_mode or "",
                            "details": list(step_context.skill_injection_details or []),
                        }
                    )
                active_skills = set(run_debug.get("active_skills") or [])
                active_skills.update(step_context.active_skill_slugs or [])
                run_debug["active_skills"] = sorted(active_skills)
                run_debug["skill_selection_mode"] = step_context.skill_selection_mode or run_debug.get("skill_selection_mode") or ""

            # Summaries from previous succeeded steps keep context compact.
            prior_summaries: list[str] = []
            prior_full_outputs: list[str] = []
            omitted_prior_outputs = 0
            truncated_prior_outputs = 0
            async with AsyncSessionLocal() as db:
                prev_rows = await db.execute(
                    select(TaskStep)
                    .where(
                        TaskStep.task_id == task_id,
                        TaskStep.step_index < step.step_index,
                        TaskStep.output_summary.isnot(None),
                    )
                    .order_by(TaskStep.step_index)
                )
                prev_steps = prev_rows.scalars().all()
                max_prior_summaries = max(1, int(settings.task_prompt_max_prior_summaries))
                summary_steps = prev_steps[-max_prior_summaries:]
                for prev in summary_steps:
                    prior_summaries.append(f"Step {prev.step_index}: {prev.output_summary}")
                max_prior_full_outputs = max(1, int(settings.task_final_synthesis_max_prior_outputs))
                output_steps = [prev for prev in prev_steps if prev.result][-max_prior_full_outputs:]
                omitted_prior_outputs = max(0, len([prev for prev in prev_steps if prev.result]) - len(output_steps))
                max_prior_output_tokens = max(50, int(settings.task_final_synthesis_max_prior_output_tokens))
                max_prior_output_chars = max(200, int(settings.task_final_synthesis_max_prior_output_chars))
                for prev in output_steps:
                    if prev.result:
                        truncated_output = _truncate_text_to_estimated_tokens(
                            prev.result,
                            max_tokens=max_prior_output_tokens,
                        )
                        if len(truncated_output) > max_prior_output_chars:
                            truncated_output = truncated_output[: max_prior_output_chars - 1].rstrip() + "…"
                        if truncated_output != prev.result:
                            truncated_prior_outputs += 1
                        prior_full_outputs.append(
                            f"Step {prev.step_index} full output:\n{truncated_output}"
                        )

            is_final_step = self._is_final_synthesis_step(step, steps)
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(run_context, dict) and isinstance(run_context.get("executor_config"), dict):
                latest_skill_event = {}
                skill_events = run_debug.get("skill_injection_events") or []
                if isinstance(skill_events, list) and skill_events:
                    latest = skill_events[-1]
                    if isinstance(latest, dict):
                        latest_skill_event = latest
                preserved_runtime_state = build_preserved_runtime_state(
                    executor_config=run_context.get("executor_config") or {},
                    step_index=step.step_index,
                    step_title=step.title,
                    step_instruction=step.instruction,
                    is_final_step=is_final_step,
                    dataset=run_context.get("dataset") if isinstance(run_context.get("dataset"), dict) else {},
                    prior_step_summaries=prior_summaries,
                    active_skill_slugs=list(latest_skill_event.get("active_skills") or run_debug.get("active_skills") or []),
                    skill_injection_details=list(latest_skill_event.get("details") or []),
                )
                run_context["preserved_runtime_state"] = preserved_runtime_state
                run_debug["preserved_runtime_state"] = preserved_runtime_state
            prompt_parts: list[str] = [f"[Task: {task_title}]", task_instruction]
            if preloaded_required_context:
                prompt_parts.insert(0, preloaded_required_context)
            prompt_parts.append(f"Current Step ({step.step_index}): {step.title}")
            prompt_parts.append(step.instruction)
            if task_profile:
                task_profile.augment_prompt(
                    prompt_parts=prompt_parts,
                    run_context=run_context,
                    is_final_step=is_final_step,
                )
            if prior_summaries:
                prompt_parts.append("Previous step summaries:\n" + "\n".join(prior_summaries))
            include_prior_full_outputs = not (
                isinstance(run_context, dict) and isinstance(run_context.get("executor_config"), dict)
            )
            if is_final_step and include_prior_full_outputs and prior_full_outputs:
                prompt_parts.append(
                    "Previous step outputs (use exact details for final synthesis):\n"
                    + "\n\n".join(prior_full_outputs)
                )
                if omitted_prior_outputs:
                    prompt_parts.append(
                        f"Older prior full outputs omitted for context budget: {omitted_prior_outputs}."
                    )
                if truncated_prior_outputs:
                    prompt_parts.append(
                        f"Included prior full outputs were truncated to an estimated token budget: {truncated_prior_outputs}."
                    )
                prompt_parts.append(
                    "If you include links in the final output, copy exact URLs from prior step outputs. "
                    "Do not invent, rewrite, or simplify URLs."
                )
            if memories:
                prompt_parts.insert(0, svc.format_for_prompt(memories))
            prompt_parts.append(f"Current time: {now_str}")

            arm_approval = (task_requires_approval or step.requires_approval) and not pre_approved
            stage = "task_final_synthesis" if is_final_step else "task_execution_step"
            primary_model = (
                model_profile.final_synthesis_model if is_final_step else model_profile.execution_model
            )
            token = _approval_armed.set(arm_approval)
            tool_record_token = reset_tool_execution_records()
            try:
                result = await self._run_step_with_model_policy(
                    prompt="\n\n".join(prompt_parts),
                    user_context=step_context,
                    task_id=task_id,
                    task_run_id=task_run_id,
                    primary_model=primary_model,
                    final_model=model_profile.final_synthesis_model,
                    stage=stage,
                    allow_large_fallback=(not is_final_step and model_profile.large_retry_enabled),
                    fallback_attempts=model_profile.large_retry_max_attempts,
                    count_small_metric=(not is_final_step),
                    count_final_large_metric=is_final_step,
                    repeated_error_counts=repeated_error_counts,
                    suppression_events=suppression_events,
                    run_debug=run_debug,
                )
                tool_records = get_tool_execution_records()
            except ApprovalRequired as exc:
                tool_records = get_tool_execution_records()
                restore_tool_execution_records(tool_record_token)
                _approval_armed.reset(token)
                async with AsyncSessionLocal() as db:
                    task = await db.get(Task, task_id)
                    step_row = await db.get(TaskStep, step.id)
                    task.status = "waiting_approval"
                    task.current_step_index = step_row.step_index
                    step_row.status = "waiting_approval"
                    step_row.waiting_approval_tool = str(exc)
                    await db.commit()
                raise
            except Exception as exc:
                tool_records = get_tool_execution_records()
                restore_tool_execution_records(tool_record_token)
                _approval_armed.reset(token)
                if (
                    getattr(task_profile, "name", "default") == "rss_newspaper"
                    and not is_final_step
                    and _is_suppressed_tool_failure_error(exc)
                ):
                    async with AsyncSessionLocal() as db:
                        task = await db.get(Task, task_id)
                        step_row = await db.get(TaskStep, step.id)
                        step_row.status = "skipped"
                        step_row.error = str(exc)
                        task.current_step_index = step.step_index + 1
                        await db.commit()
                    continue
                async with AsyncSessionLocal() as db:
                    task = await db.get(Task, task_id)
                    step_row = await db.get(TaskStep, step.id)
                    task.status = "failed"
                    step_row.status = "failed"
                    step_row.error = str(exc)
                    await db.commit()
                raise
            else:
                restore_tool_execution_records(tool_record_token)
                _approval_armed.reset(token)

            if task_profile:
                if isinstance(run_context, dict):
                    run_context["last_tool_records"] = tool_records
                cleaned, grounding_report = task_profile.validate_finalize(
                    result=result,
                    prior_full_outputs=prior_full_outputs,
                    run_context=run_context,
                    is_final_step=is_final_step,
                )
                result = cleaned
                if grounding_report is not None:
                    run_debug["grounding_report"] = grounding_report
                if grounding_report and grounding_report.get("fatal"):
                    message = str(grounding_report.get("fatal_reason") or "Ungrounded magazine output")
                    async with AsyncSessionLocal() as db:
                        task = await db.get(Task, task_id)
                        step_row = await db.get(TaskStep, step.id)
                        task.status = "failed"
                        step_row.status = "failed"
                        step_row.error = message
                        if task_run_id:
                            run = await db.get(TaskRun, task_run_id)
                            if run is not None:
                                run.status = "failed"
                                run.finished_at = datetime.now(timezone.utc)
                                run.error = message
                            await _persist_run_artifacts(
                                db,
                                task_run_id=task_run_id,
                                final_markdown=result,
                                run_debug=run_debug,
                            )
                        await db.commit()
                    raise RuntimeError(message)

            summary = result[:240] if result else ""
            final_result = result
            async with AsyncSessionLocal() as db:
                task = await db.get(Task, task_id)
                step_row = await db.get(TaskStep, step.id)
                step_row.status = "succeeded"
                step_row.result = result
                step_row.output_summary = summary
                task.current_step_index = step.step_index + 1
                await db.commit()

        # Final output should be the final synthesis step output, not joined
        # truncated summaries from intermediate steps.
        async with AsyncSessionLocal() as db:
            if not final_result:
                rows = await db.execute(
                    select(TaskStep)
                    .where(TaskStep.task_id == task_id, TaskStep.result.isnot(None))
                    .order_by(TaskStep.step_index.desc())
                    .limit(1)
                )
                last_with_result = rows.scalar_one_or_none()
                if last_with_result is not None:
                    final_result = last_with_result.result or ""
            task = await db.get(Task, task_id)
            task.current_step_index = None
            await db.commit()

        if grounding_report is not None:
            run_debug["grounding_report"] = grounding_report
        if recalled_memory_ids:
            svc = get_memory_service()
            await svc.mark_accessed(sorted(recalled_memory_ids), mode="task_materialized")
        return final_result, run_debug

    async def _run_step_with_model_policy(
        self,
        *,
        prompt: str,
        user_context,
        task_id: int,
        task_run_id: int | None,
        primary_model: str,
        final_model: str,
        stage: str,
        allow_large_fallback: bool,
        fallback_attempts: int,
        count_small_metric: bool,
        count_final_large_metric: bool,
        repeated_error_counts: dict[str, int],
        suppression_events: list[dict[str, str | int]],
        run_debug: dict[str, object],
    ) -> str:
        from app.agent.core import (
            get_agent_loop_diagnostics,
            reset_agent_loop_diagnostics,
            restore_agent_loop_diagnostics,
            run_agent,
        )

        attempts = 0
        max_attempts = max(0, fallback_attempts)
        used_fallback = False

        while True:
            using_fallback_model = attempts > 0
            model = final_model if using_fallback_model else primary_model
            if count_small_metric and not using_fallback_model:
                metrics.inc_task_model_execution_small_calls()
            if count_final_large_metric and not using_fallback_model:
                metrics.inc_task_model_final_large_calls()
            if using_fallback_model:
                metrics.inc_task_model_fallback_to_large_count()
                used_fallback = True

            try:
                agent_loop_diagnostics: dict[str, object] = {}
                usage_token = bind_llm_usage_context(
                    user_id=user_context.user_id,
                    session_id=getattr(user_context, "session_id", None),
                    task_id=task_id,
                    task_run_id=task_run_id,
                    source="task_runner",
                )
                diagnostics_token = reset_agent_loop_diagnostics()
                try:
                    try:
                        result = await run_agent(
                            [{"role": "user", "content": prompt}],
                            user_context,
                            mode="task",
                            model_override=model,
                            stage=stage,
                        )
                    finally:
                        agent_loop_diagnostics = get_agent_loop_diagnostics()
                        restore_agent_loop_diagnostics(diagnostics_token)
                finally:
                    reset_llm_usage_context(usage_token)
                _append_agent_context_budgeting(
                    run_debug,
                    stage=stage,
                    model=model,
                    diagnostics=agent_loop_diagnostics,
                )
                if not (result or "").strip():
                    raise ValueError("Empty model output")
                if used_fallback:
                    metrics.inc_task_model_fallback_success_count()
                return result
            except ApprovalRequired:
                raise
            except Exception as exc:
                _append_agent_context_budgeting(
                    run_debug,
                    stage=stage,
                    model=model,
                    diagnostics=agent_loop_diagnostics,
                )
                signature = _build_tool_error_signature(exc, stage=stage, model=model)
                if signature:
                    count = repeated_error_counts.get(signature, 0) + 1
                    repeated_error_counts[signature] = count
                    if count >= REPEATED_TOOL_ERROR_THRESHOLD:
                        suppression_events.append(
                            {
                                "signature": signature,
                                "count": count,
                                "stage": stage,
                            }
                        )
                        raise RuntimeError(
                            f"Suppressed repeated tool failure: {signature}"
                        ) from exc
                if allow_large_fallback and attempts < max_attempts:
                    attempts += 1
                    continue
                if used_fallback:
                    metrics.inc_task_model_fallback_failure_count()
                raise

    @staticmethod
    def _is_final_synthesis_step(step: TaskStep, all_steps: list[TaskStep]) -> bool:
        if not all_steps:
            return True
        if step.step_index == max(s.step_index for s in all_steps):
            return True
        text = f"{step.title} {step.instruction}".lower()
        markers = ("final", "synthesis", "summarize", "summary", "final output")
        return any(marker in text for marker in markers)

    async def _reset_steps_for_new_run(
        self,
        db,
        task_id: int,
        *,
        run_debug: Optional[dict[str, object]] = None,
    ) -> str:
        """Recurring tasks start from a clean step state each run."""
        from sqlalchemy import select

        rows = await db.execute(
            select(TaskStep).where(TaskStep.task_id == task_id)
        )
        steps = rows.scalars().all()
        if not steps:
            return ""

        snapshot_lines = ["Step snapshot from previous run:"]
        for step in steps:
            snapshot_lines.append(f"- Step {step.step_index}: {step.title} [{step.status}]")
            if step.output_summary:
                snapshot_lines.append(f"  Summary: {step.output_summary}")
            if step.result:
                trimmed = (step.result[:420] + "...") if len(step.result) > 420 else step.result
                snapshot_lines.append(f"  Output: {trimmed}")
            if step.error:
                snapshot_lines.append(f"  Error: {step.error}")
        diagnostics = _format_run_diagnostics(run_debug or {})
        if diagnostics:
            snapshot_lines.append("")
            snapshot_lines.append(diagnostics)
        snapshot = "\n".join(snapshot_lines)

        for step in steps:
            step.status = "pending"
            step.output_summary = None
            step.result = None
            step.error = None
            step.waiting_approval_tool = None

        task = await db.get(Task, task_id)
        task.current_step_index = 1
        return snapshot

    # ------------------------------------------------------------------
    # Error handling with exponential backoff
    # ------------------------------------------------------------------

    async def _handle_cancelled(self, task_id: int, task_run_id: Optional[int] = None) -> None:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is not None:
                task.status = "cancelled"
                task.last_run_at = datetime.now(timezone.utc)
                task.next_run_at = None
                task.next_retry_at = None
                task.error = "Stopped by user"

            if task_run_id:
                run = await db.get(TaskRun, task_run_id)
                if run is not None:
                    run.status = "cancelled"
                    run.finished_at = datetime.now(timezone.utc)
                    run.error = "Stopped by user"
            await db.commit()

        log.info("task.cancelled", task_id=task_id)

    async def _handle_error(self, task_id: int, exc: Exception, task_run_id: Optional[int] = None) -> None:
        from app.db.session import AsyncSessionLocal

        log.error("task.failed", task_id=task_id, error=str(exc), exc_info=True)

        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            retry = task.retry_count or 0
            unavailable, reason = _classify_llm_unavailable_error(exc)
            if unavailable:
                cooldown = max(30, int(settings.scheduler_unhealthy_cooldown_seconds or 300))
                next_time = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
                task.status = "pending"
                task.error = f"paused_unavailable: {reason}"
                task.next_retry_at = next_time
                task.next_run_at = next_time
                if task_run_id:
                    run = await db.get(TaskRun, task_run_id)
                    if run is not None:
                        run.status = "cancelled"
                        run.finished_at = datetime.now(timezone.utc)
                        run.error = f"paused_unavailable: {reason}"
                await db.commit()
                log.warning(
                    "task.paused_unavailable",
                    task_id=task_id,
                    cooldown_seconds=cooldown,
                    reason=reason,
                )
                return

            if retry < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[retry]
                task.status = "pending"
                task.retry_count = retry + 1
                task.error = str(exc)
                task.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                task.next_run_at = task.next_retry_at
                log.info(
                    "task.scheduled_retry",
                    task_id=task_id,
                    attempt=retry + 1,
                    retry_in_seconds=delay,
                )
            else:
                task.status = "failed"
                task.error = str(exc)
                task.last_run_at = datetime.now(timezone.utc)
                log.warning("task.exhausted_retries", task_id=task_id)

            if task_run_id:
                run = await db.get(TaskRun, task_run_id)
                if run is not None:
                    run.status = "failed"
                    run.finished_at = datetime.now(timezone.utc)
                    run.error = str(exc)

            await db.commit()

    async def _pause_for_llm_unavailable(
        self,
        *,
        task_id: int,
        cooldown_seconds: int,
        reason: str,
    ) -> None:
        from app.db.session import AsyncSessionLocal

        now = datetime.now(timezone.utc)
        next_time = now + timedelta(seconds=max(30, int(cooldown_seconds)))
        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return
            task.status = "pending"
            task.error = f"paused_unavailable: {reason}"
            task.next_retry_at = next_time
            task.next_run_at = next_time
            await db.commit()
        log.warning(
            "task.preflight_paused_unavailable",
            task_id=task_id,
            cooldown_seconds=cooldown_seconds,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Push delivery — Sprint 4.3
    # ------------------------------------------------------------------

    async def _push(self, task_id: int, result: str) -> None:
        """Send the task result to all registered device tokens for the task owner."""
        from app.db.session import AsyncSessionLocal
        from app.db.models import DeviceToken, Task
        from app.autonomy.push import get_apns_pusher
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return
            token_result = await db.execute(
                select(DeviceToken).where(DeviceToken.user_id == task.user_id)
            )
            tokens = token_result.scalars().all()
            task_title = task.title

        if not tokens:
            log.info("task.push_no_tokens", task_id=task_id)
            return

        pusher = get_apns_pusher()
        # Truncate body to stay well under APNs' 4 KB payload limit
        body = (result[:250] + "…") if len(result) > 250 else result
        for device in tokens:
            await pusher.send(
                device_token=device.token,
                environment=device.environment,
                title=task_title,
                body=body,
            )


# Module-level singleton
_runner: Optional[TaskRunner] = None


def get_task_runner() -> TaskRunner:
    global _runner
    if _runner is None:
        _runner = TaskRunner()
    return _runner


def _format_result_for_inbox(result: str) -> str:
    """
    Improve readability when the model returns one very long line.
    Preserve already formatted markdown/newline-heavy outputs.
    """
    text = (result or "").strip()
    if not text:
        return text
    if "\n" in text:
        return text

    # Keep obvious list-style outputs untouched.
    if re.search(r"\b\d+\.\s", text):
        return text

    # Split long single-line prose into paragraphs at sentence boundaries.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 2:
        return text
    return "\n\n".join(parts)


def _build_tool_error_signature(exc: Exception, *, stage: str, model: str) -> str:
    text = str(exc or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return f"{stage}|{model}|{normalized[:240]}"


def _is_suppressed_tool_failure_error(exc: Exception) -> bool:
    return "Suppressed repeated tool failure:" in str(exc)


def _classify_llm_unavailable_error(exc: Exception) -> tuple[bool, str]:
    text = str(exc or "").strip()
    lowered = text.lower()
    indicators = (
        "ollama_chatexception",
        "apiconnectionerror",
        "connection timed out",
        "timeout passed=",
        "connection refused",
        "failed to establish a new connection",
        "name or service not known",
        "temporarily unavailable",
    )
    if any(marker in lowered for marker in indicators):
        return True, text or "llm_unavailable"
    return False, ""


def _task_recipe_family(task: Task | None) -> str:
    if task is None:
        return ""
    recipe = task.task_recipe if isinstance(task.task_recipe, dict) else {}
    return str(recipe.get("family") or "").strip().lower()


def _is_agent_recipe_task(task: Task | None) -> bool:
    return _task_recipe_family(task) == "agent"


async def _preflight_llm_dispatch() -> tuple[int, str] | None:
    """
    Return (cooldown_seconds, reason) when local LLM is currently unavailable.
    """
    if settings.llm_backend not in ("ollama", "openai_compat"):
        return None

    from app.autonomy.scheduler import check_and_mark_llm_dispatch_health

    allowed, cooldown, reason = await check_and_mark_llm_dispatch_health(probe=False)
    if not allowed:
        return cooldown, reason
    return None


def _format_run_diagnostics(run_debug: dict[str, object]) -> str:
    if not run_debug:
        return ""

    lines: list[str] = ["Run diagnostics:"]
    suppressions = run_debug.get("tool_failure_suppressions") or []
    if isinstance(suppressions, list) and suppressions:
        lines.append(f"- Suppressed repeated failures: {len(suppressions)}")
        for item in suppressions[:5]:
            if isinstance(item, dict):
                lines.append(f"  - {item.get('signature', 'unknown')} (count={item.get('count', '?')})")

    grounding = run_debug.get("grounding_report")
    if isinstance(grounding, dict):
        lines.append(
            "- Grounding: "
            f"urls={grounding.get('detected_urls', 0)}, "
            f"invalid={len(grounding.get('invalid_urls') or [])}, "
            f"placeholders={grounding.get('placeholder_hits', 0)}, "
            f"fatal={grounding.get('fatal', False)}"
        )
    context_budgeting = run_debug.get("agent_context_budgeting") or []
    if isinstance(context_budgeting, list) and context_budgeting:
        tool_results_compacted = 0
        compaction_boundaries = 0
        overflow_retries = 0
        loop_events = 0
        max_before = 0
        max_after = 0
        for item in context_budgeting:
            if not isinstance(item, dict):
                continue
            tool_results_compacted += int(item.get("tool_results_compacted") or 0)
            compaction_boundaries += int(item.get("compaction_boundaries") or 0)
            overflow_retries += int(item.get("overflow_retries") or 0)
            loop_events += int(item.get("loop_events_count") or 0)
            max_before = max(max_before, int(item.get("max_estimated_tokens_before") or 0))
            max_after = max(max_after, int(item.get("max_estimated_tokens_after") or 0))
        lines.append(
            "- Context budgeting: "
            f"tool_results_compacted={tool_results_compacted}, "
            f"compaction_boundaries={compaction_boundaries}, "
            f"overflow_retries={overflow_retries}, "
            f"loop_events={loop_events}, "
            f"max_estimated_tokens_before={max_before}, "
            f"max_estimated_tokens_after={max_after}"
        )
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _append_agent_context_budgeting(
    run_debug: dict[str, object],
    *,
    stage: str,
    model: str,
    diagnostics: dict[str, object],
) -> None:
    if not diagnostics:
        return
    items = run_debug.setdefault("agent_context_budgeting", [])
    if not isinstance(items, list):
        items = []
        run_debug["agent_context_budgeting"] = items
    item = {
        "stage": stage,
        "model": model,
        "tool_results_compacted": int(diagnostics.get("tool_results_compacted") or 0),
        "compaction_boundaries": int(diagnostics.get("compaction_boundaries") or 0),
        "overflow_retries": int(diagnostics.get("overflow_retries") or 0),
        "overflow_retry_succeeded": bool(diagnostics.get("overflow_retry_succeeded") or False),
        "loop_events_count": len(diagnostics.get("loop_events") or []),
        "max_estimated_tokens_before": int(diagnostics.get("max_estimated_tokens_before") or 0),
        "max_estimated_tokens_after": int(diagnostics.get("max_estimated_tokens_after") or 0),
        "budget_events": diagnostics.get("budget_events") or [],
        "loop_events": diagnostics.get("loop_events") or [],
    }
    items.append(item)


async def _persist_run_artifacts(
    db,
    *,
    task_run_id: int,
    final_markdown: str,
    run_debug: dict[str, object],
) -> None:
    run = await db.get(TaskRun, task_run_id)
    task = await db.get(Task, run.task_id) if run is not None else None
    if run is None or task is None:
        return

    profile = resolve_task_execution_contract(task)
    await profile.persist_run_records(
        db=db,
        task=task,
        run=run,
        final_markdown=final_markdown,
        run_debug=run_debug,
    )
    payloads = profile.artifact_payloads(final_markdown=final_markdown, run_debug=run_debug)
    payloads.extend(
        await profile.export_artifact_payloads(
            task=task,
            run=run,
            final_markdown=final_markdown,
            run_debug=run_debug,
        )
    )

    # Replace previous artifacts for this run/type to keep data deterministic.
    existing_rows = (
        await db.execute(
            select(TaskRunArtifact).where(TaskRunArtifact.task_run_id == task_run_id)
        )
    ).scalars().all()
    for row in existing_rows:
        await db.delete(row)

    for payload in payloads:
        content_json = payload.get("content_json")
        if isinstance(content_json, dict):
            content_json = json.dumps(content_json, ensure_ascii=True, sort_keys=True)
        elif content_json is not None and not isinstance(content_json, str):
            content_json = json.dumps(content_json, ensure_ascii=True, sort_keys=True)
        db.add(
            TaskRunArtifact(
                task_run_id=task_run_id,
                artifact_type=str(payload.get("artifact_type") or "run_diagnostics"),
                content_json=content_json,
                content_text=payload.get("content_text"),
            )
        )
