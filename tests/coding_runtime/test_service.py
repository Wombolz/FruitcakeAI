from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.coding_runtime.contracts import CodingRuntimeCapabilities, CodingRuntimeError, CodingRuntimeEvent
from app.coding_runtime.service import CodingServiceError, CodingServiceLimits, CodingSessionService
from app.db.models import (
    CodingApproval, CodingRun, CodingRunEvent, CodingSession, CodingWorkspace, ModelProfile,
    User, UserModelAccess, UserProject,
)
from app.db.session import Base


class FakeRuntime:
    def __init__(self, mode="complete"):
        self.mode = mode
        self.starts = []
        self.prompts = []
        self.controls = []
        self.closed = 0
        self.aborted = False
        self.release = asyncio.Event()
        self.confirmation = asyncio.Event()
        self.approved = False

    def capabilities(self):
        return CodingRuntimeCapabilities(steering=True, follow_up=True, confirmations=True)

    async def start(self, request):
        self.starts.append(request)
        if self.mode == "startup_hang":
            await self.release.wait()

    async def run(self, prompt):
        self.prompts.append(prompt)
        yield CodingRuntimeEvent("run.started")
        if self.mode in {"blocked", "ignores_abort"}:
            await self.release.wait()
        if self.mode == "crash":
            raise CodingRuntimeError("runtime_crashed", "Private provider error must not be persisted")
        if self.mode in {"approval", "approval_expiry"}:
            yield CodingRuntimeEvent("interaction.requested", {
                "id": "private-runtime-request", "method": "confirm", "title": "Read fixture?",
                "message": "An authorized read", "timeout": 50 if self.mode == "approval_expiry" else 10000,
            })
            if self.mode == "approval_expiry":
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(self.confirmation.wait(), 0.08)
            else:
                await self.confirmation.wait()
            yield CodingRuntimeEvent("operation.completed", {"operation_id": "op1", "tool": "read", "is_error": not self.approved})
        if self.mode == "flood":
            yield CodingRuntimeEvent("assistant.text_delta", {"delta": "x" * 100000})
        if self.mode == "large_tool":
            yield CodingRuntimeEvent("operation.completed", {
                "operation_id": "large-read", "tool": "read", "text": '\\"\n\t\x00' * 10000,
                "is_error": False,
            })
        if self.mode == "large_snapshot":
            yield CodingRuntimeEvent("assistant.message", {"text": "x" * 50000, "stop_reason": "stop"})
            yield CodingRuntimeEvent("run.completed")
            return
        if self.mode == "many_deltas":
            for _ in range(3000):
                yield CodingRuntimeEvent("assistant.reasoning_status", {"status": "thinking_delta"})
                yield CodingRuntimeEvent("assistant.text_delta", {"delta": "x"})
            yield CodingRuntimeEvent("assistant.message", {"text": "x" * 3000})
            yield CodingRuntimeEvent("run.completed")
            return
        if self.mode == "event_flood":
            for _ in range(20):
                yield CodingRuntimeEvent("operation.started", {"operation_id": "op", "tool": "read"})
        if self.mode == "metadata_flood":
            yield CodingRuntimeEvent("assistant.message", {"text": "", "stop_reason": "x" * 40000})
        yield CodingRuntimeEvent("future_private", {"thinking": "DO_NOT_PERSIST"}, "provider-specific")
        yield CodingRuntimeEvent("assistant.text_delta", {"delta": "Visible answer", "thinking": "DO_NOT_PERSIST"})
        yield CodingRuntimeEvent("assistant.message", {"text": "Visible answer", "reasoning": "DO_NOT_PERSIST"})
        if self.mode == "incomplete":
            return
        yield CodingRuntimeEvent("run.completed", {"aborted": self.aborted})

    async def steer(self, message):
        self.controls.append(("steer", message))

    async def follow_up(self, message):
        self.controls.append(("follow_up", message))

    async def confirm(self, request_id, approved):
        assert request_id == "private-runtime-request"
        self.approved = approved
        self.confirmation.set()

    async def abort(self):
        if self.mode == "ignores_abort":
            return
        self.aborted = True
        self.release.set()
        self.confirmation.set()

    async def close(self):
        self.closed += 1


class Factory:
    def __init__(self):
        self.mode = "complete"
        self.instances = []

    def __call__(self):
        runtime = FakeRuntime(self.mode)
        self.instances.append(runtime)
        return runtime


@asynccontextmanager
async def harness(tmp_path, *, review=False, reviewer=None, read_approval=False, **limit_values):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'coding.sqlite'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db, db.begin():
        db.add_all([
            User(id=1, username="operator", email="operator@example.test", hashed_password="unused", role="admin"),
            User(id=2, username="owner", email="owner@example.test", hashed_password="unused", role="parent"),
            User(id=3, username="outsider", email="outsider@example.test", hashed_password="unused", role="parent"),
            ModelProfile(id=1, model_id="ollama_chat/fixture", display_name="Fixture", provider_family="ollama", is_local=True),
        ])
    factory = Factory()
    limits = CodingServiceLimits(**limit_values)
    service = CodingSessionService(sessions, factory, runtime_kind="fixture", limits=limits, checkout_root=tmp_path / "managed",
                                  review_capture_root=tmp_path / "captures" if review or reviewer else None,
                                  review_runtime_factory=reviewer, review_require_read_approval=read_approval)
    await service.startup()
    repository = tmp_path / "repo"
    repository.mkdir()
    workspace = await service.approve_workspace(operator_id=1, user_id=2, path=str(repository))
    session = await service.create_session(user_id=2, workspace_id=workspace.id, model_profile_id=1)
    try:
        yield service, sessions, factory, workspace, session
    finally:
        await service.shutdown()
        await engine.dispose()


async def finished(service, run):
    async with asyncio.timeout(3):
        events = [event async for event in service.events(user_id=2, run_id=run.id, follow=True)]
        result = await service.get_run(user_id=2, run_id=run.id)
        return result, events


async def wait_for_status(service, run_id, expected):
    async with asyncio.timeout(3):
        while (await service.get_run(user_id=2, run_id=run_id)).status != expected:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_run_is_durable_replayable_and_excludes_provider_reasoning(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect the repository")
        result, events = await finished(service, run)
        assert result.status == "completed"
        assert result.response_text == "Visible answer"
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert sum(event.kind == "run.completed" for event in events) == 1
        assert events[-1].kind == "run.completed"
        assert "DO_NOT_PERSIST" not in json.dumps([event.payload for event in events])
        replay = [event async for event in service.events(user_id=2, run_id=run.id, after=events[-2].sequence)]
        assert len(replay) == 1
        assert factory.instances[0].starts[0].workspace == tmp_path / "repo"


@pytest.mark.asyncio
async def test_access_is_owner_scoped_and_projects_never_grant_workspace_rights(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        for action in (
            service.get_session(user_id=3, session_id=session.id),
            service.create_session(user_id=3, workspace_id=workspace.id, model_profile_id=1),
            service.approve_workspace(operator_id=2, user_id=2, path=str(tmp_path / "repo")),
        ):
            with pytest.raises(CodingServiceError):
                await action
        assert await service.list_sessions(user_id=3) == []
        async with sessions() as db, db.begin():
            project = UserProject(user_id=3, name="Other owner's project")
            db.add(project)
            await db.flush()
            project_id = project.id
        with pytest.raises(CodingServiceError, match="project"):
            await service.create_session(user_id=2, workspace_id=workspace.id, model_profile_id=1, project_id=project_id)
        assert not factory.instances


@pytest.mark.asyncio
async def test_workspace_and_model_revocation_are_checked_on_every_submission(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        run = await service.submit(user_id=2, session_id=session.id, message="First")
        await finished(service, run)
        async with sessions() as db, db.begin():
            (await db.get(CodingWorkspace, workspace.id)).is_active = False
        with pytest.raises(CodingServiceError) as error:
            await service.submit(user_id=2, session_id=session.id, message="Second")
        assert error.value.code == "workspace_denied"
        async with sessions() as db, db.begin():
            (await db.get(CodingWorkspace, workspace.id)).is_active = True
            db.add(UserModelAccess(user_id=2, model_profile_id=1, allowed=False))
        with pytest.raises(CodingServiceError) as error:
            await service.submit(user_id=2, session_id=session.id, message="Third")
        assert error.value.code == "model_denied"
        assert len(factory.instances[0].prompts) == 1


@pytest.mark.asyncio
async def test_duplicate_requests_are_idempotent_and_conflicts_reject(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        first, second = await asyncio.gather(*(
            service.submit(user_id=2, session_id=session.id, message="Same", client_request_id="send-1")
            for _ in range(2)
        ))
        assert first.id == second.id
        await finished(service, first)
        assert len(factory.instances[0].prompts) == 1
        with pytest.raises(CodingServiceError) as error:
            await service.submit(user_id=2, session_id=session.id, message="Different", client_request_id="send-1")
        assert error.value.code == "request_conflict"


@pytest.mark.asyncio
async def test_active_run_and_process_capacity_are_enforced(tmp_path):
    async with harness(tmp_path, max_active_sessions=1) as (service, sessions, factory, workspace, session):
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Wait")
        await wait_for_status(service, run.id, "running")
        with pytest.raises(CodingServiceError) as error:
            await service.submit(user_id=2, session_id=session.id, message="Another")
        assert error.value.code == "session_busy"
        other = await service.create_session(user_id=2, workspace_id=workspace.id, model_profile_id=1)
        with pytest.raises(CodingServiceError) as error:
            await service.submit(user_id=2, session_id=other.id, message="No room")
        assert error.value.code == "capacity_reached"
        assert await service.stop(user_id=2, run_id=run.id)
        result, _ = await finished(service, run)
        assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_stop_before_startup_begins_still_finalizes_the_run(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        factory.mode = "startup_hang"
        run = await service.submit(user_id=2, session_id=session.id, message="Stop immediately")
        assert await service.stop(user_id=2, run_id=run.id)
        result, _ = await finished(service, run)
        assert result.status == "cancelled"
        assert factory.instances[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("crash", "runtime_crashed"), ("incomplete", "incomplete_stream"), ("flood", "response_limit"), ("blocked", "execution_timeout")])
async def test_failure_timeout_and_output_limits_are_terminal(tmp_path, mode, expected):
    async with harness(tmp_path, run_seconds=0.05) as (service, sessions, factory, workspace, session):
        factory.mode = mode
        run = await service.submit(user_id=2, session_id=session.id, message="Probe failure")
        result, events = await finished(service, run)
        assert result.status == "failed"
        assert result.error_code == expected
        assert events[-1].kind == "run.failed"
        assert factory.instances[0].closed
        assert "Private provider error" not in json.dumps([event.payload for event in events])


@pytest.mark.asyncio
async def test_large_escaped_tool_preview_does_not_fail_the_run(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        factory.mode = "large_tool"
        run = await service.submit(user_id=2, session_id=session.id, message="Read a large file")
        result, events = await finished(service, run)
        assert result.status == "completed"
        preview = next(event.payload for event in events if event.kind == "operation.completed")
        assert preview["preview_truncated"] is True
        assert preview["original_text_bytes"] == 50000
        assert "preview truncated by Fruitcake" in preview["text"]
        assert len(json.dumps(preview, ensure_ascii=False).encode()) <= service.limits.max_payload_bytes
        assert factory.instances[0].closed == 0


@pytest.mark.asyncio
async def test_large_assistant_snapshot_is_fragmented_without_losing_text(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        factory.mode = "large_snapshot"
        run = await service.submit(user_id=2, session_id=session.id, message="Give a long answer")
        result, events = await finished(service, run)
        assert result.status == "completed"
        assert result.response_text == "x" * 50000
        rendered = ""
        for event in events:
            assert len(json.dumps(event.payload, ensure_ascii=False).encode()) <= service.limits.max_payload_bytes
            if event.kind == "assistant.message":
                rendered = event.payload["text"]
            elif event.kind == "assistant.text_delta":
                rendered += event.payload["delta"]
        assert rendered == result.response_text


@pytest.mark.asyncio
async def test_token_updates_are_coalesced_without_exhausting_event_budget(tmp_path):
    async with harness(tmp_path, max_run_events=50) as (service, sessions, factory, workspace, session):
        factory.mode = "many_deltas"
        run = await service.submit(user_id=2, session_id=session.id, message="Think and answer")
        result, events = await finished(service, run)
        assert result.status == "completed"
        assert result.response_text == "x" * 3000
        assert sum(event.kind == "assistant.reasoning_status" for event in events) == 1
        assert len(events) < 20
        assert "".join(event.payload["delta"] for event in events if event.kind == "assistant.text_delta") == result.response_text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("event_flood", "event_limit"), ("metadata_flood", "event_payload_limit")])
async def test_real_limits_remain_enforced_with_specific_errors(tmp_path, mode, expected):
    async with harness(tmp_path, max_run_events=10) as (service, sessions, factory, workspace, session):
        factory.mode = mode
        run = await service.submit(user_id=2, session_id=session.id, message="Exercise hard bounds")
        result, events = await finished(service, run)
        assert result.status == "failed"
        assert result.error_code == expected
        assert events[-1].payload["error_code"] == expected


@pytest.mark.asyncio
async def test_idle_eviction_reconstructs_bounded_context_in_a_new_generation(tmp_path):
    async with harness(tmp_path, idle_seconds=0.05) as (service, sessions, factory, workspace, session):
        first = await service.submit(user_id=2, session_id=session.id, message="Remember this context")
        await finished(service, first)
        await asyncio.sleep(0.06)
        await service.expire_idle()
        second = await service.submit(user_id=2, session_id=session.id, message="Continue")
        result, events = await finished(service, second)
        assert result.context_generation == 2
        assert factory.instances[0].closed
        assert len(factory.instances) == 2
        assert factory.instances[1].prompts[0].history == (("user", "Remember this context"), ("assistant", "Visible answer"))
        assert any(event.payload.get("context_reconstructed") for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_approval_decisions_are_durable_owned_and_bound_to_active_run(tmp_path, approved):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        factory.mode = "approval"
        run = await service.submit(user_id=2, session_id=session.id, message="Ask first")
        await wait_for_status(service, run.id, "waiting_approval")
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        with pytest.raises(CodingServiceError):
            await service.resolve_approval(user_id=3, approval_id=approval.id, approved=True)
        await service.resolve_approval(user_id=2, approval_id=approval.id, approved=approved)
        await finished(service, run)
        saved = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        assert saved.status == ("approved" if approved else "denied")
        assert saved.resolved_by_user_id == 2
        events = [event async for event in service.events(user_id=2, run_id=run.id)]
        assert "private-runtime-request" not in json.dumps([event.payload for event in events])
        with pytest.raises(CodingServiceError):
            await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True)


@pytest.mark.asyncio
async def test_expired_approval_cannot_be_replayed(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        factory.mode = "approval_expiry"
        run = await service.submit(user_id=2, session_id=session.id, message="Expire the request")
        await finished(service, run)
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        assert approval.status == "expired"
        with pytest.raises(CodingServiceError):
            await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True)


@pytest.mark.asyncio
async def test_shutdown_and_restart_recovery_preserve_honest_terminal_states(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Shutdown test")
        await wait_for_status(service, run.id, "running")
        await service.shutdown()
        result = await service.get_run(user_id=2, run_id=run.id)
        assert result.status == "interrupted"
        assert result.error_code == "server_shutdown"
        async with sessions() as db, db.begin():
            stale = CodingRun(id="stale-run", session_id=session.id, user_id=2, prompt="Lost process", status="running")
            db.add(stale)
            (await db.get(CodingSession, session.id)).status = "running"
        replacement = CodingSessionService(sessions, factory, runtime_kind="fixture")
        try:
            assert await replacement.startup() == 1
            stale = await replacement.get_run(user_id=2, run_id="stale-run")
            assert stale.status == "interrupted"
            assert stale.error_code == "backend_restart"
        finally:
            await replacement.shutdown()


@pytest.mark.asyncio
async def test_late_stop_does_not_cancel_a_later_run(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        first = await service.submit(user_id=2, session_id=session.id, message="First")
        await finished(service, first)
        factory.instances[0].mode = "blocked"
        second = await service.submit(user_id=2, session_id=session.id, message="Second")
        await wait_for_status(service, second.id, "running")
        assert not await service.stop(user_id=2, run_id=first.id)
        assert not factory.instances[0].aborted
        await service.steer(user_id=2, run_id=second.id, message="Narrow the scope")
        assert factory.instances[0].controls == [("steer", "Narrow the scope")]
        await service.stop(user_id=2, run_id=second.id)
        await finished(service, second)


@pytest.mark.asyncio
async def test_workspace_revocation_stops_active_work_and_prevents_reuse(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect")
        await wait_for_status(service, run.id, "running")
        await service.revoke_workspace(operator_id=1, workspace_id=workspace.id)
        result, _ = await finished(service, run)
        assert result.status == "cancelled"
        assert factory.instances[0].closed
        with pytest.raises(CodingServiceError) as error:
            await service.submit(user_id=2, session_id=session.id, message="Try again")
        assert error.value.code == "workspace_denied"


@pytest.mark.asyncio
async def test_replaced_workspace_symlink_is_not_reauthorized_implicitly(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        repository = tmp_path / "repo"
        repository.rename(tmp_path / "old-repo")
        private = tmp_path / "private"
        private.mkdir()
        repository.symlink_to(private, target_is_directory=True)
        with pytest.raises(CodingServiceError) as error:
            await service.submit(user_id=2, session_id=session.id, message="Try escaped repository")
        assert error.value.code == "workspace_denied"
        assert not factory.instances


@pytest.mark.asyncio
async def test_idle_slot_is_reclaimed_to_make_room_without_exceeding_capacity(tmp_path):
    async with harness(tmp_path, max_active_sessions=1) as (service, sessions, factory, workspace, session):
        first = await service.submit(user_id=2, session_id=session.id, message="First session")
        await finished(service, first)
        other = await service.create_session(user_id=2, workspace_id=workspace.id, model_profile_id=1)
        second = await service.submit(user_id=2, session_id=other.id, message="Second session")
        await finished(service, second)
        assert factory.instances[0].closed
        assert len(service._slots) == 1


@pytest.mark.asyncio
async def test_service_disabled_by_default_does_not_construct_a_runtime(monkeypatch):
    from app.config import settings
    from app.coding_runtime.bootstrap import get_coding_session_service

    monkeypatch.setattr(settings, "coding_runtime_enabled", False)
    assert get_coding_session_service() is None


@pytest.mark.asyncio
async def test_context_reconstruction_retains_recent_steering_and_follow_up(tmp_path):
    async with harness(tmp_path, idle_seconds=0.05) as (service, sessions, factory, workspace, session):
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect the whole repository")
        await wait_for_status(service, run.id, "running")
        await service.steer(user_id=2, run_id=run.id, message="Focus on tests")
        await service.steer(user_id=2, run_id=run.id, message="Then summarize", follow_up=True)
        factory.instances[0].release.set()
        await finished(service, run)
        await asyncio.sleep(0.06)
        await service.expire_idle()
        factory.mode = "complete"
        resumed = await service.submit(user_id=2, session_id=session.id, message="Continue")
        await finished(service, resumed)
        history = factory.instances[-1].prompts[0].history
        assert ("user", "Focus on tests") in history
        assert ("user", "Then summarize") in history
        assert sum(len(content) for _, content in history) <= 15000


@pytest.mark.asyncio
async def test_stop_deadline_closes_a_runtime_that_acknowledges_without_settling(tmp_path):
    async with harness(tmp_path, stop_seconds=0.05) as (service, sessions, factory, workspace, session):
        factory.mode = "ignores_abort"
        run = await service.submit(user_id=2, session_id=session.id, message="Wait")
        await wait_for_status(service, run.id, "running")
        async with asyncio.timeout(1):
            await service.stop(user_id=2, run_id=run.id)
        result, _ = await finished(service, run)
        assert result.status == "cancelled"
        assert factory.instances[0].closed


@pytest.mark.asyncio
async def test_shutdown_attempts_every_runtime_when_one_cleanup_fails(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, workspace, session):
        first = await service.submit(user_id=2, session_id=session.id, message="First")
        await finished(service, first)
        other = await service.create_session(user_id=2, workspace_id=workspace.id, model_profile_id=1)
        second = await service.submit(user_id=2, session_id=other.id, message="Second")
        await finished(service, second)
        original_close = factory.instances[0].close

        async def failed_close():
            raise RuntimeError("Fixture close failure")

        factory.instances[0].close = failed_close
        try:
            with pytest.raises(CodingServiceError) as error:
                await service.shutdown()
            assert error.value.code == "cleanup_failed"
            assert factory.instances[1].closed
        finally:
            factory.instances[0].close = original_close


@pytest.mark.asyncio
async def test_startup_timeout_is_distinct_from_execution_timeout(tmp_path):
    async with harness(tmp_path, startup_seconds=0.05) as (service, sessions, factory, workspace, session):
        factory.mode = "startup_hang"
        run = await service.submit(user_id=2, session_id=session.id, message="Start a runtime")
        result, _ = await finished(service, run)
        assert result.status == "failed"
        assert result.error_code == "startup_timeout"
        assert factory.instances[0].closed
