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
from typing import Optional

import structlog
from sqlalchemy import select, update

from app.autonomy.approval import ApprovalRequired, _approval_armed
from app.autonomy.model_routing import TaskModelProfile, resolve_task_model_profile
from app.autonomy.profiles import resolve_task_profile, resolve_task_profile_by_name
from app.autonomy.scheduler import compute_next_run_at
from app.config import settings
from app.db.models import Task, TaskRun, TaskRunArtifact, TaskStep
from app.metrics import metrics

log = structlog.get_logger(__name__)

# Exponential retry delays (seconds) for transient errors
RETRY_DELAYS = [30, 60, 300, 900, 3600]
REPEATED_TOOL_ERROR_THRESHOLD = 2

# Limit concurrent task-mode agent loops
_semaphore = asyncio.Semaphore(2)


class TaskRunner:
    """
    Runs a Task in an isolated agent session.

    Call execute(task) from the scheduler tick or from the manual-trigger
    API endpoint. The semaphore ensures at most 2 tasks run concurrently.
    """

    def __init__(self) -> None:
        self._active_runs: dict[int, asyncio.Task] = {}
        self._active_lock = asyncio.Lock()

    async def execute(self, task: Task) -> None:
        async with _semaphore:
            await self._run(task.id)

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

    async def _run(self, task_id: int) -> None:
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
                    .where(Task.id == task_id, Task.status == "pending")
                    .values(
                        status="running",
                        error=None,
                        next_retry_at=None,
                    )
                )
                if (claim.rowcount or 0) == 0:
                    task = await db.get(Task, task_id)
                    status = task.status if task is not None else "missing"
                    log.info("task.skip_non_pending", task_id=task_id, status=status)
                    await db.rollback()
                    return

                task = await db.get(Task, task_id)
                if task is None:
                    log.warning("task.not_found", task_id=task_id)
                    await db.rollback()
                    return
                task_title = task.title
                task_deliver = task.deliver
                run = TaskRun(task_id=task.id, status="running")
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
                    task.next_run_at = compute_next_run_at(task.schedule)
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
            if task_deliver and result:
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
        session_id: Optional[int] = None
        task_user_id: Optional[int] = None
        task_instruction: Optional[str] = None
        task_title: Optional[str] = None
        task_requires_approval: bool = False
        has_plan = False
        pre_approved = False
        model_profile: TaskModelProfile | None = None
        task_profile = None

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
            task_profile = resolve_task_profile(task, user)
            persona_name = resolved.persona
            task_user_id = task.user_id
            task_instruction = task.instruction
            task_title = task.title
            task_requires_approval = task.requires_approval
            has_plan = bool(task.has_plan)
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

        async with AsyncSessionLocal() as db:
            user = await db.get(User, task_user_id)
            if user is None:
                raise ValueError(f"User {task_user_id} not found for task {task_id}")
            user_context = UserContext.from_user(user, persona_name=persona_name)
        user_context.session_id = session_id

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
                    )

        # Back-compat mode: single instruction task behavior.
        async with AsyncSessionLocal() as db:
            svc = get_memory_service()
            memories = await svc.retrieve_for_context(db, task_user_id, query=task_instruction)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts: list[str] = []
        if memories:
            parts.append(svc.format_for_prompt(memories))
        parts.append(f"[Task: {task_title}]")
        parts.append(task_instruction or "")
        parts.append(f"\nCurrent time: {now_str}")

        arm_approval = task_requires_approval and not pre_approved
        token = _approval_armed.set(arm_approval)
        try:
            result = await run_agent(
                [{"role": "user", "content": "\n\n".join(parts)}],
                user_context,
                mode="task",
                model_override=model_profile.final_synthesis_model if model_profile else None,
                stage="task_single_stage",
            )
            return result, {}
        finally:
            _approval_armed.reset(token)

    @staticmethod
    def _should_auto_plan(task: Task) -> bool:
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
    ) -> tuple[str, dict[str, object]]:
        """Execute task steps sequentially from current_step_index."""
        from sqlalchemy import select
        from app.db.session import AsyncSessionLocal
        from app.agent.core import run_agent
        from app.memory.service import get_memory_service

        final_result = ""
        repeated_error_counts: dict[str, int] = {}
        suppression_events: list[dict[str, str | int]] = []
        step_user_context = user_context
        run_context: dict[str, object] = {}
        grounding_report: dict[str, object] | None = None
        run_debug: dict[str, object] = {
            "profile": getattr(task_profile, "name", "default"),
            "tool_failure_suppressions": suppression_events,
        }

        if task_profile and task_run_id:
            async with AsyncSessionLocal() as db:
                run_context = await task_profile.prepare_run_context(
                    db=db,
                    user_id=user_id,
                    task_run_id=task_run_id,
                )
                await db.commit()
            if isinstance(run_context, dict):
                for key in ("dataset", "dataset_stats", "refresh_stats"):
                    if key in run_context:
                        run_debug[key] = run_context[key]
            blocked = set(step_user_context.blocked_tools or [])
            blocked.update(task_profile.effective_blocked_tools(run_context=run_context))
            step_user_context = replace(step_user_context, blocked_tools=sorted(blocked))

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

            # Summaries from previous succeeded steps keep context compact.
            prior_summaries: list[str] = []
            prior_full_outputs: list[str] = []
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
                for prev in prev_steps:
                    prior_summaries.append(f"Step {prev.step_index}: {prev.output_summary}")
                    if prev.result:
                        prior_full_outputs.append(
                            f"Step {prev.step_index} full output:\n{prev.result[:2200]}"
                        )

            is_final_step = self._is_final_synthesis_step(step, steps)
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            prompt_parts: list[str] = [f"[Task: {task_title}]", task_instruction]
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
            if is_final_step and prior_full_outputs:
                prompt_parts.append(
                    "Previous step outputs (use exact details for final synthesis):\n"
                    + "\n\n".join(prior_full_outputs)
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
            try:
                result = await self._run_step_with_model_policy(
                    prompt="\n\n".join(prompt_parts),
                    user_context=step_user_context,
                    primary_model=primary_model,
                    final_model=model_profile.final_synthesis_model,
                    stage=stage,
                    allow_large_fallback=(not is_final_step and model_profile.large_retry_enabled),
                    fallback_attempts=model_profile.large_retry_max_attempts,
                    count_small_metric=(not is_final_step),
                    count_final_large_metric=is_final_step,
                    repeated_error_counts=repeated_error_counts,
                    suppression_events=suppression_events,
                )
            except ApprovalRequired as exc:
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
                _approval_armed.reset(token)
                if (
                    getattr(task_profile, "name", "default") == "news_magazine"
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
                _approval_armed.reset(token)

            if task_profile:
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
        return final_result, run_debug

    async def _run_step_with_model_policy(
        self,
        *,
        prompt: str,
        user_context,
        primary_model: str,
        final_model: str,
        stage: str,
        allow_large_fallback: bool,
        fallback_attempts: int,
        count_small_metric: bool,
        count_final_large_metric: bool,
        repeated_error_counts: dict[str, int],
        suppression_events: list[dict[str, str | int]],
    ) -> str:
        from app.agent.core import run_agent

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
                result = await run_agent(
                    [{"role": "user", "content": prompt}],
                    user_context,
                    mode="task",
                    model_override=model,
                    stage=stage,
                )
                if not (result or "").strip():
                    raise ValueError("Empty model output")
                if used_fallback:
                    metrics.inc_task_model_fallback_success_count()
                return result
            except ApprovalRequired:
                raise
            except Exception as exc:
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
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


async def _persist_run_artifacts(
    db,
    *,
    task_run_id: int,
    final_markdown: str,
    run_debug: dict[str, object],
) -> None:
    profile = resolve_task_profile_by_name(str(run_debug.get("profile") or "default"))
    payloads = profile.artifact_payloads(final_markdown=final_markdown, run_debug=run_debug)

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
