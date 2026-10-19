"""Owner-scoped Explorer, bound to the session's ready managed checkout only."""
from pathlib import Path

from app.coding_runtime.file_browser import FILE_BYTES, FILE_LINES, PAGE_ENTRIES
from app.coding_runtime.repository import RepositoryError


class CheckoutExplorerService:
    async def _explorer_identity(self, *, user_id, session_id):
        from app.coding_runtime.service import CodingServiceError
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            session = await self._session(db, user_id, session_id)
            workspace = await self._workspace(db, user_id, session.workspace_id)
            checkout = await self._checkout(db, session_id)
            if checkout is None:
                raise CodingServiceError("explorer_unavailable", "Create a ready managed checkout to browse files. The source repository is not a fallback.")
            if checkout.user_id != user_id or checkout.workspace_id not in (None, workspace.id):
                raise CodingServiceError("workspace_denied", "This checkout does not belong to the authorized workspace owner.")
            if checkout.status != "ready":
                raise CodingServiceError("checkout_unavailable", "The Explorer checkout is not ready.")
            root = self._checkout_path(checkout)
            reference = {"provider": "coding_checkout", "root_id": checkout.id, "session_id": session_id,
                         "label": Path(workspace.canonical_path).name, "read_only": True,
                         "byte_limit": FILE_BYTES, "line_limit": FILE_LINES, "entry_limit": PAGE_ENTRIES}
        return root, reference, workspace.id

    async def _explorer_root(self, *, user_id, session_id):
        from app.coding_runtime.service import CodingServiceError
        identity = await self._explorer_identity(user_id=user_id, session_id=session_id)
        # Establish ownership before touching a checkout, then release the DB while
        # validating its actual worktree. Root metadata also needs post-wait access checks.
        root = await self._execution_root(user_id=user_id, session_id=session_id)
        if root != identity[0] or await self._explorer_identity(user_id=user_id, session_id=session_id) != identity:
            raise CodingServiceError("workspace_denied", "Explorer access changed during root validation.")
        return identity

    async def explorer_root(self, *, user_id, session_id):
        _, reference, _ = await self._explorer_root(user_id=user_id, session_id=session_id)
        return reference

    async def inspect_source(self, *, user_id, session_id, root_id, path, file=False, offset=0, version=None):
        from app.coding_runtime.service import CodingServiceError
        root, reference, grant = await self._explorer_root(user_id=user_id, session_id=session_id)
        if reference["root_id"] != root_id:
            raise CodingServiceError("checkout_invalid", "This tab belongs to a different checkout. Open Explorer again.")
        try:
            result = await self._file_browser.inspect(root, path=path, file=file, offset=offset, version=version)
        except RepositoryError as exc:
            raise CodingServiceError(exc.code, str(exc)) from exc
        # No DB lock/transaction spans filesystem or Git waits. Revalidate ownership,
        # grant, ready checkout and attachment identity before returning any bytes.
        if await self._explorer_root(user_id=user_id, session_id=session_id) != (root, reference, grant):
            raise CodingServiceError("workspace_denied", "Explorer access changed during inspection.")
        return {"root": reference, **result}
