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
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from app.autonomy.approval import ApprovalRequired, _approval_armed
from app.autonomy.scheduler import compute_next_run_at
from app.db.models import Task, TaskRun, TaskStep

log = structlog.get_logger(__name__)

# Exponential retry delays (seconds) for transient errors
RETRY_DELAYS = [30, 60, 300, 900, 3600]

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

        current = asyncio.current_task()
        async with self._active_lock:
            if current is not None:
                self._active_runs[task_id] = current

        try:
            async with AsyncSessionLocal() as db:
                task = await db.get(Task, task_id)
                if task is None:
                    log.warning("task.not_found", task_id=task_id)
                    return
                if task.status not in ("pending",):
                    log.info("task.skip_non_pending", task_id=task_id, status=task.status)
                    return
                task_title = task.title
                task_deliver = task.deliver
                task.status = "running"
                run = TaskRun(task_id=task.id, status="running")
                db.add(run)
                await db.flush()
                task_run_id = run.id
                await db.commit()

            log.info("task.started", task_id=task_id, title=task_title)

            result = await self._execute_agent(task_id, task_run_id=task_run_id)

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
                    recurring_snapshot = await self._reset_steps_for_new_run(db, task.id)
                    task.next_run_at = compute_next_run_at(task.schedule)
                    task.status = "pending"   # re-queue for next run

                if task_run_id:
                    run = await db.get(TaskRun, task_run_id)
                    if run is not None:
                        run.status = "completed"
                        run.finished_at = datetime.now(timezone.utc)
                        run.error = None
                        if recurring_snapshot:
                            merged = f"{(result or '').strip()}\n\n{recurring_snapshot}".strip()
                            run.summary = merged[:4000]
                        else:
                            run.summary = (result or "")[:1000]

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

    async def _execute_agent(self, task_id: int, task_run_id: Optional[int] = None) -> str:
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
                    max_steps=6,
                    notes=task.instruction or "",
                    style="concise",
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
            return await run_agent([{"role": "user", "content": "\n\n".join(parts)}], user_context, mode="task")
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
    ) -> str:
        """Execute task steps sequentially from current_step_index."""
        from sqlalchemy import select
        from app.db.session import AsyncSessionLocal
        from app.agent.core import run_agent
        from app.memory.service import get_memory_service

        final_result = ""

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
                for prev in prev_rows.scalars().all():
                    prior_summaries.append(f"Step {prev.step_index}: {prev.output_summary}")

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            prompt_parts: list[str] = [f"[Task: {task_title}]", task_instruction]
            prompt_parts.append(f"Current Step ({step.step_index}): {step.title}")
            prompt_parts.append(step.instruction)
            if prior_summaries:
                prompt_parts.append("Previous step summaries:\n" + "\n".join(prior_summaries))
            if memories:
                prompt_parts.insert(0, svc.format_for_prompt(memories))
            prompt_parts.append(f"Current time: {now_str}")

            arm_approval = (task_requires_approval or step.requires_approval) and not pre_approved
            token = _approval_armed.set(arm_approval)
            try:
                result = await run_agent(
                    [{"role": "user", "content": "\n\n".join(prompt_parts)}],
                    user_context,
                    mode="task",
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

        return final_result

    async def _reset_steps_for_new_run(self, db, task_id: int) -> str:
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
            elif step.result:
                trimmed = (step.result[:220] + "...") if len(step.result) > 220 else step.result
                snapshot_lines.append(f"  Result: {trimmed}")
            if step.error:
                snapshot_lines.append(f"  Error: {step.error}")
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
