"""Owner-scoped review history, Stop, approvals, dispositions and event replay."""
import asyncio
import json

from sqlalchemy import case, select

from app.coding_runtime.review_admission import failure
from app.coding_runtime.review_service import now
from app.coding_runtime.review_supervisor import REVIEW_ACTIVE, REVIEW_TERMINAL
from app.db.models import CodingReview, CodingReviewApproval, CodingReviewEvent, CodingReviewFinding, CodingReviewCapture, CodingCheckoutReservation


class ReviewAccess:
    def _review_metadata(self, row):
        return {"id": row.id, "session_id": row.session_id, "checkout_id": row.checkout_id,
            "capture_id": row.capture_id, "model_profile_id": row.model_profile_id,
            "model": json.loads(row.model_json), "reasoning": json.loads(row.reasoning_json),
            "coverage": json.loads(row.coverage_json or "{}"), "limitations": json.loads(row.limitations_json or "[]"),
            "summary": row.summary, "status": row.status, "error_code": row.error_code,
            "last_event_sequence": row.last_event_sequence, "created_at": row.created_at, "finished_at": row.finished_at}

    async def _owned_review(self, db, user_id, review_id):
        self._reviews_enabled()
        await self._require_user(db, user_id)
        row = await db.get(CodingReview, review_id)
        if row is None or row.user_id != user_id:
            raise failure("not_found", "Review not found")
        await self._session(db, user_id, row.session_id)
        capture = await db.get(CodingReviewCapture, row.capture_id)
        await self._workspace(db, user_id, capture.workspace_id)
        return row

    async def list_reviews(self, *, user_id, session_id, limit=50, offset=0):
        self._reviews_enabled()
        self._validate_page(limit, offset)
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            session = await self._session(db, user_id, session_id)
            await self._workspace(db, user_id, session.workspace_id)
            rows = list((await db.execute(select(CodingReview).where(CodingReview.session_id == session_id,
                CodingReview.user_id == user_id).order_by(CodingReview.created_at.desc(), CodingReview.id).offset(offset).limit(limit))).scalars())
            return [self._review_metadata(row) for row in rows]

    async def get_review(self, *, user_id, review_id, include_content=True):
        async with self._db_lock, self._sessions() as db:
            row = await self._owned_review(db, user_id, review_id)
            findings = list((await db.execute(select(CodingReviewFinding).where(CodingReviewFinding.review_id == review_id)
                .order_by(case({"blocker": 0, "should_fix": 1, "consider": 2}, value=CodingReviewFinding.severity), CodingReviewFinding.id).limit(100))).scalars())
            metadata = self._review_metadata(row)
            metadata["findings"] = [self._finding_metadata(item) for item in findings]
        if include_content:
            metadata["capture"] = await self.get_review_capture(user_id=user_id, capture_id=row.capture_id)
        async with self._db_lock, self._sessions() as db:
            await self._owned_review(db, user_id, review_id)
        return metadata

    @staticmethod
    def _finding_metadata(row):
        return {key: getattr(row, key) for key in ("id", "capture_id", "severity", "title", "body", "path", "side",
            "line_start", "line_end", "evidence", "disposition", "reason", "updated_by_user_id", "updated_at")}

    async def set_finding_disposition(self, *, user_id, review_id, finding_id, disposition, reason=None):
        if disposition not in {"open", "dismissed"} or (disposition == "dismissed" and (not isinstance(reason, str) or not reason.strip())) or (reason is not None and (not isinstance(reason, str) or len(reason) > 2000)):
            raise failure("invalid_request", "Dismissal requires a nonempty reason of at most 2000 characters")
        async with self._db_lock, self._sessions() as db, db.begin():
            review = await self._owned_review(db, user_id, review_id)
            row = await db.get(CodingReviewFinding, finding_id)
            if row is None or row.review_id != review.id:
                raise failure("not_found", "Finding not found in this review")
            row.disposition, row.reason, row.updated_by_user_id, row.updated_at = disposition, reason, user_id, now()
            self._review_event(db, review, "finding.disposition_changed", {"finding_id": finding_id,
                "disposition": disposition, "reason": reason, "actor_id": user_id})
            return self._finding_metadata(row)

    async def list_review_approvals(self, *, user_id, review_id):
        async with self._db_lock, self._sessions() as db:
            await self._owned_review(db, user_id, review_id)
            rows = list((await db.execute(select(CodingReviewApproval).where(CodingReviewApproval.review_id == review_id)
                .order_by(CodingReviewApproval.created_at, CodingReviewApproval.id).limit(128))).scalars())
            return [{"id": row.id, "review_id": review_id, "status": row.status, "created_at": row.created_at,
                "resolved_at": row.resolved_at, "expires_at": None, **json.loads(row.payload_json)} for row in rows]

    async def resolve_review_approval(self, *, user_id, review_id, approval_id, approved):
        if not isinstance(approved, bool):
            raise failure("invalid_request", "Choose approve or deny")
        async with self._lock, self._db_lock, self._sessions() as db, db.begin():
            review = await self._owned_review(db, user_id, review_id)
            row = await db.get(CodingReviewApproval, approval_id)
            if row is None or row.review_id != review_id:
                raise failure("not_found", "Review approval not found")
            if row.status != "pending":
                return
            slot = self._reviews.get(review_id)
            future = slot.approvals.get(approval_id) if slot is not None else None
            if future is None or future.done() or slot.stop or review.status != "waiting_approval":
                raise failure("run_inactive", "Review is no longer waiting for this approval")
            await self._authorize_review_run(db, review)
            row.status, row.resolved_at = "approved" if approved else "denied", now()
            self._review_event(db, review, "review.approval_resolved", {"approval_id": approval_id, "approved": approved})
        # Only wake after the decision has committed. No model/DB transaction is
        # held while waiting for the human or while delivering captured content.
        if not future.done():
            future.set_result(approved)

    async def stop_review(self, *, user_id, review_id):
        async with self._lock, self._db_lock, self._sessions() as db, db.begin():
            review = await self._owned_review(db, user_id, review_id)
            if review.status in REVIEW_TERMINAL:
                return False
            slot = self._reviews.get(review_id)
            if slot is None:
                raise failure("run_inactive", "Review process is unavailable; recovery is required")
            if not slot.stop:
                review.status, slot.stop = "stopping", True
                self._review_event(db, review, "review.stopping", {})
        if slot.stop_task is None:
            self._cancel_review_slot(slot)
            slot.stop_task = asyncio.create_task(self._join_stopped_review(review_id, slot))
            slot.stop_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(slot.stop_task)
        return True

    async def _join_stopped_review(self, review_id, slot):
        await asyncio.gather(slot.task, return_exceptions=True)
        if review_id in self._reviews and not slot.quarantined:
            await self._finish_review(review_id, slot, "cancelled", "user_stop")
        if slot.quarantined:
            raise failure("cleanup_failed", "Reviewer could not be closed; checkout remains reserved")

    async def review_events(self, *, user_id, review_id, after=0, follow=False):
        from app.coding_runtime.service import StoredCodingEvent
        if after < 0:
            raise failure("invalid_request", "Review event cursor must be nonnegative")
        while True:
            async with self._db_lock, self._sessions() as db:
                row = await self._owned_review(db, user_id, review_id)
                if after > row.last_event_sequence:
                    raise failure("cursor_ahead", "Review cursor is ahead of saved history")
                events = list((await db.execute(select(CodingReviewEvent).where(CodingReviewEvent.review_id == review_id,
                    CodingReviewEvent.sequence > after).order_by(CodingReviewEvent.sequence).limit(100))).scalars())
                terminal = row.status in REVIEW_TERMINAL
            for event in events:
                after = event.sequence
                yield StoredCodingEvent(event.sequence, event.event_type, json.loads(event.payload_json), event.schema_version)
            if events:
                continue
            if terminal or not follow:
                return
            await asyncio.sleep(0.25)

    async def _capture_is_pinned(self, capture_id):
        if self._review_factory is None:
            return False
        async with self._db_lock, self._sessions() as db:
            held = select(CodingCheckoutReservation.holder_id).where(CodingCheckoutReservation.holder_kind == "review")
            return await db.scalar(select(CodingReview.id).where(CodingReview.capture_id == capture_id,
                CodingReview.status.in_(REVIEW_ACTIVE) | CodingReview.id.in_(held)).limit(1)) is not None
