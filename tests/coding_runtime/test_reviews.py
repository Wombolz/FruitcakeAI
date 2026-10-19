import asyncio
from dataclasses import asdict
from datetime import timedelta
from importlib import import_module
import json
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, select

from app.coding_runtime.contracts import CodingModel, CodingRuntimeCapabilities, CodingRuntimeError, CodingRuntimeEvent
from app.coding_runtime.review_budget import input_byte_budget
from app.coding_runtime.review_output import ReviewMessages, ReviewOutput
from app.coding_runtime.review_service import now
from app.coding_runtime.review_supervisor import READ_PROTOCOL
from app.coding_runtime.service import CodingServiceError
from app.db.models import (CodingCheckoutReservation, CodingReview, CodingReviewApproval, CodingReviewCapture, CodingReviewFinding,
    CodingReviewEvent, CodingSession, ModelProfile, UserModelAccess, CodingWorkspace)
from app.db.session import Base
from app.coding_runtime.service import CodingSessionService
from app.coding_runtime.review_admission import claim
from test_service import harness
from test_api import api_harness, headers, frames
from test_changes import enable


FINDING = {"type": "finding", "severity": "should_fix", "title": "Missing result", "body": "Returns no value", "path": "file.txt",
           "side": "after", "line_start": 1, "line_end": 1, "evidence": "return None"}
SUMMARY = {"type": "summary", "summary": "One issue reported; tests were not executed", "limitations": []}


class Reviewer:
    def __init__(self, mode="complete"):
        self.mode = mode
        self.started = asyncio.Event()
        self.finding_sent = asyncio.Event()
        self.response = None
        self.closed = False
        self.hold = asyncio.Event()

    def capabilities(self):
        return CodingRuntimeCapabilities(confirmations=True)

    async def start(self, request):
        self.request = request
        self.provenance = {"sent": request.resolved_effort, "runtime_readback": request.resolved_effort,
                           "rpc_acknowledged": True, "provider_confirmation": None, "runtime_version": "fixture"}
        self.started.set()

    async def run(self, prompt):
        self.prompt = prompt
        assert prompt.history == ()
        assert (self.request.workspace / "file.txt").read_text() == "return None\n"
        assert not (self.request.workspace / ".git").exists()
        if self.mode == "tool_narration":
            yield CodingRuntimeEvent("assistant.text_delta", {"delta": "I will inspect the captured file.\n"})
            yield CodingRuntimeEvent("assistant.message", {"text": "I will inspect the captured file.\n", "stop_reason": "toolUse"})
        if self.mode == "tool_escape":
            yield CodingRuntimeEvent("operation.started", {"tool": "bash", "arguments": {"command": "id"}})
            return
        proposal = {"protocol": READ_PROTOCOL, "operation_id": "read-file", "path": "file.txt", "side": "after"}
        if self.mode == "outside_read":
            proposal["path"] = "../../outside"
        if self.mode == "large_read":
            proposal["path"] = "large.txt"
        if self.mode == "unicode_read":
            proposal["path"] = "unicode.txt"
        if self.mode == "patch_read":
            proposal["side"] = "patch"
        yield CodingRuntimeEvent("interaction.requested", {"id": "read-1", "method": "input", "title": "Read captured review file?",
                                                           "placeholder": json.dumps(proposal)})
        if self.mode == "patch_read":
            self.patch_response = self.response
            proposal.update(side="after", operation_id="read-source")
            yield CodingRuntimeEvent("interaction.requested", {"id": "read-2", "method": "input", "title": "Read captured review file?",
                                                               "placeholder": json.dumps(proposal)})
        if self.response["status"] != "completed" and self.mode != "claim_unread":
            yield CodingRuntimeEvent("assistant.message", {"text": json.dumps(SUMMARY), "stop_reason": "stop"})
            yield CodingRuntimeEvent("run.completed", {"aborted": False})
            return
        finding = dict(FINDING)
        if self.mode == "bad_anchor": finding["line_end"] = 999
        if self.mode == "invented_path": finding["path"] = "not-captured.py"
        if self.mode == "bad_evidence": finding["evidence"] = "imaginary text"
        if self.mode == "private_path": finding["body"] = f"Internal path: {self.request.workspace}/file.txt"
        text = json.dumps(finding, indent=2 if self.mode == "pretty" else None) + "\n"
        if self.mode == "malformed": text += "not json\n"
        yield CodingRuntimeEvent("assistant.text_delta", {"delta": text, "thinking": "PRIVATE_CHAIN_OF_THOUGHT"})
        self.finding_sent.set()
        if self.mode == "length":
            yield CodingRuntimeEvent("assistant.message", {"text": text, "stop_reason": "length"})
            yield CodingRuntimeEvent("run.completed", {"aborted": False})
            return
        if self.mode in {"block", "cleanup_fail"}:
            await self.hold.wait()
        yield CodingRuntimeEvent("assistant.text_delta", {"delta": json.dumps(SUMMARY) + "\n"})
        yield CodingRuntimeEvent("assistant.message", {"text": text + json.dumps(SUMMARY) + "\n"})
        yield CodingRuntimeEvent("run.completed", {"aborted": False})

    async def answer_read(self, request_id, result):
        self.response = result

    async def close(self):
        if self.mode == "cleanup_fail":
            raise RuntimeError("fixture cleanup failure")
        self.closed = True


class ReviewFactory:
    def __init__(self, mode="complete"):
        self.mode, self.instances = mode, []

    def __call__(self):
        instance = Reviewer(self.mode)
        self.instances.append(instance)
        return instance


async def setup(service, session, tmp_path):
    source, checkout = await enable(service, session, tmp_path)
    (Path(checkout.canonical_path) / "file.txt").write_text("return None\n")
    return source, checkout


async def start(service, session, **overrides):
    return await service.start_review(user_id=2, session_id=session.id,
        **{"model_profile_id": 1, "scope": "checkout_changes", "paths": ["file.txt"], "client_request_id": str(uuid4()), **overrides})


async def done(service, review):
    slot = service._reviews.get(review["id"])
    if slot:
        await asyncio.wait_for(asyncio.shield(slot.task), 5)
    return await service.get_review(user_id=2, review_id=review["id"])


async def wait_status(service, review_id, status):
    async with asyncio.timeout(5):
        while True:
            row = await service.get_review(user_id=2, review_id=review_id, include_content=False)
            if row["status"] == status:
                return row
            if row["status"] in {"failed", "cancelled", "interrupted"}:
                pytest.fail(str(row))
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_review_is_fresh_capture_only_measured_and_independent_of_builder(tmp_path):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, builder, _, session):
        source, checkout = await setup(service, session, tmp_path)
        async with sessions() as db, db.begin():
            db.add(ModelProfile(id=2, model_id="ollama_chat/other", display_name="Reviewer", provider_family="ollama", is_local=True))
        review = await start(service, session, model_profile_id=2)
        result = await done(service, review)
        assert result["status"] == "completed", result
        assert not builder.instances
        assert result["model"]["profile_id"] == 2
        assert (await service.get_session(user_id=2, session_id=session.id)).model_profile_id == 1
        runtime = reviewer.instances[0]
        assert runtime.closed and runtime.response["text"].endswith("L1|return None\n")
        assert runtime.response["source_bytes"] == len("return None\n".encode())
        assert runtime.request.workspace != Path(checkout.canonical_path) and runtime.request.workspace != source
        assert not runtime.request.workspace.exists()
        assert result["coverage"]["successful_captured_reads"] == 1
        assert result["coverage"]["read_result_truncated"] is False
        assert result["coverage"]["semantic_coverage"] == "unknown"
        assert result["reasoning"]["provider_confirmation"] is None
        assert result["findings"][0]["evidence"] == "return None"
        async with sessions() as db:
            assert await db.get(CodingCheckoutReservation, checkout.id) is None
        events = [event async for event in service.review_events(user_id=2, review_id=result["id"])]
        assert "PRIVATE_CHAIN_OF_THOUGHT" not in str(events)
        assert [event.sequence for event in events] == list(range(1, len(events)+1))
        (Path(checkout.canonical_path) / "file.txt").write_text("later\n")
        assert (await service.get_review(user_id=2, review_id=result["id"]))["capture"]["applicability"] == "stale"
        diff = await service.review_capture_diff(user_id=2, capture_id=result["capture_id"], path="file.txt")
        assert "+return None" in diff["patch"] and "+later" not in diff["patch"]
        again = await done(service, await start(service, session))
        assert len(reviewer.instances) == 2 and reviewer.instances[0] is not reviewer.instances[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,error", [("bad_anchor", "review_output_invalid"), ("invented_path", "review_output_invalid"),
    ("bad_evidence", "review_output_invalid"), ("malformed", "review_output_invalid"), ("tool_escape", "runtime_denied")])
async def test_invalid_output_is_a_named_failed_review_not_a_clean_report(tmp_path, mode, error):
    reviewer = ReviewFactory(mode)
    async with harness(tmp_path, reviewer=reviewer) as (service, _, _, _, session):
        await setup(service, session, tmp_path)
        result = await done(service, await start(service, session))
        assert result["status"] == "failed", result
        assert result["error_code"] == error
        assert result["limitations"]
        assert len(result["findings"]) == (1 if mode == "malformed" else 0)
        assert reviewer.instances[0].closed


@pytest.mark.asyncio
async def test_private_execution_locations_are_not_public_model_navigation_targets(tmp_path):
    reviewer = ReviewFactory("private_path")
    async with harness(tmp_path, reviewer=reviewer) as (service, _, _, _, session):
        await setup(service, session, tmp_path)
        result = await done(service, await start(service, session))
        assert result["status"] == "completed"
        assert str(tmp_path) not in json.dumps(result, default=str)
        assert "[private capture]" in result["findings"][0]["body"]


@pytest.mark.asyncio
async def test_busy_idempotent_review_stop_retains_partial_findings(tmp_path):
    reviewer = ReviewFactory("block")
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, _, _, session):
        _, checkout = await setup(service, session, tmp_path)
        review = await start(service, session, client_request_id="once")
        await wait_status(service, review["id"], "running")
        await asyncio.wait_for(reviewer.instances[0].finding_sent.wait(), 3)
        assert (await start(service, session, client_request_id="once"))["id"] == review["id"]
        for operation in (
            lambda: start(service, session),
            lambda: service.submit(user_id=2, session_id=session.id, message="Must not overlap"),
            lambda: service.delivery(user_id=2, session_id=session.id, action="commit", files=["file.txt"], message="No overlap"),
        ):
            with pytest.raises(CodingServiceError) as exc:
                await operation()
            assert exc.value.code == "checkout_busy"
        with pytest.raises(CodingServiceError) as exc:
            await start(service, session, client_request_id="once", scope="uncommitted")
        assert exc.value.code == "request_conflict"
        assert await service.stop_review(user_id=2, review_id=review["id"])
        result = await done(service, review)
        assert result["status"] == "cancelled" and len(result["findings"]) == 1
        assert not await service.stop_review(user_id=2, review_id=review["id"])
        async with sessions() as db:
            assert await db.get(CodingCheckoutReservation, checkout.id) is None


@pytest.mark.asyncio
async def test_cancel_before_first_instruction_releases_only_own_review(tmp_path):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, _, _, session):
        _, checkout = await setup(service, session, tmp_path)
        review = await start(service, session)
        await service.stop_review(user_id=2, review_id=review["id"])
        result = await done(service, review)
        assert result["status"] == "cancelled" and not reviewer.instances
        assert result["capture"]["status"] == "interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_read_approval_is_durable_and_human_wait_is_not_an_execution_timeout(tmp_path, approve):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer, read_approval=True, run_seconds=0.15) as (service, sessions, _, _, session):
        await setup(service, session, tmp_path)
        review = await start(service, session)
        await wait_status(service, review["id"], "waiting_approval")
        approvals = await service.list_review_approvals(user_id=2, review_id=review["id"])
        assert approvals[0]["expires_at"] is None
        assert reviewer.instances[0].response is None
        await asyncio.sleep(0.25)  # Deliberately exceed the execution budget during a human wait.
        assert (await service.get_review(user_id=2, review_id=review["id"], include_content=False))["status"] == "waiting_approval"
        with pytest.raises(CodingServiceError):
            await service.resolve_review_approval(user_id=3, review_id=review["id"], approval_id=approvals[0]["id"], approved=True)
        await service.resolve_review_approval(user_id=2, review_id=review["id"], approval_id=approvals[0]["id"], approved=approve)
        result = await done(service, review)
        assert result["status"] == "completed", result
        assert result["coverage"]["successful_captured_reads"] == int(approve)
        assert result["coverage"]["read_denied"] == int(not approve)


@pytest.mark.asyncio
async def test_review_pin_retains_expired_content_while_waiting_and_releases_on_stop(tmp_path):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer, read_approval=True) as (service, sessions, _, _, session):
        await setup(service, session, tmp_path)
        review = await start(service, session)
        await wait_status(service, review["id"], "waiting_approval")
        async with sessions() as db, db.begin():
            (await db.get(CodingReviewCapture, review["capture_id"])).expires_at = now()-timedelta(seconds=1)
        await service.expire_captures()
        assert (await service.get_review(user_id=2, review_id=review["id"]))["capture"]["content_status"] == "available"
        await service.stop_review(user_id=2, review_id=review["id"])
        await service.expire_captures()
        assert (await service.get_review(user_id=2, review_id=review["id"]))["capture"]["content_status"] == "unavailable"
        assert (await service.list_review_approvals(user_id=2, review_id=review["id"]))[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_reasoning_policy_is_independent_and_unsupported_effort_does_not_admit(tmp_path):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, _, _, session):
        _, checkout = await setup(service, session, tmp_path)
        with pytest.raises(CodingServiceError) as exc:
            await start(service, session, effort="high")
        assert exc.value.code == "unsupported_effort" and not reviewer.instances
        async with sessions() as db, db.begin():
            profile = await db.get(ModelProfile, 1)
            profile.supports_thinking, profile.reasoning_efforts = True, ["low", "high", "unsupported"]
        options = await service.review_options(user_id=2, session_id=session.id)
        assert options["reviewer_profiles"][0]["reasoning_efforts"] == ["default", "low", "high"]
        result = await done(service, await start(service, session, effort="high"))
        assert result["reasoning"]["requested"] == "high" and result["reasoning"]["sent"] == "high"
        assert result["reasoning"]["provider_confirmation"] is None


@pytest.mark.asyncio
async def test_dispositions_are_owner_scoped_and_audited_without_marking_addressed(tmp_path):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, _, _, session):
        await setup(service, session, tmp_path)
        result = await done(service, await start(service, session))
        finding_id = result["findings"][0]["id"]
        with pytest.raises(CodingServiceError):
            await service.set_finding_disposition(user_id=3, review_id=result["id"], finding_id=finding_id, disposition="dismissed", reason="No")
        with pytest.raises(CodingServiceError):
            await service.set_finding_disposition(user_id=2, review_id=result["id"], finding_id=finding_id, disposition="dismissed", reason=" ")
        dismissed = await service.set_finding_disposition(user_id=2, review_id=result["id"], finding_id=finding_id, disposition="dismissed", reason="Expected behavior")
        assert dismissed["disposition"] == "dismissed" and dismissed["updated_by_user_id"] == 2
        await service.set_finding_disposition(user_id=2, review_id=result["id"], finding_id=finding_id, disposition="open")
        events = [event async for event in service.review_events(user_id=2, review_id=result["id"])]
        assert len([event for event in events if event.kind == "finding.disposition_changed"]) == 2
        assert (await service.get_review(user_id=2, review_id=result["id"]))["findings"][0]["disposition"] == "open"


@pytest.mark.asyncio
async def test_review_api_validates_selectors_and_replays_durable_events(tmp_path):
    reviewer = ReviewFactory()
    async with api_harness(tmp_path, reviewer=reviewer) as (client, app, service, sessions, _, _, session, auth):
        await setup(service, session, tmp_path)
        url = f"/coding/sessions/{session.id}/reviews"
        body = {"paths": ["file.txt"], "client_request_id": "api", "model_profile_id": 1}
        assert (await client.post(url, json={**body, "effort": "wild"})).status_code == 422
        assert (await client.post(url, json=body, headers=headers(3))).status_code == 404
        response = await client.post(url, json=body)
        assert response.status_code == 202, response.text
        result = await done(service, response.json())
        assert result["status"] == "completed"
        original = service.get_review
        async def checked(**kwargs):
            assert not auth["session"].in_transaction()
            return await original(**kwargs)
        service.get_review = checked
        assert (await client.get(f"/coding/reviews/{result['id']}")).status_code == 200
        service.get_review = original
        assert (await client.get(f"/coding/reviews/{result['id']}", headers=headers(3))).status_code == 404
        stream = await client.get(f"/coding/reviews/{result['id']}/events", params={"follow": False})
        events = frames(stream.text)
        assert events[-1]["event"] == "review.completed"
        assert events[-1]["data"]["review_id"] == result["id"]
        assert str(tmp_path) not in stream.text
        replay = await client.get(f"/coding/reviews/{result['id']}/events", params={"follow": False}, headers={"Last-Event-ID": events[-1]["id"]})
        assert replay.text == ""
        assert (await client.get(f"/coding/reviews/{result['id']}/events?after=9999&follow=false")).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grant", "model", "user"])
async def test_revocation_stops_the_reviewer_and_preserves_partial_evidence(tmp_path, kind):
    reviewer = ReviewFactory("block")
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, _, workspace, session):
        await setup(service, session, tmp_path)
        review = await start(service, session)
        await wait_status(service, review["id"], "running")
        await asyncio.wait_for(reviewer.instances[0].finding_sent.wait(), 3)
        slot = service._reviews[review["id"]]
        async with sessions() as db, db.begin():
            if kind == "grant": (await db.get(CodingWorkspace, workspace.id)).is_active = False
            if kind == "model": db.add(UserModelAccess(user_id=2, model_profile_id=1, allowed=False))
            if kind == "user":
                from app.db.models import User
                (await db.get(User, 2)).is_active = False
        await asyncio.wait_for(asyncio.shield(slot.task), 3)
        async with sessions() as db:
            row = await db.get(CodingReview, review["id"])
            assert row.status == "failed" and row.error_code == "authorization_changed"
            assert await db.scalar(select(CodingReviewFinding.id).where(CodingReviewFinding.review_id == row.id))
            assert await db.get(CodingCheckoutReservation, row.checkout_id) is None
        assert reviewer.instances[0].closed


@pytest.mark.asyncio
async def test_shutdown_recovery_does_not_resume_a_reviewer_or_drop_its_findings(tmp_path):
    reviewer = ReviewFactory("block")
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, builder, _, session):
        _, checkout = await setup(service, session, tmp_path)
        review = await start(service, session)
        await wait_status(service, review["id"], "running")
        await asyncio.wait_for(reviewer.instances[0].finding_sent.wait(), 3)
        await service.shutdown()
        async with sessions() as db, db.begin():
            row = await db.get(CodingReview, review["id"])
            assert row.status == "interrupted"
            row.status = "running"  # Simulate persisted work left by a process crash.
            db.add(CodingReviewApproval(id="pending", review_id=row.id, runtime_request_id="old-input", payload_json="{}", status="pending"))
            await claim(db, checkout.id, "review", row.id, "dead-epoch")
        replacement = CodingSessionService(sessions, builder, runtime_kind="fixture", checkout_root=tmp_path/"managed",
            review_capture_root=tmp_path/"captures", review_runtime_factory=reviewer)
        try:
            await replacement.startup()
            result = await replacement.get_review(user_id=2, review_id=review["id"])
            assert result["status"] == "interrupted" and result["error_code"] == "backend_restart"
            assert len(result["findings"]) == 1 and len(reviewer.instances) == 1
            async with sessions() as db:
                assert (await db.get(CodingReviewApproval, "pending")).status == "cancelled"
                assert await db.get(CodingCheckoutReservation, checkout.id) is None
        finally:
            await replacement.shutdown()


@pytest.mark.asyncio
async def test_cleanup_failure_pins_checkout_until_the_process_can_be_closed(tmp_path):
    reviewer = ReviewFactory("cleanup_fail")
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, _, _, session):
        _, checkout = await setup(service, session, tmp_path)
        review = await start(service, session)
        await wait_status(service, review["id"], "running")
        with pytest.raises(CodingServiceError) as exc:
            await service.stop_review(user_id=2, review_id=review["id"])
        assert exc.value.code == "cleanup_failed"
        async with sessions() as db:
            assert await db.get(CodingCheckoutReservation, checkout.id) is not None
        with pytest.raises(CodingServiceError) as exc:
            await start(service, session)
        assert exc.value.code == "checkout_busy"
        reviewer.instances[0].mode = "complete"
        await service.shutdown()
        async with sessions() as db:
            assert await db.get(CodingCheckoutReservation, checkout.id) is None


@pytest.mark.asyncio
async def test_simultaneous_stop_and_disconnected_observer_do_not_interrupt_process_cleanup(tmp_path):
    reviewer = ReviewFactory("block")
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, _, _, session):
        _, checkout = await setup(service, session, tmp_path)
        review = await start(service, session)
        await wait_status(service, review["id"], "running")
        runtime = reviewer.instances[0]
        cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()
        original = runtime.close
        async def close():
            cleaning.set()
            await finish_cleanup.wait()
            await original()
        runtime.close = close
        first = asyncio.create_task(service.stop_review(user_id=2, review_id=review["id"]))
        await asyncio.wait_for(cleaning.wait(), 3)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        second = asyncio.create_task(service.stop_review(user_id=2, review_id=review["id"]))
        async with sessions() as db:
            assert await db.get(CodingCheckoutReservation, checkout.id) is not None
        finish_cleanup.set()
        assert await asyncio.wait_for(second, 3)
        assert runtime.closed
        async with sessions() as db:
            assert await db.get(CodingCheckoutReservation, checkout.id) is None


@pytest.mark.asyncio
async def test_context_budget_and_reasoning_mismatch_fail_before_a_model_prompt(tmp_path):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer) as (service, sessions, _, _, session):
        await setup(service, session, tmp_path)
        async with sessions() as db, db.begin():
            (await db.get(ModelProfile, 1)).context_window_tokens = 8192
        result = await done(service, await start(service, session))
        assert result["error_code"] == "review_input_limit" and not reviewer.instances
        assert result["coverage"]["prompt_bytes"] > result["coverage"]["input_byte_budget"]
        assert any("No model prompt was sent" in text for text in result["limitations"])
        async with sessions() as db, db.begin():
            (await db.get(ModelProfile, 1)).context_window_tokens = 65536
        def bad_adapter():
            runtime = reviewer()
            original = runtime.start
            async def start_wrong(request):
                await original(request)
                runtime.provenance["runtime_readback"] = "high"
            runtime.start = start_wrong
            return runtime
        service._review_factory = bad_adapter
        result = await done(service, await start(service, session))
        assert result["error_code"] == "reasoning_mismatch"
        assert not hasattr(reviewer.instances[0], "prompt") and reviewer.instances[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pretty", "tool_narration"])
async def test_formatted_records_and_tool_turn_preambles_do_not_fail_valid_reviews(tmp_path, mode):
    reviewer = ReviewFactory(mode)
    async with harness(tmp_path, reviewer=reviewer) as (service, _, _, _, session):
        await setup(service, session, tmp_path)
        result = await done(service, await start(service, session))
        assert result["status"] == "completed", result
        assert len(result["findings"]) == 1
        assert "I will inspect" not in json.dumps(result, default=str)


@pytest.mark.asyncio
async def test_model_output_token_limit_is_not_misreported_as_format_failure(tmp_path):
    reviewer = ReviewFactory("length")
    async with harness(tmp_path, reviewer=reviewer) as (service, _, _, _, session):
        await setup(service, session, tmp_path)
        result = await done(service, await start(service, session))
        assert result["error_code"] == "review_output_limit"
        assert result["coverage"]["output_limit_kind"] == "token_allowance"
        assert result["coverage"]["assistant_stop_reason"] == "length"
        assert len(result["findings"]) == 1
        assert any("output token allowance" in text for text in result["limitations"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [("bad_anchor", "line_range"), ("invented_path", "unknown_path"),
    ("bad_evidence", "evidence_mismatch"), ("malformed", "json_syntax")])
async def test_invalid_reports_expose_safe_diagnostic_not_raw_output(tmp_path, mode, reason):
    reviewer = ReviewFactory(mode)
    async with harness(tmp_path, reviewer=reviewer) as (service, _, _, _, session):
        await setup(service, session, tmp_path)
        result = await done(service, await start(service, session))
        assert result["error_code"] == "review_output_invalid"
        assert result["coverage"]["output_validation_error"] == reason
        assert len(result["limitations"]) >= 3
        assert "PRIVATE_CHAIN_OF_THOUGHT" not in json.dumps(result, default=str)


def test_review_parser_accepts_chunked_formatted_objects_but_keeps_schema_and_evidence_strict():
    parser = ReviewOutput({("file.txt", "after"): "return None\n"})
    records = []
    for character in json.dumps(FINDING, indent=2) + "\n\n" + json.dumps(SUMMARY, indent=2):
        records.extend(parser.feed(character))
    records.extend(parser.feed("", flush=True))
    parser.finish()
    assert records == [FINDING, SUMMARY]


@pytest.mark.parametrize("text", ["I reviewed the code", "```json\n{}\n```", "[]", '{"type":"summary","summary":"Done","limitations":[],"extra":true}'])
def test_review_parser_rejects_prose_fences_arrays_and_extra_fields(text):
    parser = ReviewOutput({})
    with pytest.raises(CodingRuntimeError):
        list(parser.feed(text, flush=True))


def test_tool_narration_must_be_a_tool_turn_and_stays_bounded():
    parser = ReviewOutput({})
    messages = ReviewMessages(parser)
    assert list(messages.delta("Let me read the captured file.")) == []
    assert list(messages.message("Let me read the captured file.", "toolUse")) == []
    assert list(messages.message(json.dumps(SUMMARY), "stop")) == [SUMMARY]
    assert list(messages.finish()) == []
    messages = ReviewMessages(ReviewOutput({}))
    list(messages.delta("A conversational final answer"))
    with pytest.raises(CodingRuntimeError):
        list(messages.message("A conversational final answer", "stop"))
    messages = ReviewMessages(ReviewOutput({}))
    with pytest.raises(CodingRuntimeError, match="64 KiB"):
        list(messages.delta("x" * (64 * 1024 + 1)))


@pytest.mark.parametrize("context,output,expected", [(65536, 8192, 49152), (8192, 8192, 0),
    (16384, 8192, 0), (1_000_000, 8192, 512 * 1024)])
def test_review_budget_reserves_output_protocol_and_caps_bytes(context, output, expected):
    model = CodingModel(1, "fixture", "ollama", True, context, output)
    assert input_byte_budget(model) == expected


@pytest.mark.asyncio
async def test_three_file_review_uses_lazy_numbered_reads_not_duplicate_inline_source(tmp_path):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer) as (service, _, _, _, session):
        _, checkout = await setup(service, session, tmp_path)
        root = Path(checkout.canonical_path)
        (root / "implementation.py").write_text("# implementation\n" * 900)
        (root / "tests.py").write_text("# regression tests\n" * 750)
        paths = ["file.txt", "implementation.py", "tests.py"]
        result = await done(service, await start(service, session, paths=paths))
        assert result["status"] == "completed", result
        coverage = result["coverage"]
        assert coverage["prompt_bytes"] < 8192 < coverage["input_byte_budget"] == 49152
        assert coverage["files_in_scope"] == 3 and coverage["files_provided_in_prompt"] == 0
        assert coverage["patches_provided"] == 0
        assert coverage["prompt_delivered_to_runtime"] is True
        data = json.loads(reviewer.instances[0].prompt.message.split("\nCapture data:\n", 1)[1])
        assert {item["path"] for item in data["files"]} == set(paths)
        assert next(item["after"]["lines"] for item in data["files"] if item["path"] == "implementation.py") == 900
        assert "# implementation" not in reviewer.instances[0].prompt.message
        assert coverage["read_result_truncated"] is False


@pytest.mark.asyncio
async def test_review_budget_counts_utf8_bytes_and_keeps_large_prompt_fail_closed(tmp_path):
    reviewer = ReviewFactory("unicode_read")
    async with harness(tmp_path, reviewer=reviewer) as (service, _, _, _, session):
        _, checkout = await setup(service, session, tmp_path)
        (Path(checkout.canonical_path) / "unicode.txt").write_text("\u754c" * 20000)
        result = await done(service, await start(service, session, paths=["file.txt", "unicode.txt"]))
        assert result["status"] == "completed" and not result["findings"]
        assert result["coverage"]["prompt_bytes"] < 8192
        assert result["coverage"]["prompt_delivered_to_runtime"] is True
        assert result["coverage"]["read_unavailable"] == 1
        assert result["coverage"]["source_bytes_delivered"] == 0
        assert "text" not in reviewer.instances[0].response
        assert any("unavailable" in text for text in result["limitations"])


@pytest.mark.asyncio
async def test_denied_source_cannot_back_a_guessed_finding(tmp_path):
    reviewer = ReviewFactory("claim_unread")
    async with harness(tmp_path, reviewer=reviewer, read_approval=True) as (service, _, _, _, session):
        await setup(service, session, tmp_path)
        review = await start(service, session)
        await wait_status(service, review["id"], "waiting_approval")
        approval = (await service.list_review_approvals(user_id=2, review_id=review["id"]))[0]
        await service.resolve_review_approval(user_id=2, review_id=review["id"], approval_id=approval["id"], approved=False)
        result = await done(service, review)
        assert result["error_code"] == "review_output_invalid"
        assert result["coverage"]["output_validation_error"] == "unread_side"
        assert not result["findings"] and result["coverage"]["source_bytes_delivered"] == 0


@pytest.mark.asyncio
async def test_retained_patch_is_explicit_and_source_side_still_backs_finding(tmp_path):
    reviewer = ReviewFactory("patch_read")
    async with harness(tmp_path, reviewer=reviewer) as (service, _, _, _, session):
        await setup(service, session, tmp_path)
        result = await done(service, await start(service, session))
        assert result["status"] == "completed" and len(result["findings"]) == 1
        patch = await service.review_capture_diff(user_id=2, capture_id=result["capture_id"], path="file.txt")
        assert reviewer.instances[0].patch_response["text"].endswith(patch["patch"])
        assert "retained unified diff" in reviewer.instances[0].patch_response["text"]
        assert result["coverage"]["successful_captured_reads"] == 2


@pytest.mark.asyncio
async def test_review_read_cannot_exceed_remaining_byte_budget_or_return_prefix(tmp_path):
    reviewer = ReviewFactory("large_read")
    async with harness(tmp_path, reviewer=reviewer) as (service, _, _, _, session):
        source, checkout = await setup(service, session, tmp_path)
        # Context must be tracked; unselected untracked files are never admitted.
        (source / "large.txt").write_text("context\n" * 9000)
        await service._repository._git(source, "add", "large.txt")
        await service._repository._git(source, "commit", "-m", "Context fixture")
        (Path(checkout.canonical_path) / "large.txt").write_text("context\n" * 9000)
        await service._repository._git(Path(checkout.canonical_path), "add", "large.txt")
        result = await done(service, await start(service, session))
        assert result["status"] == "completed", result
        response = reviewer.instances[0].response
        assert response["status"] == "unavailable" and "text" not in response
        assert result["coverage"]["read_unavailable"] == 1
        assert result["coverage"]["read_result_truncated"] is False


@pytest.mark.asyncio
async def test_profile_revocation_is_checked_at_approval_not_only_by_the_watchdog(tmp_path):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer, read_approval=True) as (service, sessions, _, _, session):
        await setup(service, session, tmp_path)
        review = await start(service, session)
        await wait_status(service, review["id"], "waiting_approval")
        approval = (await service.list_review_approvals(user_id=2, review_id=review["id"]))[0]
        async with sessions() as db, db.begin():
            db.add(UserModelAccess(user_id=2, model_profile_id=1, allowed=False))
        with pytest.raises(CodingServiceError):
            await service.resolve_review_approval(user_id=2, review_id=review["id"], approval_id=approval["id"], approved=True)
        assert reviewer.instances[0].response is None
        slot = service._reviews[review["id"]]
        await asyncio.wait_for(asyncio.shield(slot.task), 3)
        assert reviewer.instances[0].closed


@pytest.mark.asyncio
async def test_missing_captured_read_never_falls_back_to_the_live_checkout(tmp_path):
    reviewer = ReviewFactory()
    async with harness(tmp_path, reviewer=reviewer, read_approval=True) as (service, sessions, _, _, session):
        _, checkout = await setup(service, session, tmp_path)
        review = await start(service, session)
        await wait_status(service, review["id"], "waiting_approval")
        approval = (await service.list_review_approvals(user_id=2, review_id=review["id"]))[0]
        capture = await service.get_review_capture(user_id=2, capture_id=review["capture_id"])
        blob = service._capture_storage.directory(2, review["capture_id"]) / capture["files"][0]["after"]["sha256"]
        blob.unlink()
        (Path(checkout.canonical_path) / "file.txt").write_text("LIVE_REPLACEMENT_MUST_NOT_BE_READ\n")
        await service.resolve_review_approval(user_id=2, review_id=review["id"], approval_id=approval["id"], approved=True)
        result = await done(service, review)
        assert result["status"] == "failed" and result["error_code"] == "capture_corrupt"
        assert reviewer.instances[0].response is None and reviewer.instances[0].closed


@pytest.mark.asyncio
async def test_disabled_reviews_do_not_query_review_tables(tmp_path):
    async with api_harness(tmp_path, review=True) as (client, _, service, sessions, _, _, session, _):
        async with sessions.kw["bind"].begin() as connection:
            for table in (CodingReviewApproval, CodingReviewEvent, CodingReviewFinding, CodingReview):
                await connection.run_sync(lambda conn, table=table: table.__table__.drop(conn))
        assert not (await client.get("/coding/status")).json()["ai_reviews"]
        response = await client.post(f"/coding/sessions/{session.id}/reviews",
            json={"paths": ["file.txt"], "client_request_id": "off", "model_profile_id": 1})
        assert response.status_code == 409


def test_output_budget_and_finding_bound_are_not_successful_empty_reviews():
    from app.coding_runtime.contracts import CodingRuntimeError
    parser = ReviewOutput({("file.txt", "after"): "return None\n"})
    with pytest.raises(CodingRuntimeError):
        list(parser.feed("x" * (64 * 1024 + 1)))
    parser = ReviewOutput({("file.txt", "after"): "return None\n"})
    for _ in range(100):
        list(parser.feed(json.dumps(FINDING) + "\n"))
    with pytest.raises(CodingRuntimeError):
        list(parser.feed(json.dumps(FINDING) + "\n"))


def test_review_migration_matches_metadata_and_refuses_to_delete_history(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    names = {"coding_reviews", "coding_review_findings", "coding_review_events", "coding_review_approvals"}
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables if table.name not in names])
    migration = import_module("app.db.migrations.versions.059_coding_reviews")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
        inspector = inspect(connection)
        for name in names:
            expected = Base.metadata.tables[name]
            assert set(expected.columns.keys()) == {column["name"] for column in inspector.get_columns(name)}
            assert {index.name for index in expected.indexes} == {index["name"] for index in inspector.get_indexes(name)}
        migration.downgrade()
        migration.upgrade()
        connection.execute(CodingReviewEvent.__table__.insert().values(review_id="x", sequence=1, event_type="review.accepted", payload_json="{}", schema_version=1))
        with pytest.raises(RuntimeError, match="operator inspection"):
            migration.downgrade()
    engine.dispose()
