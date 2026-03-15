from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models import Task, TaskRun, User
from app.autonomy import scheduler as sched
from tests.conftest import TestSessionLocal


def _make_user() -> User:
    return User(
        username="sched_user",
        email="sched_user@test.local",
        hashed_password="x",
        role="admin",
    )


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@pytest.mark.asyncio
async def test_tick_unhealthy_skips_recurring_backlog_and_uses_cooldown(monkeypatch):
    now = datetime.now(timezone.utc)

    async with TestSessionLocal() as db:
        user = _make_user()
        db.add(user)
        await db.flush()

        recurring = Task(
            user_id=user.id,
            title="Recurring",
            instruction="refresh",
            task_type="recurring",
            status="pending",
            schedule="every:1h",
            next_run_at=now - timedelta(minutes=2),
        )
        one_shot = Task(
            user_id=user.id,
            title="One-shot",
            instruction="once",
            task_type="one_shot",
            status="pending",
            next_run_at=now - timedelta(minutes=2),
        )
        db.add_all([recurring, one_shot])
        await db.commit()
        one_shot_due = one_shot.next_run_at

    calls = {"health": 0, "runs": 0}

    async def _down() -> bool:
        calls["health"] += 1
        return False

    class _Runner:
        async def execute(self, task):
            calls["runs"] += 1

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr("app.autonomy.runner.get_task_runner", lambda: _Runner())
    monkeypatch.setattr(sched, "_is_llm_available", _down)
    monkeypatch.setattr(settings, "scheduler_llm_health_gate_enabled", True, raising=False)
    monkeypatch.setattr(settings, "scheduler_unhealthy_cooldown_seconds", 300, raising=False)
    sched._llm_unhealthy_until = None

    await sched.tick()
    await sched.tick()  # within cooldown window, should not re-probe

    assert calls["health"] == 1
    assert calls["runs"] == 0

    async with TestSessionLocal() as db:
        rows = await db.execute(select(Task).order_by(Task.id))
        tasks = rows.scalars().all()
        recurring_row = next(t for t in tasks if t.task_type == "recurring")
        one_shot_row = next(t for t in tasks if t.task_type == "one_shot")
        assert recurring_row.next_run_at is not None
        assert _as_aware(recurring_row.next_run_at) > now
        assert _as_aware(one_shot_row.next_run_at) == _as_aware(one_shot_due)


@pytest.mark.asyncio
async def test_tick_healthy_dispatches_due_tasks(monkeypatch):
    now = datetime.now(timezone.utc)

    async with TestSessionLocal() as db:
        user = _make_user()
        user.username = "sched_user2"
        user.email = "sched_user2@test.local"
        db.add(user)
        await db.flush()
        db.add_all(
            [
                Task(
                    user_id=user.id,
                    title="T1",
                    instruction="a",
                    task_type="recurring",
                    status="pending",
                    schedule="every:1h",
                    next_run_at=now - timedelta(minutes=1),
                ),
                Task(
                    user_id=user.id,
                    title="T2",
                    instruction="b",
                    task_type="one_shot",
                    status="pending",
                    next_run_at=now - timedelta(minutes=1),
                ),
            ]
        )
        await db.commit()

    ran: list[int] = []

    async def _up() -> bool:
        return True

    class _Runner:
        async def execute(self, task):
            ran.append(task.id)

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr("app.autonomy.runner.get_task_runner", lambda: _Runner())
    monkeypatch.setattr(sched, "_is_llm_available", _up)
    monkeypatch.setattr(settings, "scheduler_llm_health_gate_enabled", True, raising=False)
    sched._llm_unhealthy_until = None

    await sched.tick()
    await asyncio_sleep()

    assert len(ran) == 2


@pytest.mark.asyncio
async def test_recover_stale_running_tasks_requeues_and_closes_run(monkeypatch):
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=45)

    async with TestSessionLocal() as db:
        user = _make_user()
        user.username = "sched_user3"
        user.email = "sched_user3@test.local"
        db.add(user)
        await db.flush()

        task = Task(
            user_id=user.id,
            title="Stale",
            instruction="x",
            task_type="recurring",
            status="running",
            schedule="every:1h",
            last_run_at=old,
        )
        db.add(task)
        await db.flush()
        run = TaskRun(
            task_id=task.id,
            status="running",
            started_at=old,
        )
        db.add(run)
        await db.commit()

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr(settings, "scheduler_stale_running_recovery_minutes", 20, raising=False)

    recovered = await sched.recover_stale_running_tasks()
    assert recovered == 1

    async with TestSessionLocal() as db:
        row = await db.get(Task, task.id)
        run_row = await db.get(TaskRun, run.id)
        assert row.status == "pending"
        assert row.next_run_at is not None
        assert run_row.status == "failed"
        assert run_row.error == "recovered_after_restart_or_sleep"


async def asyncio_sleep() -> None:
    import asyncio

    await asyncio.sleep(0)
