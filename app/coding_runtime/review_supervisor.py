"""Fresh-context, capture-only reviewer supervision. No builder process reuse."""
import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import timedelta
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from sqlalchemy import func, select, update

from app.coding_runtime.contracts import CodingPrompt, CodingRuntimeError
from app.coding_runtime.review_admission import failure
from app.coding_runtime.review_budget import input_byte_budget
from app.coding_runtime.review_input import captured_read, review_prompt
from app.coding_runtime.review_capture import SCOPES, digest, encode
from app.coding_runtime.review_output import ReviewMessages, ReviewOutput, VALIDATION_MESSAGES
from app.coding_runtime.review_runtime import ReviewRuntimeStart, effort_choices, resolve_effort
from app.coding_runtime.review_service import now, OWNER_BYTES, RESERVED_BYTES
from app.coding_runtime.run_deadline import RunDeadline
from app.coding_runtime.repository import RepositoryError
from app.db.models import (CodingReview, CodingReviewApproval, CodingReviewCapture, CodingReviewEvent,
                           CodingReviewFinding, ModelProfile)

REVIEW_ACTIVE = {"capturing", "starting", "running", "waiting_approval", "stopping"}
REVIEW_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
READ_PROTOCOL = "fruitcake.review_read.v1"


@dataclass
class ReviewSlot:
    token: str
    task: asyncio.Task | None = None
    runtime: object = None
    phase: str = "capturing"
    stop: bool = False
    error: str | None = None
    approvals: dict = field(default_factory=dict)
    deadline: object = None
    quarantined: bool = False
    view: object = None
    read_count: int = 0
    cancel_requested: bool = False
    stop_task: asyncio.Task | None = None


class ReviewSupervisor:
    def _init_reviews(self, factory, require_read_approval):
        if factory is not None and self._capture_storage is None:
            raise ValueError("Review execution requires retained capture storage")
        self._review_factory = factory
        self._review_read_approval = require_read_approval
        self._reviews = {}

    @property
    def supports_ai_reviews(self):
        return self._review_factory is not None and self.supports_review_captures

    def _reviews_enabled(self):
        if not self.supports_ai_reviews:
            raise failure("unsupported", "AI reviewer execution is disabled")

    @staticmethod
    def _cancel_review_slot(slot):
        if not slot.cancel_requested:
            slot.cancel_requested = True
            slot.task.cancel()

    async def _review_model(self, db, user_id, profile_id, effort):
        model, tools = await self._model(db, user_id, profile_id)
        profile = await db.get(ModelProfile, profile_id)
        if "read" not in tools or "fruitcake_review_read" in profile.blocked_tools:
            raise failure("model_denied", "Reviewer requires an authorized read tool")
        if not model.is_local or model.provider_family not in {"ollama", "local", "openai_compat"}:
            raise failure("model_denied", "This reviewer adapter supports authorized local profiles only")
        try:
            resolved = resolve_effort(profile, effort)
        except CodingRuntimeError as exc:
            raise failure(exc.code, str(exc)) from None
        return model, profile.supports_thinking, resolved

    async def start_review(self, *, user_id, session_id, model_profile_id, scope, paths, client_request_id, effort="default"):
        self._reviews_enabled()
        if scope not in SCOPES or not isinstance(paths, list) or not 1 <= len(paths) <= 128 or any(
            not isinstance(path, str) or not path or len(path.encode()) > 1024 for path in paths
        ) or len(set(paths)) != len(paths) or not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 100:
            raise failure("invalid_request", "Choose distinct capture paths, scope and an idempotency key")
        request_hash = digest(encode({"model_profile_id": model_profile_id, "effort": effort, "scope": scope, "paths": sorted(paths)}))
        async with self._lock, self._db_lock, self._sessions() as db, db.begin():
            await self._require_user(db, user_id)
            session = await self._session(db, user_id, session_id)
            await self._workspace(db, user_id, session.workspace_id)
            previous = await db.scalar(select(CodingReview).where(CodingReview.session_id == session_id,
                                                                 CodingReview.client_request_id == client_request_id))
            if previous is not None:
                if previous.request_hash != request_hash:
                    raise failure("request_conflict", "Review key was used for a different request")
                return self._review_metadata(previous)
            if self._closing:
                raise failure("shutting_down", "Coding service is shutting down")
            _, _, workspace, checkout = await self._capture_context(db, user_id, session_id, starting=True, check_model=False)
            model, thinking, resolved = await self._review_model(db, user_id, model_profile_id, effort)
            if len(self._slots) + len(self._reviews) >= self.limits.max_active_sessions or self._active_capture_count() >= 2:
                raise failure("capacity_reached", "Reviewer capacity is busy; no work was queued")
            count = await db.scalar(select(func.count()).select_from(CodingReviewCapture).where(
                CodingReviewCapture.checkout_id == checkout.id, CodingReviewCapture.status != "expired"))
            size = await db.scalar(select(func.coalesce(func.sum(CodingReviewCapture.stored_bytes), 0)).where(CodingReviewCapture.user_id == user_id))
            if count >= 10 or size + RESERVED_BYTES > OWNER_BYTES:
                raise failure("capture_limit", "Capture retention quota is full")
            review_id, capture_id = str(uuid4()), str(uuid4())
            capture = CodingReviewCapture(id=capture_id, user_id=user_id, session_id=session_id, checkout_id=checkout.id,
                workspace_id=workspace.id, scope=scope, client_request_id="review-" + review_id, request_hash=request_hash,
                status="capturing", stored_bytes=RESERVED_BYTES, created_at=now(), expires_at=now()+timedelta(days=30))
            db.add(capture)
            await db.flush()
            row = CodingReview(id=review_id, user_id=user_id, session_id=session_id, checkout_id=checkout.id,
                capture_id=capture_id, client_request_id=client_request_id, request_hash=request_hash, model_profile_id=model_profile_id,
                model_json=json.dumps(asdict(model)), reasoning_json=json.dumps({"requested": effort, "resolved": resolved, "declared_thinking": thinking,
                    "sent": None, "rpc_acknowledged": False, "runtime_readback": None, "provider_confirmation": None}),
                status="capturing", last_event_sequence=0, created_at=now())
            db.add(row)
            await db.flush()
            token = await self._claim_checkout(db, checkout.id, "review", row.id)
            self._review_event(db, row, "review.accepted", {"capture_id": capture_id})
            root, baseline, source = self._checkout_path(checkout), checkout.baseline_commit, workspace.canonical_path
        slot = ReviewSlot(token)
        self._reviews[row.id] = slot
        slot.task = asyncio.create_task(self._execute_review(row, capture, checkout, root, baseline, source, paths,
            model, thinking, resolved, slot), name=f"coding-review-{row.id}")
        return self._review_metadata(row)

    def _review_event(self, db, row, kind, payload):
        if len(encode(payload)) > 16 * 1024 or row.last_event_sequence >= 2000:
            raise failure("review_event_limit", "Reviewer event budget exceeded")
        row.last_event_sequence += 1
        db.add(CodingReviewEvent(review_id=row.id, sequence=row.last_event_sequence, event_type=kind,
                                payload_json=json.dumps(payload), schema_version=1))

    async def _authorize_review_run(self, db, row, model=None, effort=None):
        await self._require_user(db, row.user_id)
        session = await self._session(db, row.user_id, row.session_id)
        if session.status == "closed" or self._closing:
            raise failure("authorization_changed", "Reviewer conversation was closed")
        capture = await db.get(CodingReviewCapture, row.capture_id)
        await self._workspace(db, row.user_id, capture.workspace_id)
        recorded = json.loads(row.reasoning_json)
        current, thinking, resolved = await self._review_model(db, row.user_id, row.model_profile_id, recorded["requested"])
        if asdict(current) != json.loads(row.model_json) or resolved != recorded["resolved"] or thinking != recorded["declared_thinking"]:
            raise failure("authorization_changed", "Reviewer profile changed; start a fresh review")
        if not await self._owns_checkout_reservation(db, row.checkout_id, "review", row.id):
            raise failure("coding_owner_lost", "Reviewer checkout reservation was lost")

    async def _review_watch(self, row, model, slot):
        while True:
            await asyncio.sleep(1)
            try:
                async with self._db_lock, self._sessions() as db:
                    await self._authorize_review_run(db, row, model, json.loads(row.reasoning_json)["requested"])
            except Exception:
                slot.error = "authorization_changed"
                self._cancel_review_slot(slot)
                return

    def _materialize_review(self, capture, manifest, root):
        sides, entries = {}, {}
        for item in manifest["files"]:
            for side in ("before", "after"):
                if item[side] is not None:
                    sides[(item["path"], side)] = self._capture_storage.blob(capture.user_id, capture.id, item[side])
            if item["after"] is not None:
                entries[item["path"]] = item["after"]
        for item in manifest["context"]:
            entries[item["path"]] = item
        for path, entry in entries.items():
            text = self._capture_storage.blob(capture.user_id, capture.id, entry)
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._capture_storage.write(target, text.encode())
        return sides, entries

    async def _execute_review(self, row, capture, checkout, root, baseline, source, paths, model, thinking, resolved, slot):
        status, error = "failed", "review_incomplete"
        view, watcher, clean, messages = None, None, True, None
        coverage = {"files_in_scope": 0, "files_provided_in_prompt": 0, "successful_captured_reads": 0,
            "read_unavailable": 0, "read_denied": 0, "read_result_truncated": False, "bytes_delivered": 0,
            "semantic_coverage": "unknown", "tests_executed": False, "successful_read_files": [], "prompt_delivered_to_runtime": False}
        try:
            if slot.stop:
                raise asyncio.CancelledError
            await self._make_capture(capture, root, baseline, source, checkout, paths, slot.token, review_id=row.id)
            if slot.stop or self._closing:
                raise asyncio.CancelledError
            async with self._db_lock, self._sessions() as db, db.begin():
                capture = await db.get(CodingReviewCapture, capture.id)
                if capture.status != "ready":
                    raise CodingRuntimeError(capture.error_code or "capture_unavailable", "Review capture failed")
                await self._authorize_review_run(db, row, model, json.loads(row.reasoning_json)["requested"])
                current = await db.get(CodingReview, row.id)
                current.status = slot.phase = "starting"
                self._review_event(db, current, "capture.ready", {"capture_id": capture.id})
            manifest = await asyncio.to_thread(self._capture_storage.load, row.user_id, capture.id, capture.manifest_hash)
            view = tempfile.TemporaryDirectory(prefix=".review-view-", dir=self._capture_storage.root)
            slot.view = view
            materializing = asyncio.create_task(asyncio.to_thread(self._materialize_review, capture, manifest, Path(view.name)))
            try:
                sides, entries = await asyncio.shield(materializing)
            except asyncio.CancelledError:
                await materializing
                raise
            prompt = review_prompt(manifest, sides)
            budget = input_byte_budget(model)
            prompt_bytes = len(prompt.encode())
            coverage.update(files_in_scope=len(manifest["files"]), captured_context_files=len(manifest["context"]),
                input_byte_budget=budget, prompt_bytes=prompt_bytes, input_budget_basis="utf8_byte_per_token",
                source_delivery="captured_read_tools_only", source_bytes_delivered=0)
            if prompt_bytes > budget:
                raise CodingRuntimeError("review_input_limit", "Complete capture input exceeds this reviewer's context budget; narrow scope")
            async with self._db_lock, self._sessions() as db:
                await self._authorize_review_run(db, row, model, json.loads(row.reasoning_json)["requested"])
            slot.runtime = self._review_factory()
            capabilities = slot.runtime.capabilities()
            if not capabilities.read_only or capabilities.approved_edits or capabilities.container_commands:
                raise CodingRuntimeError("runtime_denied", "Reviewer runtime must be strictly read-only")
            await asyncio.wait_for(slot.runtime.start(ReviewRuntimeStart(Path(view.name), model, thinking, resolved)), self.limits.startup_seconds)
            provenance = getattr(slot.runtime, "provenance", {})
            if provenance.get("sent") != resolved or provenance.get("rpc_acknowledged") is not True or provenance.get("runtime_readback") != resolved:
                raise CodingRuntimeError("reasoning_mismatch", "Reviewer effort was not acknowledged and read back; no prompt was sent")
            watcher = asyncio.create_task(self._review_watch(row, model, slot))
            provided_sides = set()
            messages = ReviewMessages(ReviewOutput(sides, provided_sides=provided_sides))
            async with self._db_lock, self._sessions() as db, db.begin():
                await self._authorize_review_run(db, row, model, json.loads(row.reasoning_json)["requested"])
                current = await db.get(CodingReview, row.id)
                current.reasoning_json = json.dumps({**json.loads(current.reasoning_json), **provenance, "provider_confirmation": None})
                current.status = slot.phase = "running"
                current.limitations_json = json.dumps(["Read-only reviewer did not execute tests or commands",
                    *(["Capture excludes unavailable or private context"] if manifest["limited"] else [])])
                self._review_event(db, current, "review.started", {"capture_id": capture.id})
            slot.deadline = RunDeadline(self.limits.run_seconds)
            async with slot.deadline:
                async for event in slot.runtime.run(CodingPrompt(prompt)):
                    if slot.stop:
                        raise asyncio.CancelledError
                    if not coverage["prompt_delivered_to_runtime"]:
                        coverage.update(prompt_delivered_to_runtime=True, files_provided_in_prompt=0,
                                        patches_provided=0, bytes_delivered=prompt_bytes)
                    if event.kind == "interaction.requested":
                        await self._review_read(row, slot, event.payload, manifest, sides, coverage, provided_sides)
                    elif event.kind in {"operation.started", "operation.output", "operation.completed"} and event.payload.get("tool") != "fruitcake_review_read":
                        raise CodingRuntimeError("runtime_denied", "Reviewer requested a forbidden tool")
                    elif event.kind in {"assistant.text_delta", "assistant.message"}:
                        if event.kind == "assistant.text_delta":
                            records = messages.delta(event.payload.get("delta", ""))
                        else:
                            records = messages.message(event.payload.get("text", ""), event.payload.get("stop_reason"))
                        for record in records:
                            await self._review_record(row, record, slot)
                        coverage.update(output_bytes=messages.bytes, assistant_stop_reason=messages.stop_reason)
                    elif event.kind == "run.completed":
                        for record in messages.finish():
                            await self._review_record(row, record, slot)
                        if event.payload.get("aborted"):
                            raise CodingRuntimeError("review_incomplete", "Reviewer aborted before completion")
                        status, error = "completed", None
                        break
                    elif event.kind in {"run.failed", "session.failed", "runtime.protocol_error"}:
                        raise CodingRuntimeError("review_runtime_failed", "Reviewer runtime failed")
        except asyncio.CancelledError:
            status, error = ("interrupted", "backend_shutdown") if self._closing else ("failed", slot.error) if slot.error else ("cancelled", "user_stop") if slot.stop else ("interrupted", "backend_shutdown")
        except TimeoutError:
            error = "review_timeout"
        except Exception as exc:
            error = getattr(exc, "code", "review_failed")
            if error == "review_output_invalid":
                coverage["output_validation_error"] = getattr(exc, "validation_reason", "json_syntax")
            if error == "review_output_limit":
                coverage["output_limit_kind"] = "token_allowance" if messages and messages.stop_reason in {"length", "max_tokens"} else "record_or_byte_limit"
            if messages is not None:
                coverage.update(output_bytes=messages.bytes, assistant_stop_reason=messages.stop_reason)
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if slot.runtime is not None:
                try:
                    await asyncio.wait_for(slot.runtime.close(), self.limits.stop_seconds)
                except Exception:
                    clean, slot.quarantined, error, status = False, True, "cleanup_failed", "failed"
            if view is not None and clean:
                try:
                    await asyncio.to_thread(view.cleanup)
                    slot.view = None
                except Exception:
                    clean, slot.quarantined, error, status = False, True, "cleanup_failed", "failed"
            await self._finish_review(row.id, slot, status, error, coverage, release=clean)

    async def _review_record(self, review, record, slot):
        # Provider prose is not allowed to turn a private execution location
        # into a public navigation target. Anchored source evidence stays exact.
        def public_text(value):
            for private in (slot.view.name, str(self._capture_storage.root)):
                value = value.replace(private, "[private capture]")
            return value
        record = {**record}
        for key in ("title", "body", "summary"):
            if key in record:
                record[key] = public_text(record[key])
        if "limitations" in record:
            record["limitations"] = [public_text(value) for value in record["limitations"]]
        async with self._db_lock, self._sessions() as db, db.begin():
            await self._authorize_review_run(db, review)
            row = await db.get(CodingReview, review.id)
            if record["type"] == "finding":
                finding = CodingReviewFinding(id=str(uuid4()), review_id=row.id, capture_id=row.capture_id,
                    **{key: value for key, value in record.items() if key != "type"})
                db.add(finding)
                self._review_event(db, row, "review.finding", {"finding_id": finding.id})
            else:
                row.summary = record["summary"]
                row.limitations_json = json.dumps([*json.loads(row.limitations_json), *record["limitations"]])

    async def _review_read(self, row, slot, payload, manifest, sides, coverage, provided_sides):
        slot.read_count += 1
        if slot.read_count > 128:
            raise CodingRuntimeError("review_read_limit", "Captured read request limit reached")
        if payload.get("method") != "input" or payload.get("title") != "Read captured review file?":
            raise CodingRuntimeError("runtime_denied", "Reviewer requested an unsupported interaction")
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise CodingRuntimeError("review_protocol_invalid", "Invalid captured read request")
        try:
            proposal = json.loads(payload["placeholder"])
            if set(proposal) != {"protocol", "operation_id", "path", "side"} or proposal["protocol"] != READ_PROTOCOL:
                raise ValueError
            path, side, operation_id = proposal["path"], proposal["side"], proposal["operation_id"]
            if not isinstance(path, str) or len(path.encode()) > 1024 or side not in {"before", "after", "patch"} or not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise CodingRuntimeError("review_protocol_invalid", "Invalid captured read proposal") from None
        reference = next((item.get(side) for item in manifest["files"] if item["path"] == path), None)
        if reference is None and side == "after":
            reference = next((item for item in manifest["context"] if item["path"] == path), None)
        result = {"protocol": READ_PROTOCOL, "operation_id": operation_id, "status": "unavailable", "error": "Captured path/side unavailable"}
        public_path = path if reference is not None else "[unavailable captured path]"
        approved = True
        async with self._db_lock, self._sessions() as db, db.begin():
            await self._authorize_review_run(db, row)
            current = await db.get(CodingReview, row.id)
            self._review_event(db, current, "operation.started", {"operation_id": operation_id, "tool": "fruitcake_review_read", "path": public_path, "side": side})
            if reference is not None and self._review_read_approval:
                if len(slot.approvals) >= 128:
                    raise CodingRuntimeError("review_read_limit", "Captured read request limit reached")
                approval = CodingReviewApproval(id=str(uuid4()), review_id=row.id, runtime_request_id=request_id,
                    payload_json=json.dumps({"path": path, "side": side, "operation_id": operation_id}), status="pending")
                db.add(approval)
                future = asyncio.get_running_loop().create_future()
                slot.approvals[approval.id] = future
                slot.deadline.pause(approval.id)
                current.status = "waiting_approval"
                self._review_event(db, current, "review.waiting_approval", {"approval_id": approval.id, "path": path, "side": side})
            else:
                future = None
        if future is not None:
            try:
                approved = await future
            finally:
                slot.deadline.resolve(approval.id)
                slot.approvals.pop(approval.id, None)
        async with self._db_lock, self._sessions() as db:
            await self._authorize_review_run(db, row)
        if not approved:
            coverage["read_denied"] += 1
            result["error"] = "Captured read denied"
        elif reference is not None:
            try:
                text = await asyncio.to_thread(self._capture_storage.blob, row.user_id, row.capture_id, reference)
            except (OSError, ValueError, RepositoryError):
                raise CodingRuntimeError("capture_corrupt", "Captured read failed integrity validation; no live file was substituted") from None
            presentation = captured_read(path, side, text)
            if coverage["successful_captured_reads"] >= 128 or coverage["bytes_delivered"] + len(presentation.encode()) > coverage["input_byte_budget"]:
                result["error"] = "Complete read exceeds reviewer input budget; no prefix was substituted"
                coverage["read_unavailable"] += 1
            else:
                result.update(status="completed", text=presentation, source_bytes=len(text.encode()), presentation_bytes=len(presentation.encode()))
        else:
            coverage["read_unavailable"] += 1
        await slot.runtime.answer_read(request_id, result)
        if result["status"] == "completed":
            if (path, side) in sides:
                provided_sides.add((path, side))
            coverage["bytes_delivered"] += len(result["text"].encode())
            coverage["source_bytes_delivered"] += result["source_bytes"]
            coverage["successful_captured_reads"] += 1
            identity = {"path": path, "side": side}
            if identity not in coverage["successful_read_files"]:
                coverage["successful_read_files"].append(identity)
        async with self._db_lock, self._sessions() as db, db.begin():
            await self._authorize_review_run(db, row)
            current = await db.get(CodingReview, row.id)
            current.status = "running"
            self._review_event(db, current, "operation.completed", {"operation_id": operation_id, "path": public_path,
                "side": side, "status": result["status"], "bytes": len(result.get("text", "").encode()),
                "source_bytes": result.get("source_bytes", 0), "truncated": False})

    async def _finish_review(self, review_id, slot, status, error, coverage=None, *, release=True):
        async with self._db_lock, self._sessions() as db, db.begin():
            if not await self._owns_checkout_reservation(db, (await db.get(CodingReview, review_id)).checkout_id, "review", review_id, slot.token):
                return
            row = await db.get(CodingReview, review_id)
            if row.status in REVIEW_ACTIVE or row.error_code == "cleanup_failed":
                row.status, row.error_code, row.finished_at = status, str(error)[:50] if error else None, now()
                if coverage is not None:
                    row.coverage_json = json.dumps(coverage)
                if error == "review_input_limit" and coverage is not None:
                    row.limitations_json = json.dumps([*json.loads(row.limitations_json),
                        f"Complete review prompt requires {coverage.get('prompt_bytes', 0):,} UTF-8 bytes; "
                        f"this reviewer's conservative input allowance is {coverage.get('input_byte_budget', 0):,} bytes "
                        "after output/protocol reserves. Select fewer files or a reviewer with a larger configured context. "
                        "No model prompt was sent or source truncated."])
                if error == "review_output_invalid" and coverage is not None:
                    reason = coverage.get("output_validation_error")
                    row.limitations_json = json.dumps([*json.loads(row.limitations_json),
                        VALIDATION_MESSAGES.get(reason, "Reviewer output did not satisfy the structured report contract")])
                if error == "review_output_limit" and coverage and coverage.get("output_limit_kind") == "token_allowance":
                    row.limitations_json = json.dumps([*json.loads(row.limitations_json),
                        "Reviewer reached its configured output token allowance before completing the report. "
                        "Use a narrower scope or deliberately increase the review model profile's output reserve."])
                if coverage and (coverage["read_unavailable"] or coverage["read_denied"]):
                    row.limitations_json = json.dumps([*json.loads(row.limitations_json), "Some captured reads were unavailable or denied"])
                if status != "completed":
                    row.limitations_json = json.dumps([*json.loads(row.limitations_json), "Review is incomplete; retained findings are partial"])
                await db.execute(update(CodingReviewApproval).where(CodingReviewApproval.review_id == row.id,
                    CodingReviewApproval.status == "pending").values(status="cancelled", resolved_at=now()))
                capture = await db.get(CodingReviewCapture, row.capture_id)
                if capture.status == "capturing":
                    capture.status, capture.error_code, capture.finished_at = "interrupted", error, now()
                # Leave room for one terminal event after the normal event cap.
                if row.last_event_sequence < 2000:
                    self._review_event(db, row, f"review.{status}", {"error_code": row.error_code})
            if release:
                await self._release_checkout(db, "review", row.id, slot.token)
        if release:
            self._reviews.pop(review_id, None)

    async def _recover_reviews(self, db):
        if self._review_factory is not None:
            rows = list((await db.execute(select(CodingReview).where(CodingReview.status.in_(REVIEW_ACTIVE)))).scalars())
            for row in rows:
                row.status, row.error_code, row.finished_at = "interrupted", "backend_restart", now()
                row.limitations_json = json.dumps([*json.loads(row.limitations_json), "Interrupted review; findings are partial"])
                if row.last_event_sequence < 2000:
                    self._review_event(db, row, "review.interrupted", {"error_code": "backend_restart"})
            if rows:
                await db.execute(update(CodingReviewApproval).where(CodingReviewApproval.review_id.in_([row.id for row in rows]),
                    CodingReviewApproval.status == "pending").values(status="cancelled", resolved_at=now()))

    async def _shutdown_reviews(self):
        slots = list(self._reviews.items())
        for _, slot in slots:
            self._cancel_review_slot(slot)
        await asyncio.shield(asyncio.gather(*(slot.task for _, slot in slots), return_exceptions=True))
        for review_id, slot in slots:
            if slot.quarantined:
                if slot.runtime is not None:
                    await asyncio.wait_for(slot.runtime.close(), self.limits.stop_seconds)
                if slot.view is not None:
                    await asyncio.to_thread(slot.view.cleanup)
            if review_id in self._reviews:
                await self._finish_review(review_id, slot, "interrupted", "backend_shutdown")
