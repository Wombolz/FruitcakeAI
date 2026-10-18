"""Conversation controls and owner-scoped reusable checkout coordination."""
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select

from app.db.models import CodingCheckout, CodingCheckoutAttachment, CodingSession, CodingWorkspace
from app.coding_runtime import git_delivery
from app.coding_runtime.repository import RepositoryError
import asyncio


def error(code, message):
    from app.coding_runtime.service import CodingServiceError
    return CodingServiceError(code, message)


class WorkspaceLifecycleService:
    async def _owned_checkout(self, db, user_id, checkout_id):
        await self._require_user(db, user_id)
        checkout = await db.get(CodingCheckout, checkout_id)
        if checkout is None or checkout.user_id != user_id:
            raise error("not_found", "Checkout not found")
        if checkout.workspace_id is None:
            creator = await db.get(CodingSession, checkout.session_id)
            checkout.workspace_id = creator.workspace_id
        await self._workspace(db, user_id, checkout.workspace_id)
        return checkout

    async def _attached_sessions(self, db, checkout):
        linked = select(CodingCheckoutAttachment.session_id).where(CodingCheckoutAttachment.checkout_id == checkout.id)
        return list((await db.execute(select(CodingSession).where(or_(
            CodingSession.id.in_(linked), CodingSession.id == checkout.session_id,
        )))).scalars())

    async def _assert_checkout_idle(self, db, checkout, *, allow_failed=False):
        if checkout.status not in ({"ready", "failed"} if allow_failed else {"ready"}):
            raise error("checkout_unavailable", "Checkout is not ready; the source repository will not be used as a fallback")
        for session in await self._attached_sessions(db, checkout):
            slot = self._slots.get(session.id)
            if session.status in {"starting", "running", "waiting_approval", "stopping"} or (slot and slot.task is not None):
                raise error("checkout_conflict", "Another run is using this checkout. Finish or Stop it before continuing.")

    async def list_checkouts(self, *, user_id):
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            return list((await db.execute(select(CodingCheckout).where(
                CodingCheckout.user_id == user_id, CodingCheckout.status != "removed",
            ).order_by(CodingCheckout.created_at.desc()))).scalars())

    async def change_model(self, *, user_id, session_id, model_profile_id):
        async with self._lock:
            async with self._db_lock, self._sessions() as db:
                session = await self._session(db, user_id, session_id)
                await self._require_user(db, user_id)
                await self._workspace(db, user_id, session.workspace_id)
                await self._model(db, user_id, model_profile_id)
                if session.status not in {"idle", "interrupted"} or self._closing:
                    raise error("session_busy", "Change models only while the conversation is idle")
                checkout = await self._checkout(db, session_id)
                if checkout is not None and checkout.status in {"creating", "removing", "committing", "applying"}:
                    raise error("checkout_conflict", "Wait for the checkout operation before changing models")
            if slot := self._slots.get(session_id):
                await self._evict(session_id, slot)
            async with self._db_lock, self._sessions() as db, db.begin():
                session = await self._session(db, user_id, session_id)
                await self._model(db, user_id, model_profile_id)
                session.model_profile_id = model_profile_id
                # Different model policies may not allow the previous tool opt-ins.
                session.edit_mode = session.command_mode = False
                session.updated_at = datetime.now(timezone.utc)
                return session

    async def archive_session(self, *, user_id, session_id, archived=True):
        if archived:
            await self.close_session(user_id=user_id, session_id=session_id)
        async with self._lock, self._db_lock, self._sessions() as db, db.begin():
            session = await self._session(db, user_id, session_id)
            if not archived and session.archived_at is None:
                return session
            session.archived_at = datetime.now(timezone.utc) if archived else None
            session.status = "closed" if archived else "idle"
            session.edit_mode = session.command_mode = False
            session.updated_at = datetime.now(timezone.utc)
            return session

    async def delete_session(self, *, user_id, session_id):
        # Tombstone only: keep command approvals/change provenance and files.
        await self.close_session(user_id=user_id, session_id=session_id)
        async with self._lock, self._db_lock, self._sessions() as db, db.begin():
            session = await self._session(db, user_id, session_id)
            session.deleted_at = datetime.now(timezone.utc)
            session.edit_mode = session.command_mode = False
            session.updated_at = datetime.now(timezone.utc)

    async def delivery(self, *, user_id, session_id, action, files=(), message="", review_hash=None):
        if self._checkouts is None:
            raise error("unsupported", "Managed checkouts are unavailable")
        async with self._lock:
            if self._closing:
                raise error("shutting_down", "Coding service is shutting down")
            async with self._db_lock, self._sessions() as db:
                user = await self._require_user(db, user_id)
                if action == "apply" and user.role not in self._admin_roles:
                    raise error("workspace_denied", "Only an operator may apply commits to the original source folder")
                session = await self._session(db, user_id, session_id)
                checkout = await self._checkout(db, session_id)
                if checkout is None:
                    raise error("checkout_conflict", "Attach a managed checkout first")
                checkout = await self._owned_checkout(db, user_id, checkout.id)
                await self._assert_checkout_idle(db, checkout)
                workspace = await self._workspace(db, user_id, session.workspace_id)
                root, source = self._checkout_path(checkout), Path(workspace.canonical_path)
                repository_grants = select(CodingWorkspace.id).where(CodingWorkspace.canonical_path == str(source))
                busy = (await db.execute(select(CodingCheckout.id).where(
                    CodingCheckout.workspace_id.in_(repository_grants),
                    CodingCheckout.status.in_(("committing", "applying")),
                ))).first()
                active = (await db.execute(select(CodingSession.id).where(
                    CodingSession.workspace_id.in_(repository_grants),
                    CodingSession.status.in_(("starting", "running", "waiting_approval", "stopping")),
                ))).first()
                if busy or active:
                    raise error("checkout_conflict", "Finish active work in this repository before reviewing or delivering commits")
                attached = await self._attached_sessions(db, checkout)
            for attached_session in attached:
                if slot := self._slots.get(attached_session.id):
                    await self._evict(attached_session.id, slot)
            async with self._db_lock, self._sessions() as db, db.begin():
                await self._require_user(db, user_id)
                await self._workspace(db, user_id, session.workspace_id)
                row = await db.get(CodingCheckout, checkout.id)
                row.status = "committing" if action == "commit" else "applying"
                row.error_code = None
        mutated = False
        try:
            async with asyncio.timeout(60):
                await self._checkouts.validate_ready(source, root, checkout.baseline_commit,
                    head_commit=checkout.head_commit, branch_name=checkout.branch_name)
                plan = (await git_delivery.commit_plan(self._checkouts, root, checkout, files, message)
                        if action == "commit" else await git_delivery.source_plan(self._checkouts, source, checkout))
                if review_hash is None:
                    return plan
                if review_hash != plan["review_hash"]:
                    raise error("repository_changed", "Files, commits, or source state changed. Review a fresh preview before confirming.")
                async with self._db_lock, self._sessions() as db:
                    user = await self._require_user(db, user_id)
                    if action == "apply" and user.role not in self._admin_roles:
                        raise error("workspace_denied", "Source delivery is no longer authorized")
                    await self._workspace(db, user_id, session.workspace_id)
                if self._closing:
                    raise error("shutting_down", "Coding service is shutting down; nothing was applied")
                mutated = True
                if action == "commit":
                    new_head = await git_delivery.create_commit(self._checkouts, root, plan)
                    async with self._db_lock, self._sessions() as db, db.begin():
                        row = await db.get(CodingCheckout, checkout.id)
                        row.head_commit, row.branch_name = new_head, plan["branch"]
                else:
                    await git_delivery.apply_source(self._checkouts, source, plan)
                    new_head = plan["head"]
                return {"action": action, "head": new_head, "checkout_id": checkout.id}
        except RepositoryError as exc:
            raise error(exc.code, str(exc)) from exc
        except (OSError, UnicodeError, TimeoutError) as exc:
            raise error("repository_unavailable", "Git delivery could not complete safely. Inspect the checkout before retrying.") from exc
        finally:
            # A crash/cancellation after a Git mutation is uncertain, never replayed.
            import sys
            failed = sys.exc_info()[0] is not None
            async with self._db_lock, self._sessions() as db, db.begin():
                row = await db.get(CodingCheckout, checkout.id)
                row.status = "failed" if failed and mutated else "ready"
                row.error_code = "delivery_uncertain" if failed and mutated else None
