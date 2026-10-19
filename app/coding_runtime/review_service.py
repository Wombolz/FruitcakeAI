"""Capture-only foundation. No model process or reviewer is launched here."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from app.db.models import CodingCheckoutReservation, CodingReviewCapture
from app.coding_runtime.review_admission import SupervisorFence, claim, failure, release
from app.coding_runtime.review_capture import CaptureBuilder, CaptureStorage, SCOPES, digest, encode
from app.coding_runtime.repository import RepositoryError

RESERVED_BYTES = 38 * 1024 * 1024
OWNER_BYTES = 256 * 1024 * 1024


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ReviewCaptureService:
    def _init_captures(self, root):
        self._capture_storage = CaptureStorage(root) if root is not None else None
        self._capture_builder = CaptureBuilder()
        self._capture_jobs = {}
        self._capture_epoch = str(uuid4())
        # Even feature-disabled instances must not recover another supervisor's work.
        self._capture_fence = SupervisorFence(self._sessions)
        self._capture_started = False
        self._supervisor_started = False
        self._supervisor_start_lock = asyncio.Lock()

    @property
    def supports_review_captures(self):
        return self._capture_storage is not None and self._capture_started

    def _captures_enabled(self):
        if not self.supports_review_captures:
            raise failure("unsupported", "Review captures are disabled; AI review execution is not implemented")

    async def _recover_captures(self, db):
        if self._capture_storage is not None:
            await db.execute(update(CodingReviewCapture).where(CodingReviewCapture.status == "capturing").values(
                status="interrupted", error_code="backend_restart", finished_at=now()))
            # Only called while holding the sole-supervisor fence, before serving.
            await db.execute(delete(CodingCheckoutReservation))

    async def _claim_checkout(self, db, checkout_id, kind, holder_id):
        if self._capture_storage is not None:
            return await claim(db, checkout_id, kind, holder_id, self._capture_epoch)

    async def _release_checkout(self, db, kind, holder_id, token=None):
        if self._capture_storage is not None:
            await release(db, kind, holder_id, self._capture_epoch, token=token)

    async def _owns_checkout_reservation(self, db, checkout_id, kind, holder_id, token=None):
        if self._capture_storage is None:
            return True
        row = await db.get(CodingCheckoutReservation, checkout_id)
        return row is not None and row.service_epoch == self._capture_epoch and row.holder_kind == kind and row.holder_id == holder_id and (token is None or row.token == token)

    def _active_capture_count(self):
        return len(self._capture_jobs) + sum(slot.phase == "capturing" for slot in self._reviews.values())

    async def _capture_context(self, db, user_id, session_id, *, starting=False, check_model=True):
        user = await self._require_user(db, user_id)
        session = await self._session(db, user_id, session_id)
        workspace = await self._workspace(db, user_id, session.workspace_id)
        checkout = await self._checkout(db, session_id)
        if checkout is None or checkout.user_id != user_id:
            raise failure("capture_unsupported", "Review capture requires an owned managed checkout")
        if starting:
            if session.status not in {"idle", "interrupted"} or self._closing:
                raise failure("checkout_busy", "Finish the current run before capturing a review")
            await self._assert_checkout_idle(db, checkout)
            if check_model:
                await self._model(db, user_id, session.model_profile_id)
        return user, session, workspace, checkout

    async def review_options(self, *, user_id, session_id, scope="checkout_changes"):
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            session = await self._session(db, user_id, session_id)
            await self._workspace(db, user_id, session.workspace_id)
            if not self.supports_review_captures:
                return {"captures_enabled": False, "ai_reviews": False, "reasoning_efforts": []}
            _, _, workspace, checkout = await self._capture_context(db, user_id, session_id)
            root, baseline = self._checkout_path(checkout), checkout.baseline_commit
            busy = checkout.status != "ready" or await db.get(CodingCheckoutReservation, checkout.id) is not None
            source = workspace.canonical_path
        try:
            await self._checkouts.validate_ready(Path(source), root, baseline,
                head_commit=checkout.head_commit, branch_name=checkout.branch_name)
            result = await self._capture_builder.options(root, baseline, scope)
        except RepositoryError as exc:
            raise failure(exc.code, str(exc)) from exc
        except (OSError, ValueError, TimeoutError):
            raise failure("capture_unavailable", "Repository options could not be observed safely") from None
        async with self._db_lock, self._sessions() as db:
            await self._capture_context(db, user_id, session_id)
            profiles = []
            if self.supports_ai_reviews:
                from app.coding_runtime.review_runtime import effort_choices
                from app.db.models import ModelProfile
                for profile in (await db.execute(select(ModelProfile).where(ModelProfile.enabled.is_(True)).order_by(ModelProfile.id).limit(100))).scalars():
                    try:
                        await self._review_model(db, user_id, profile.id, "default")
                    except Exception:
                        continue
                    profiles.append({"id": profile.id, "label": profile.display_name, "model_id": profile.model_id,
                                     "reasoning_efforts": effort_choices(profile)})
        return {**result, "captures_enabled": True, "ai_reviews": self.supports_ai_reviews, "reasoning_efforts": [],
                "reviewer_profiles": profiles,
                "busy": busy, "content_retention_days": 30}

    async def create_review_capture(self, *, user_id, session_id, scope, paths, client_request_id):
        self._captures_enabled()
        if scope not in SCOPES or not isinstance(paths, list) or not 1 <= len(paths) <= 128 or any(
            not isinstance(path, str) or not path or len(path.encode()) > 1024 for path in paths
        ) or len(paths) != len(set(paths)) or not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 100:
            raise failure("invalid_request", "Choose a scope, distinct changed paths and an idempotency key")
        request_hash = digest(encode({"scope": scope, "paths": sorted(paths)}))
        async with self._lock, self._db_lock, self._sessions() as db, db.begin():
            # Replays still reauthorize, but may succeed while the original is active.
            await self._require_user(db, user_id)
            session = await self._session(db, user_id, session_id)
            await self._workspace(db, user_id, session.workspace_id)
            previous = (await db.execute(select(CodingReviewCapture).where(
                CodingReviewCapture.session_id == session_id,
                CodingReviewCapture.client_request_id == client_request_id))).scalar_one_or_none()
            if previous:
                if previous.request_hash != request_hash:
                    raise failure("request_conflict", "Capture request key was used for different inputs")
                return self._capture_metadata(previous)
            _, session, workspace, checkout = await self._capture_context(db, user_id, session_id, starting=True)
            if self._active_capture_count() >= 2:
                raise failure("capacity_reached", "Capture workers are busy; no work was queued")
            count = await db.scalar(select(func.count()).select_from(CodingReviewCapture).where(
                CodingReviewCapture.checkout_id == checkout.id,
                CodingReviewCapture.status != "expired"))
            size = await db.scalar(select(func.coalesce(func.sum(CodingReviewCapture.stored_bytes), 0)).where(
                CodingReviewCapture.user_id == user_id))
            if count >= 10 or size + RESERVED_BYTES > OWNER_BYTES:
                raise failure("capture_limit", "Retained capture quota is full; no evidence was evicted")
            capture = CodingReviewCapture(id=str(uuid4()), user_id=user_id, session_id=session_id,
                checkout_id=checkout.id, workspace_id=workspace.id, client_request_id=client_request_id,
                request_hash=request_hash, scope=scope, status="capturing", stored_bytes=RESERVED_BYTES,
                created_at=now(), expires_at=now() + timedelta(days=30))
            db.add(capture)
            await db.flush()
            token = await self._claim_checkout(db, checkout.id, "capture", capture.id)
            root, baseline, source = self._checkout_path(checkout), checkout.baseline_commit, workspace.canonical_path
        task = asyncio.create_task(self._make_capture(capture, root, baseline, source, checkout, paths, token),
                                   name=f"coding-capture-{capture.id}")
        self._capture_jobs[capture.id] = task
        task.add_done_callback(lambda completed: self._capture_jobs.pop(capture.id, None))
        return self._capture_metadata(capture)

    async def _make_capture(self, capture, root, baseline, source, checkout, paths, token, review_id=None):
        status, error_code, stored_bytes, manifest_hash, summary = "failed", "capture_unavailable", 0, None, {}
        publish = None
        try:
            await self._checkouts.validate_ready(Path(source), root, baseline,
                head_commit=checkout.head_commit, branch_name=checkout.branch_name)
            identity = {"capture_id": capture.id, "user_id": capture.user_id, "session_id": capture.session_id,
                "workspace_id": capture.workspace_id, "checkout_id": capture.checkout_id}
            manifest, blobs = await self._capture_builder.capture(root, baseline, capture.scope, paths, identity)
            async with self._db_lock, self._sessions() as db:
                await self._capture_context(db, capture.user_id, capture.session_id)
            publish = asyncio.create_task(asyncio.to_thread(self._capture_storage.publish, capture.user_id, capture.id, manifest, blobs))
            try:
                manifest_hash, stored_bytes = await asyncio.shield(publish)
            except asyncio.CancelledError:
                manifest_hash, stored_bytes = await publish
                raise
            async with self._db_lock, self._sessions() as db:
                await self._capture_context(db, capture.user_id, capture.session_id)
            summary = {key: manifest[key] for key in ("base", "head", "fingerprint", "limited", "omission_counts")}
            summary["files_in_scope"], summary["context_files"] = len(manifest["files"]), len(manifest["context"])
            status, error_code = "ready", None
        except asyncio.CancelledError:
            status, error_code = "interrupted", "capture_interrupted"
        except RepositoryError as exc:
            error_code = exc.code
        except Exception:
            # No filesystem paths, Git stderr, credentials or provider output in API errors.
            error_code = "capture_unavailable"
        finally:
            if publish is not None and manifest_hash is None:
                # An error after rename may leave published bytes. Retain quota
                # accounting until expiry rather than assuming publication failed.
                stored_bytes = RESERVED_BYTES
            async with self._db_lock, self._sessions() as db, db.begin():
                reservation = await db.get(CodingCheckoutReservation, capture.checkout_id)
                if reservation is not None and reservation.token == token and reservation.service_epoch == self._capture_epoch:
                    row = await db.get(CodingReviewCapture, capture.id)
                    row.status, row.error_code, row.finished_at = status, error_code, now()
                    row.stored_bytes, row.manifest_hash = stored_bytes, manifest_hash
                    row.summary_json = json.dumps(summary)
                    if review_id is None:
                        await self._release_checkout(db, "capture", capture.id, token)

    def _capture_metadata(self, row):
        return {"id": row.id, "session_id": row.session_id, "checkout_id": row.checkout_id,
            "scope": row.scope, "status": row.status, "error_code": row.error_code,
            "created_at": row.created_at, "expires_at": row.expires_at, "finished_at": row.finished_at,
            "stored_bytes": row.stored_bytes, "ai_reviews": False, **json.loads(row.summary_json or "{}")}

    async def _authorized_capture(self, db, user_id, capture_id):
        await self._require_user(db, user_id)
        row = await db.get(CodingReviewCapture, capture_id)
        if row is None or row.user_id != user_id:
            raise failure("not_found", "Review capture not found")
        await self._session(db, user_id, row.session_id)
        await self._workspace(db, user_id, row.workspace_id)
        return row

    async def _load_capture(self, user_id, capture_id):
        self._captures_enabled()
        async with self._db_lock, self._sessions() as db:
            row = await self._authorized_capture(db, user_id, capture_id)
        if row.status != "ready":
            raise failure("capture_expired" if row.status == "expired" else "capture_unavailable", "Retained capture content is unavailable")
        if utc(row.expires_at) <= now() and not await self._capture_is_pinned(capture_id):
            raise failure("capture_expired", "Retained capture content has expired")
        try:
            manifest = await asyncio.to_thread(self._capture_storage.load, user_id, capture_id, row.manifest_hash)
        except (OSError, ValueError, RepositoryError):
            raise failure("capture_corrupt", "Retained content is missing or failed integrity validation") from None
        async with self._db_lock, self._sessions() as db:
            await self._authorized_capture(db, user_id, capture_id)
        return row, manifest

    async def get_review_capture(self, *, user_id, capture_id):
        self._captures_enabled()
        async with self._db_lock, self._sessions() as db:
            row = await self._authorized_capture(db, user_id, capture_id)
        metadata = self._capture_metadata(row)
        if row.status != "ready":
            return {**metadata, "content_status": "unavailable", "applicability": "unknown"}
        try:
            _, manifest = await self._load_capture(user_id, capture_id)
            def validate():
                for item in manifest["files"]:
                    for side in ("before", "after", "patch"):
                        if item[side] is not None:
                            self._capture_storage.blob(user_id, capture_id, item[side])
                for item in manifest["context"]:
                    self._capture_storage.blob(user_id, capture_id, item)
            await asyncio.to_thread(validate)
        except Exception as exc:
            code = getattr(exc, "code", "capture_corrupt")
            if code not in {"capture_expired", "capture_unavailable", "capture_corrupt"}:
                raise
            async with self._db_lock, self._sessions() as db:
                await self._authorized_capture(db, user_id, capture_id)
            return {**metadata, "content_status": "expired" if code == "capture_expired" else "unavailable",
                    "error_code": code, "applicability": "unknown"}
        applicability = "unknown"
        try:
            async with self._db_lock, self._sessions() as db:
                _, _, _, checkout = await self._capture_context(db, user_id, row.session_id)
                root, baseline = self._checkout_path(checkout), checkout.baseline_commit
            # This is a NEW live observation only for applicability, never evidence content.
            async with asyncio.timeout(self._capture_builder.limits.seconds):
                current, _ = await self._capture_builder.scan(root, baseline, row.scope, [item["path"] for item in manifest["files"]])
            applicability = "current" if current["fingerprint"] == manifest["fingerprint"] else "stale"
        except RepositoryError as exc:
            if exc.code == "capture_changed":
                applicability = "stale"
        except Exception:
            pass
        async with self._db_lock, self._sessions() as db:
            await self._authorized_capture(db, user_id, capture_id)
        return {**metadata, "content_status": "available", "applicability": applicability,
            "applicability_observed_at": now(), "files": manifest["files"], "context": manifest["context"],
            "exclusions": manifest["exclusions"]}

    async def review_capture_diff(self, *, user_id, capture_id, path):
        row, manifest = await self._load_capture(user_id, capture_id)
        item = next((item for item in manifest["files"] if item["path"] == path), None)
        if item is None:
            raise failure("not_found", "Path is not part of this captured diff")
        try:
            def read():
                for side in ("before", "after"):
                    if item[side] is not None:
                        self._capture_storage.blob(user_id, capture_id, item[side])
                return self._capture_storage.blob(user_id, capture_id, item["patch"])
            patch = await asyncio.to_thread(read)
        except (OSError, ValueError, RepositoryError):
            raise failure("capture_corrupt", "Retained diff failed integrity validation") from None
        async with self._db_lock, self._sessions() as db:
            await self._authorized_capture(db, user_id, capture_id)
        return {"basis": "review_capture", "capture_id": row.id, "scope": row.scope,
            "base": manifest["base"], "head": manifest["head"], "path": path,
            "captured_at": manifest["captured_at"], "patch": patch, "truncated": False}

    async def _interrupt_capture_jobs(self):
        if self._capture_storage is not None:
            async with self._db_lock, self._sessions() as db, db.begin():
                holders = select(CodingCheckoutReservation.holder_id).where(
                    CodingCheckoutReservation.holder_kind == "capture",
                    CodingCheckoutReservation.service_epoch == self._capture_epoch)
                await db.execute(update(CodingReviewCapture).where(CodingReviewCapture.id.in_(holders),
                    CodingReviewCapture.status == "capturing").values(
                        status="interrupted", error_code="backend_shutdown", finished_at=now()))
                await db.execute(delete(CodingCheckoutReservation).where(
                    CodingCheckoutReservation.service_epoch == self._capture_epoch,
                    CodingCheckoutReservation.holder_kind == "capture"))

    async def expire_captures(self):
        if not self.supports_review_captures:
            return
        async with self._db_lock, self._sessions() as db:
            rows = list((await db.execute(select(CodingReviewCapture).where(
                CodingReviewCapture.expires_at <= now(), CodingReviewCapture.status != "capturing",
                (CodingReviewCapture.status != "expired") | (CodingReviewCapture.stored_bytes > 0)).limit(50))).scalars())
        for row in rows:
            if await self._capture_is_pinned(row.id):
                continue
            try:
                await asyncio.to_thread(self._capture_storage.purge, row.user_id, row.id)
            except (OSError, RepositoryError):
                continue
            async with self._db_lock, self._sessions() as db, db.begin():
                current = await db.get(CodingReviewCapture, row.id)
                if current.status != "capturing":
                    current.status, current.stored_bytes = "expired", 0
