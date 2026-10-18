import asyncio
from dataclasses import replace
from importlib import import_module
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect

from app.coding_runtime.contracts import CodingRuntimeEvent
from app.coding_runtime.run_deadline import RunDeadline
from app.coding_runtime.service import CodingServiceError
from app.db.models import CodingApproval, CodingRun, CodingSession, CodingWorkspace
from test_api import api_harness
from test_changes import EditRuntime, enable
from test_commands import CommandRuntime, command_setup
from test_service import FakeRuntime, finished, harness, wait_for_status


class WaitingRuntime(FakeRuntime):
    async def run(self, prompt):
        yield CodingRuntimeEvent("interaction.requested", {
            "id": "private-runtime-request", "method": "confirm", "title": "Allow read?",
            "message": json.dumps({"operation_id": "read-1"}),
        })
        await self.confirmation.wait()
        if self.mode == "blocked_after_approval":
            await self.release.wait()
        yield CodingRuntimeEvent("operation.completed", {"operation_id": "read-1", "tool": "read"})
        yield CodingRuntimeEvent("run.completed", {"aborted": self.aborted})


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny", "stop", "shutdown", "revoked"])
async def test_human_wait_exceeds_run_budget_without_expiry(tmp_path, decision):
    async with harness(tmp_path, run_seconds=0.5) as (service, sessions, factory, workspace, session):
        runtime = WaitingRuntime()
        service._factory = lambda: runtime
        run = await service.submit(user_id=2, session_id=session.id, message="Ask first")
        await wait_for_status(service, run.id, "waiting_approval")
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        assert approval.expires_at is None
        await asyncio.sleep(0.7)
        assert (await service.get_run(user_id=2, run_id=run.id)).status == "waiting_approval"
        if decision == "shutdown":
            await service.shutdown()
        elif decision == "stop":
            await service.stop(user_id=2, run_id=run.id)
        elif decision == "revoked":
            async with sessions() as db, db.begin():
                (await db.get(CodingWorkspace, workspace.id)).is_active = False
            with pytest.raises(CodingServiceError, match="authorized"):
                await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True)
            await service.stop(user_id=2, run_id=run.id)
        else:
            await service.resolve_approval(user_id=2, approval_id=approval.id, approved=decision == "allow")
        result, _ = await finished(service, run)
        assert result.status == ("interrupted" if decision == "shutdown" else "cancelled" if decision in {"stop", "revoked"} else "completed")
        assert (await service.list_approvals(user_id=2, run_id=run.id))[0].status == {
            "allow": "approved", "deny": "denied",
        }.get(decision, "cancelled")


@pytest.mark.asyncio
async def test_execution_timeout_resumes_after_human_approval(tmp_path):
    async with harness(tmp_path, run_seconds=0.5) as (service, sessions, factory, workspace, session):
        service._factory = lambda: WaitingRuntime("blocked_after_approval")
        run = await service.submit(user_id=2, session_id=session.id, message="Ask then hang")
        await wait_for_status(service, run.id, "waiting_approval")
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        await asyncio.sleep(0.7)
        await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True)
        result, _ = await finished(service, run)
        assert result.status == "failed" and result.error_code == "execution_timeout"


@pytest.mark.asyncio
async def test_parallel_approvals_resume_only_after_last_decision_and_keep_remaining_budget():
    async with RunDeadline(0.4) as deadline:
        await asyncio.sleep(0.08)
        deadline.pause("a")
        remaining = deadline.remaining
        deadline.pause("b")
        deadline.resolve("a")
        deadline.resolve("unrelated")
        await asyncio.sleep(0.5)
        assert deadline.timer.when() is None
        deadline.resolve("b")
        assert deadline.timer.when() is not None
        assert deadline.remaining == remaining < 0.4


@pytest.mark.asyncio
async def test_multiple_waits_do_not_reset_consumed_execution_budget():
    with pytest.raises(TimeoutError):
        async with RunDeadline(0.3) as deadline:
            await asyncio.sleep(0.08)
            deadline.pause("first")
            first = deadline.remaining
            await asyncio.sleep(0.4)
            deadline.resolve("first")
            await asyncio.sleep(0.08)
            deadline.pause("second")
            assert deadline.remaining < first
            await asyncio.sleep(0.4)
            deadline.resolve("second")
            await asyncio.sleep(0.3)


@pytest.mark.asyncio
async def test_exhausted_execution_budget_cannot_become_an_unlimited_wait():
    with pytest.raises(TimeoutError):
        async with RunDeadline(0) as deadline:
            deadline.pause("too-late")


@pytest.mark.asyncio
async def test_restart_recovery_cancels_non_expiring_approval_without_replaying_it(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        await service.shutdown()
        async with sessions() as db, db.begin():
            (await db.get(CodingSession, session.id)).status = "waiting_approval"
            db.add(CodingRun(id="lost-run", session_id=session.id, user_id=2,
                             status="waiting_approval", prompt="Wait"))
            await db.flush()
            db.add(CodingApproval(id="lost-approval", run_id="lost-run", runtime_request_id="lost-request",
                                  status="pending", expires_at=None))
        assert await service.startup() == 1
        run = await service.get_run(user_id=2, run_id="lost-run")
        assert run.status == "interrupted" and run.error_code == "backend_restart"
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        assert approval.status == "cancelled" and approval.expires_at is None
        assert not factory.instances


@pytest.mark.asyncio
async def test_command_approval_waits_without_starting_container(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, grant, session, _):
        source, checkout, runner = await command_setup(service, session, tmp_path)
        service.limits = replace(service.limits, run_seconds=2)
        service._factory = CommandRuntime
        run = await service.submit(user_id=2, session_id=session.id, message="Run after approval")
        await wait_for_status(service, run.id, "waiting_approval")
        response = await client.get(f"/coding/runs/{run.id}/approvals")
        approval = response.json()[0]
        assert approval["expires_at"] is None
        await asyncio.sleep(2.2)
        assert not runner.calls
        assert (await service.get_run(user_id=2, run_id=run.id)).status == "waiting_approval"
        await service.resolve_approval(user_id=2, approval_id=approval["id"], approved=True,
                                       command_hash=approval["command"]["hash"])
        result, _ = await finished(service, run)
        assert result.status == "completed" and len(runner.calls) == 1, (result.error_code, runner.calls)


class WaitingEditRuntime(EditRuntime):
    async def run(self, prompt):
        async for event in super().run(prompt):
            if event.kind == "interaction.requested":
                event.payload.pop("timeout", None)
            yield event


@pytest.mark.asyncio
async def test_long_wait_does_not_allow_a_stale_file_write(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, grant, session, _):
        source, checkout = await enable(service, session, tmp_path)
        service.limits = replace(service.limits, run_seconds=0.5)
        service._factory = WaitingEditRuntime
        run = await service.submit(user_id=2, session_id=session.id, message="Edit after approval")
        await wait_for_status(service, run.id, "waiting_approval")
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        assert approval.expires_at is None
        await asyncio.sleep(0.7)
        target = Path(checkout.canonical_path) / "file.txt"
        target.write_text("new operator content\n")
        await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True,
                                       change_hash=approval.change["after_hash"])
        await finished(service, run)
        assert target.read_text() == "new operator content\n"
        assert (await service.list_changes(user_id=2, run_id=run.id))[0].status == "failed"


def test_approval_wait_migration_preserves_old_expiry_and_refuses_lossy_downgrade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE user_projects (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE model_profiles (id INTEGER PRIMARY KEY)")
            with Operations.context(MigrationContext.configure(connection)):
                import_module("app.db.migrations.versions.052_coding_sessions").upgrade()
                connection.exec_driver_sql("INSERT INTO coding_approvals (id,run_id,runtime_request_id,status,payload_json,expires_at) VALUES ('old','r','old','pending','{}','2030-01-01')")
                migration = import_module("app.db.migrations.versions.057_coding_approval_wait")
                migration.upgrade()
                assert next(c for c in inspect(connection).get_columns("coding_approvals") if c["name"] == "expires_at")["nullable"]
                assert connection.exec_driver_sql("SELECT expires_at FROM coding_approvals WHERE id='old'").scalar() == "2030-01-01"
                connection.exec_driver_sql("INSERT INTO coding_approvals (id,run_id,runtime_request_id,status,payload_json) VALUES ('new','r','new','pending','{}')")
                with pytest.raises(RuntimeError, match="Non-expiring"):
                    migration.downgrade()
                connection.exec_driver_sql("DELETE FROM coding_approvals WHERE id='new'")
                migration.downgrade()
                assert not next(c for c in inspect(connection).get_columns("coding_approvals") if c["name"] == "expires_at")["nullable"]
    finally:
        engine.dispose()
