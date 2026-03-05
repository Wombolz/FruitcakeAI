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
from app.db.models import Task

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

    async def execute(self, task: Task) -> None:
        async with _semaphore:
            await self._run(task.id)

    # ------------------------------------------------------------------
    # Core execution pipeline
    # ------------------------------------------------------------------

    async def _run(self, task_id: int) -> None:
        from app.db.session import AsyncSessionLocal

        # Phase 1: Re-fetch and mark running
        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is None:
                log.warning("task.not_found", task_id=task_id)
                return
            if task.status not in ("pending",):
                log.info("task.skip_non_pending", task_id=task_id, status=task.status)
                return
            task.status = "running"
            await db.commit()

        log.info("task.started", task_id=task_id, title=task.title)

        try:
            result = await self._execute_agent(task_id)

            # Phase 6a: Success — update task record
            async with AsyncSessionLocal() as db:
                from app.db.models import ChatMessage
                task = await db.get(Task, task_id)
                task.status = "completed"
                task.result = result
                task.error = None
                task.last_run_at = datetime.now(timezone.utc)
                task.retry_count = 0

                if task.task_type == "recurring" and task.schedule:
                    task.next_run_at = compute_next_run_at(task.schedule)
                    task.status = "pending"   # re-queue for next run

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
            if task.deliver and result:
                await self._push(task_id, result)

        except ApprovalRequired as exc:
            # The approval gate was triggered — persist waiting_approval state
            async with AsyncSessionLocal() as db:
                task = await db.get(Task, task_id)
                task.status = "waiting_approval"
                task.last_run_at = datetime.now(timezone.utc)
                await db.commit()
            log.info("task.waiting_approval", task_id=task_id, blocked_tool=str(exc))

        except Exception as exc:
            await self._handle_error(task_id, exc)

    # ------------------------------------------------------------------
    # Agent execution in isolated session
    # ------------------------------------------------------------------

    async def _execute_agent(self, task_id: int) -> str:
        from app.db.session import AsyncSessionLocal
        from app.db.models import ChatSession, User
        from app.agent.context import UserContext
        from app.agent.core import run_agent
        from app.memory.service import get_memory_service

        # Load task + user
        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
            user = await db.get(User, task.user_id)
            if user is None:
                raise ValueError(f"User {task.user_id} not found for task {task_id}")

            # Check pre-approval flag (set by PATCH /tasks/{id} {"approved": true})
            pre_approved = task.error == "__pre_approved__"
            if pre_approved:
                task.error = None  # clear the sentinel
                await db.commit()

            # Create isolated task session (excluded from chat UI session list)
            session = ChatSession(
                user_id=task.user_id,
                title=f"[Task] {task.title}",
                persona=user.persona or "family_assistant",
                is_task_session=True,
            )
            db.add(session)
            await db.flush()
            session_id = session.id

            # Link this session back to the task for /admin/task-runs join
            task.last_session_id = session_id
            await db.commit()

        # Build UserContext
        from app.agent.context import UserContext
        from app.agent.persona_loader import get_persona

        persona_name = user.persona or "family_assistant"
        user_context = UserContext.from_user(user, persona_name=persona_name)
        user_context.session_id = session_id

        # Retrieve memories for this user
        async with AsyncSessionLocal() as db:
            svc = get_memory_service()
            memories = await svc.retrieve_for_context(
                db, task.user_id, query=task.instruction
            )

        # Compose prompt: memory context → task header → instruction → timestamp
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts: list[str] = []

        if memories:
            parts.append(svc.format_for_prompt(memories))

        parts.append(f"[Task: {task.title}]")
        parts.append(task.instruction)
        parts.append(f"\nCurrent time: {now_str}")

        messages = [{"role": "user", "content": "\n\n".join(parts)}]

        # Arm the approval gate if this task requires it (and isn't pre-approved)
        arm_approval = task.requires_approval and not pre_approved
        token = _approval_armed.set(arm_approval)

        try:
            result = await run_agent(messages, user_context, mode="task")
        finally:
            _approval_armed.reset(token)  # always disarm, even on exception

        return result

    # ------------------------------------------------------------------
    # Error handling with exponential backoff
    # ------------------------------------------------------------------

    async def _handle_error(self, task_id: int, exc: Exception) -> None:
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
