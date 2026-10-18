from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.coding_runtime.contracts import (
    CodingModel, CodingPrompt, CodingRuntime, CodingRuntimeError, CodingRuntimeEvent, CodingRuntimeStart,
)
from app.db.models import (
    CodingApproval, CodingCheckout, CodingCheckoutAttachment, CodingRun, CodingRunEvent, CodingSession, CodingWorkspace,
    ModelProfile, User, UserModelAccess, UserProject,
)
from app.host_root_access import validate_host_root_candidate
from app.coding_runtime.output import operation_preview, payload_size, text_prefix
from app.coding_runtime.repository import RepositoryError, RepositoryInspector
from app.coding_runtime.checkouts import CheckoutManager
from app.coding_runtime import changes
from app.coding_runtime.command_service import ContainerCommandService
from app.coding_runtime.workspace_lifecycle import WorkspaceLifecycleService
from app.coding_runtime.container_commands import DockerCommandRunner
from app.coding_runtime.run_deadline import RunDeadline
from app.db.models import CodingChange


ACTIVE_STATUSES = frozenset({"starting", "running", "waiting_approval", "stopping"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
READ_TOOLS = ("read", "grep", "find", "ls")


class CodingServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CodingServiceLimits:
    max_active_sessions: int = 2
    idle_seconds: float = 300
    run_seconds: float = 1800
    startup_seconds: float = 30
    stop_seconds: float = 10
    max_run_events: int = 2000
    max_payload_bytes: int = 32768
    max_response_chars: int = 65536
    max_prompt_chars: int = 32000

    def __post_init__(self):
        if min(vars(self).values()) <= 0:
            raise ValueError("Coding service limits must be positive")


@dataclass(frozen=True)
class StoredCodingEvent:
    sequence: int
    kind: str
    payload: dict
    schema_version: int = 1


@dataclass
class _RuntimeSlot:
    runtime: CodingRuntime
    request: CodingRuntimeStart
    workspace_id: str
    owner_id: int
    task: asyncio.Task | None = None
    run_id: str | None = None
    ready: bool = False
    stop_requested: bool = False
    last_used: float = field(default_factory=time.monotonic)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    entered: bool = False
    quarantined: bool = False
    finalizing: bool = False
    deadline: RunDeadline | None = None


class CodingSessionService(WorkspaceLifecycleService, ContainerCommandService):
    """Single-backend-process supervisor with durable, user-scoped run state."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime_factory: Callable[[], CodingRuntime],
        *,
        runtime_kind: str = "pi",
        limits: CodingServiceLimits | None = None,
        admin_roles: tuple[str, ...] = ("admin", "super_admin"),
        checkout_root: Path | None = None,
        command_runner: DockerCommandRunner | None = None,
    ):
        self._sessions = sessions
        self._factory = runtime_factory
        self.runtime_kind = runtime_kind
        self.limits = limits or CodingServiceLimits()
        self._admin_roles = admin_roles
        self._slots: dict[str, _RuntimeSlot] = {}
        self._lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self._closing = False
        self._maintenance: asyncio.Task | None = None
        self._repository = RepositoryInspector()
        self._checkouts = CheckoutManager(checkout_root.resolve()) if checkout_root is not None else None
        self._checkout_jobs: dict[str, asyncio.Task] = {}
        self._commands = command_runner
        self._command_jobs: dict[str, tuple[str, asyncio.Task]] = {}
        self._command_cleanup_failed = False

    @property
    def supports_managed_checkouts(self) -> bool:
        return self._checkouts is not None

    async def startup(self) -> int:
        """Recover durable state after the sole backend process restarts."""
        async with self._lock:
            if self._slots or self._maintenance is not None:
                raise CodingServiceError("already_started", "Coding service has already started")
            if self._commands is not None:
                try:
                    await self._commands.recover()
                    self._command_cleanup_failed = False
                except Exception:
                    self._command_cleanup_failed = True
            async with self._db_lock, self._sessions() as db, db.begin():
                runs = (await db.execute(select(CodingRun).where(CodingRun.status.in_(ACTIVE_STATUSES)))).scalars().all()
                for run in runs:
                    run.status = "interrupted"
                    run.error_code = "backend_restart"
                    run.finished_at = _now()
                    session = await db.get(CodingSession, run.session_id)
                    if session is not None:
                        session.status = "interrupted"
                    await self._resolve_pending(db, run.id)
                    await self._recover_command_records(db, run.id)
                    self._add_event(db, run, "run.interrupted", {"reason": "backend_restart"})
                checkouts = (await db.execute(select(CodingCheckout).where(
                    CodingCheckout.status.in_(("creating", "removing", "committing", "applying")),
                ))).scalars()
                for checkout in checkouts:
                    checkout.status = "failed"
                    checkout.error_code = "backend_restart"
                await db.execute(update(CodingChange).where(CodingChange.status == "applying").values(
                    status="uncertain", error_code="backend_restart",
                ))
            self._closing = False
            self._maintenance = asyncio.create_task(self._maintain(), name="coding-runtime-idle")
            return len(runs)

    async def _require_workspace_operator(self, operator_id: int, user_id: int) -> None:
        async with self._db_lock, self._sessions() as db:
            operator = await db.get(User, operator_id)
            if operator is None or not operator.is_active or operator.role not in self._admin_roles:
                raise CodingServiceError("workspace_denied", "Only an operator may authorize coding workspaces")
            await self._require_user(db, user_id)

    async def create_repository(self, *, operator_id: int, user_id: int, path: str) -> CodingWorkspace:
        await self._require_workspace_operator(operator_id, user_id)
        if not self._repository.git:
            raise CodingServiceError("repository_unavailable", "Git is not installed on the backend")
        supplied = Path(path)
        if not supplied.is_absolute() or supplied.name in {"", ".", "..", ".git"}:
            raise CodingServiceError("invalid_request", "Choose a name and location for a new repository folder")
        parent = supplied.parent.resolve(strict=True)
        candidate = parent / supplied.name
        validate_host_root_candidate(str(candidate))
        try:
            candidate.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError as exc:
            raise CodingServiceError("request_conflict", "That folder already exists. Choose a new folder name, or authorize an existing repository.") from exc
        try:
            async with asyncio.timeout(30):
                # No inherited templates, signing, hooks, identity, or shell commands.
                await self._repository._git(candidate, "init", "--initial-branch=main", "--template=")
                await self._repository._git(candidate, "-c", "user.name=Fruitcake", "-c",
                                            "user.email=fruitcake@localhost", "-c", "commit.gpgSign=false",
                                            "commit", "--allow-empty", "--no-gpg-sign", "-m", "Initial commit")
                return await self.approve_workspace(operator_id=operator_id, user_id=user_id,
                                                    path=str(candidate), require_repository=True)
        except (RepositoryError, OSError, TimeoutError) as exc:
            raise CodingServiceError("repository_creation_failed", "Repository setup did not finish. The new folder was kept; inspect it before authorizing it or choosing another name.") from exc

    async def approve_workspace(self, *, operator_id: int, user_id: int, path: str,
                                require_repository: bool = False) -> CodingWorkspace:
        await self._require_workspace_operator(operator_id, user_id)
        candidate = validate_host_root_candidate(path).resolve(strict=True)
        if not candidate.is_dir():
            raise CodingServiceError("workspace_denied", "A coding workspace must be an existing directory")
        if require_repository:
            try:
                async with asyncio.timeout(10):
                    await self._repository._validate_root(candidate)
                    head, _ = await self._repository._git(candidate, "rev-parse", "--verify", "HEAD^{commit}", allowed=(0, 128))
                    if not head:
                        raise CodingServiceError("not_repository", "This repository has no initial commit. Create a commit before authorizing it for isolated checkouts.")
            except RepositoryError as exc:
                raise CodingServiceError(exc.code, "Select the top-level Git repository folder, not an empty folder or a subfolder.") from exc
            except TimeoutError as exc:
                raise CodingServiceError("repository_timeout", "Repository validation timed out") from exc
        async with self._db_lock, self._sessions() as db, db.begin():
            operator = await db.get(User, operator_id)
            if operator is None or not operator.is_active or operator.role not in self._admin_roles:
                raise CodingServiceError("workspace_denied", "Only an operator may authorize coding workspaces")
            await self._require_user(db, user_id)
            workspace = (await db.execute(select(CodingWorkspace).where(
                CodingWorkspace.user_id == user_id, CodingWorkspace.canonical_path == str(candidate),
            ))).scalar_one_or_none()
            if workspace is None:
                workspace = CodingWorkspace(user_id=user_id, canonical_path=str(candidate), approved_by_user_id=operator_id)
                db.add(workspace)
            else:
                workspace.is_active = True
                workspace.approved_by_user_id = operator_id
            await db.flush()
            return workspace

    async def create_session(
        self, *, user_id: int, workspace_id: str, model_profile_id: int,
        title: str = "Coding session", project_id: int | None = None, checkout_id: str | None = None,
    ) -> CodingSession:
        if not title.strip() or len(title) > 255:
            raise CodingServiceError("invalid_request", "Coding session title must contain 1-255 characters")
        async with self._db_lock, self._sessions() as db, db.begin():
            user = await self._require_user(db, user_id)
            await self._workspace(db, user_id, workspace_id)
            await self._model(db, user_id, model_profile_id)
            if project_id is not None:
                project = await db.get(UserProject, project_id)
                if project is None or project.user_id != user_id or project.archived_at is not None:
                    raise CodingServiceError("not_found", "Active project not found")
            session = CodingSession(
                user_id=user_id, workspace_id=workspace_id, model_profile_id=model_profile_id,
                project_id=project_id, title=title.strip(), persona=user.persona or "family_assistant",
                runtime_kind=self.runtime_kind,
            )
            db.add(session)
            await db.flush()
            if checkout_id is not None:
                checkout = await self._owned_checkout(db, user_id, checkout_id)
                if checkout.workspace_id != workspace_id or checkout.status != "ready":
                    raise CodingServiceError("checkout_conflict", "Choose a ready checkout from this authorized repository")
                db.add(CodingCheckoutAttachment(session_id=session.id, checkout_id=checkout.id))
            return session

    async def revoke_workspace(self, *, operator_id: int, workspace_id: str) -> None:
        async with self._db_lock, self._sessions() as db, db.begin():
            operator = await db.get(User, operator_id)
            if operator is None or not operator.is_active or operator.role not in self._admin_roles:
                raise CodingServiceError("workspace_denied", "Only an operator may revoke coding workspaces")
            workspace = await db.get(CodingWorkspace, workspace_id)
            if workspace is None:
                raise CodingServiceError("not_found", "Coding workspace not found")
            workspace.is_active = False
            checkout_ids = list((await db.execute(select(CodingCheckout.id).join(
                CodingSession, CodingSession.id == CodingCheckout.session_id,
            ).where(CodingSession.workspace_id == workspace_id))).scalars())
        checkout_tasks = [self._checkout_jobs[key] for key in checkout_ids if key in self._checkout_jobs]
        for task in checkout_tasks:
            task.cancel()
        await asyncio.gather(*checkout_tasks, return_exceptions=True)
        await self._interrupt_checkouts(checkout_ids, "checkout_interrupted")
        async with self._lock:
            active = [(key, slot, slot.run_id, slot.task) for key, slot in self._slots.items()
                      if slot.workspace_id == workspace_id]
        for key, slot, run_id, task in active:
            if task is not None and run_id is not None:
                await self.stop(user_id=slot.owner_id, run_id=run_id)
                await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            for key, slot, _, _ in active:
                if self._slots.get(key) is slot and slot.task is None:
                    await self._evict(key, slot)

    async def get_session(self, *, user_id: int, session_id: str) -> CodingSession:
        async with self._db_lock, self._sessions() as db:
            return await self._session(db, user_id, session_id)

    async def get_checkout(self, *, user_id: int, session_id: str) -> CodingCheckout | None:
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            await self._session(db, user_id, session_id)
            return await self._checkout(db, session_id)

    async def set_edit_mode(self, *, user_id: int, session_id: str, enabled: bool) -> CodingSession:
        if enabled:
            await self._execution_root(user_id=user_id, session_id=session_id)
        async with self._lock:
            async with self._db_lock, self._sessions() as db, db.begin():
                await self._require_user(db, user_id)
                session = await self._session(db, user_id, session_id)
                await self._workspace(db, user_id, session.workspace_id)
                if session.status not in {"idle", "interrupted"} or self._closing:
                    raise CodingServiceError("session_busy", "Change edit mode only while the session is idle")
                checkout = await self._checkout(db, session_id)
                if enabled and (checkout is None or checkout.status != "ready"):
                    raise CodingServiceError("checkout_conflict", "Approved edits require a ready isolated checkout")
                if enabled and not await self._write_tools(db, user_id, session.model_profile_id):
                    raise CodingServiceError("model_denied", "Edit tools are disabled for this model")
                session.edit_mode = enabled
                session.updated_at = _now()
            if slot := self._slots.get(session_id):
                await self._evict(session_id, slot)
            return session

    async def list_changes(self, *, user_id: int, run_id: str) -> list[CodingChange]:
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            await self._run(db, user_id, run_id)
            return list((await db.execute(select(CodingChange).where(CodingChange.run_id == run_id)
                                         .order_by(CodingChange.created_at, CodingChange.id))).scalars())

    async def create_checkout(self, *, user_id: int, session_id: str) -> CodingCheckout:
        if self._checkouts is None:
            raise CodingServiceError("unsupported", "Managed checkouts are unavailable on this backend")
        async with self._lock:
            if self._closing:
                raise CodingServiceError("shutting_down", "Coding service is shutting down")
            async with self._db_lock, self._sessions() as db, db.begin():
                await self._require_user(db, user_id)
                session = await self._session(db, user_id, session_id)
                await self._workspace(db, user_id, session.workspace_id)
                existing = await self._checkout(db, session_id)
                if existing is not None:
                    return existing  # Session identity is the idempotency boundary.
                if session.status not in {"idle", "interrupted"}:
                    raise CodingServiceError("checkout_conflict", "Stop the active run before creating an isolated checkout; closed sessions cannot transition")
                slot = self._slots.get(session_id)
                if slot is not None and slot.task is not None:
                    raise CodingServiceError("checkout_conflict", "Wait for the current run to finish before changing execution roots")
                retained = list((await db.execute(select(CodingCheckout.id).where(
                    CodingCheckout.user_id == user_id, CodingCheckout.status != "removed",
                ).limit(8))).scalars())
                if len(retained) >= 8 or len(self._checkout_jobs) >= 2:
                    raise CodingServiceError("capacity_reached", "Managed checkout capacity reached; remove an unused clean checkout first")
            # Retire source-root process memory before attaching the checkout.
            # History stays durable; no DB lock is held over process teardown.
            if slot is not None:
                try:
                    await self._evict(session_id, slot)
                except Exception as exc:
                    raise CodingServiceError("cleanup_failed", "The source runtime could not be retired; no checkout was attached. Reconcile runtime state before retrying.") from exc
            async with self._db_lock, self._sessions() as db, db.begin():
                await self._require_user(db, user_id)
                session = await self._session(db, user_id, session_id)
                await self._workspace(db, user_id, session.workspace_id)
                checkout = CodingCheckout(id=str(uuid4()), session_id=session_id, user_id=user_id,
                                          workspace_id=session.workspace_id, status="creating")
                checkout.canonical_path = str(self._checkouts.path(user_id, checkout.id))
                db.add(checkout)
                await db.flush()
                db.add(CodingCheckoutAttachment(session_id=session_id, checkout_id=checkout.id))
            self._start_checkout_job(checkout.id)
            return checkout

    async def remove_checkout(self, *, user_id: int, session_id: str) -> CodingCheckout:
        if self._checkouts is None:
            raise CodingServiceError("unsupported", "Managed checkouts are unavailable on this backend")
        async with self._lock:
            if self._closing:
                raise CodingServiceError("shutting_down", "Coding service is shutting down")
            async with self._db_lock, self._sessions() as db, db.begin():
                await self._require_user(db, user_id)
                session = await self._session(db, user_id, session_id)
                await self._workspace(db, user_id, session.workspace_id)
                checkout = await self._checkout(db, session_id)
                if checkout is None:
                    raise CodingServiceError("not_found", "Managed checkout not found")
                if checkout.status in {"removed", "removing"}:
                    return checkout
                if session.status != "closed" or checkout.status == "creating" or session_id in self._slots:
                    raise CodingServiceError("checkout_conflict", "Close the coding session before removing its checkout")
                await self._assert_checkout_idle(db, checkout, allow_failed=True)
                attached = await self._attached_sessions(db, checkout)
                if any(item.status != "closed" for item in attached):
                    raise CodingServiceError("checkout_conflict", "Close all conversations attached to this checkout before removing it")
                if len(self._checkout_jobs) >= 2:
                    raise CodingServiceError("capacity_reached", "Managed checkout operations are busy")
                checkout.status = "removing"
                checkout.error_code = None
            self._start_checkout_job(checkout.id, removing=True)
            return checkout

    def _start_checkout_job(self, checkout_id: str, *, removing: bool = False) -> None:
        task = asyncio.create_task(self._manage_checkout(checkout_id, removing=removing), name=f"coding-checkout-{checkout_id}")
        self._checkout_jobs[checkout_id] = task
        def completed(_task):
            if self._checkout_jobs.get(checkout_id) is task:
                self._checkout_jobs.pop(checkout_id, None)
        task.add_done_callback(completed)

    async def _interrupt_checkouts(self, checkout_ids: list[str], reason: str) -> None:
        if not checkout_ids:
            return
        # A task cancelled before its first instruction never enters its finally
        # block. Finalize those durable reservations explicitly as well.
        async with self._db_lock, self._sessions() as db, db.begin():
            await db.execute(update(CodingCheckout).where(
                CodingCheckout.id.in_(checkout_ids), CodingCheckout.status.in_(("creating", "removing")),
            ).values(status="failed", error_code=reason))

    async def _manage_checkout(self, checkout_id: str, *, removing: bool) -> None:
        status, error_code = "failed", "checkout_failed"
        try:
            async with self._db_lock, self._sessions() as db:
                checkout = await db.get(CodingCheckout, checkout_id)
                await self._require_user(db, checkout.user_id)
                creator = await db.get(CodingSession, checkout.session_id)
                workspace_id = checkout.workspace_id or creator.workspace_id
                workspace = await self._workspace(db, checkout.user_id, workspace_id)
                source = Path(workspace.canonical_path)
                target = self._checkout_path(checkout)
                baseline = checkout.baseline_commit
                expected_head, branch = checkout.head_commit or baseline, checkout.branch_name
            reason = f"Fruitcake coding checkout {checkout_id}"
            if removing:
                await self._checkouts.remove(source, target, baseline, reason, head_commit=expected_head, branch_name=branch)
                status, error_code = "removed", None
            else:
                baseline = await asyncio.wait_for(self._checkouts.baseline(source), 15)
                async with self._db_lock, self._sessions() as db, db.begin():
                    await self._require_user(db, checkout.user_id)
                    await self._workspace(db, checkout.user_id, workspace_id)
                    row = await db.get(CodingCheckout, checkout_id)
                    row.baseline_commit = row.head_commit = baseline
                await self._checkouts.create(source, target, reason, baseline=baseline)
                async with self._db_lock, self._sessions() as db:
                    await self._require_user(db, checkout.user_id)
                    await self._workspace(db, checkout.user_id, workspace_id)
                status, error_code = "ready", None
        except asyncio.CancelledError:
            error_code = "backend_shutdown" if self._closing else "checkout_interrupted"
        except (CodingServiceError, RepositoryError) as exc:
            error_code = exc.code
            if removing and exc.code == "checkout_dirty":
                status = "ready"  # Preserve reviewability of the retained checkout.
        except Exception:
            pass  # Never expose Git diagnostics/provider paths through error text.
        finally:
            async with self._db_lock, self._sessions() as db, db.begin():
                checkout = await db.get(CodingCheckout, checkout_id)
                checkout.status, checkout.error_code = status, error_code

    async def _checkout(self, db: AsyncSession, session_id: str) -> CodingCheckout | None:
        attachment = await db.get(CodingCheckoutAttachment, session_id)
        if attachment is not None:
            return await db.get(CodingCheckout, attachment.checkout_id)
        return (await db.execute(select(CodingCheckout).where(CodingCheckout.session_id == session_id))).scalar_one_or_none()

    def _checkout_path(self, checkout: CodingCheckout) -> Path:
        if self._checkouts is None or str(self._checkouts.path(checkout.user_id, checkout.id)) != checkout.canonical_path:
            raise CodingServiceError("checkout_invalid", "Managed checkout identity or storage location changed")
        return Path(checkout.canonical_path)

    async def _execution_path(self, db: AsyncSession, session: CodingSession, workspace: CodingWorkspace) -> Path:
        checkout = await self._checkout(db, session.id)
        if checkout is None:
            return Path(workspace.canonical_path)
        if checkout.status != "ready":
            raise CodingServiceError("checkout_unavailable", "Managed checkout is not ready. The source repository will not be used as a fallback.")
        return self._checkout_path(checkout)

    async def _execution_root(self, *, user_id: int, session_id: str) -> Path:
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            session = await self._session(db, user_id, session_id)
            workspace = await self._workspace(db, user_id, session.workspace_id)
            root = await self._execution_path(db, session, workspace)
            checkout = await self._checkout(db, session_id)
        if checkout is not None:
            try:
                await self._checkouts.validate_ready(Path(workspace.canonical_path), root, checkout.baseline_commit,
                                                    head_commit=checkout.head_commit, branch_name=checkout.branch_name)
            except (RepositoryError, OSError, TimeoutError) as exc:
                raise CodingServiceError("checkout_invalid", "Managed checkout is missing, changed, or no longer matches its baseline") from exc
        return root

    async def inspect_repository(self, *, user_id: int, session_id: str, path: str | None = None) -> dict:
        root = await self._execution_root(user_id=user_id, session_id=session_id)
        try:
            result = await self._repository.inspect(root, path=path)
        except RepositoryError as exc:
            raise CodingServiceError(exc.code, str(exc)) from exc
        # A grant/user may be revoked while Git is running. Do not deliver data
        # after that revocation; no DB lock is held over the subprocess wait.
        if await self._execution_root(user_id=user_id, session_id=session_id) != root:
            raise CodingServiceError("workspace_denied", "Coding workspace changed during inspection")
        return result

    async def list_workspaces(self, *, user_id: int) -> list[CodingWorkspace]:
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            return list((await db.execute(select(CodingWorkspace).where(
                CodingWorkspace.user_id == user_id,
            ).order_by(CodingWorkspace.created_at, CodingWorkspace.id))).scalars())

    async def list_models(self, *, user_id: int) -> list[dict]:
        """Return selectable local profiles with coding's numeric profile identity."""
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            profiles = (await db.execute(select(ModelProfile).where(
                ModelProfile.enabled.is_(True), ModelProfile.is_local.is_(True),
                ModelProfile.provider_family.in_(("ollama", "local", "openai_compat")),
            ).order_by(ModelProfile.display_name, ModelProfile.id))).scalars()
            result = []
            for profile in profiles:
                try:
                    await self._model(db, user_id, profile.id)
                except CodingServiceError as exc:
                    if exc.code == "model_denied":
                        continue
                    raise
                result.append({"id": profile.id, "label": profile.display_name, "model_id": profile.model_id})
            return result

    async def list_sessions(self, *, user_id: int, limit: int = 50, offset: int = 0, archived: bool = False) -> list[CodingSession]:
        self._validate_page(limit, offset)
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            return list((await db.execute(select(CodingSession).where(
                CodingSession.user_id == user_id,
                CodingSession.deleted_at.is_(None),
                CodingSession.archived_at.is_not(None) if archived else CodingSession.archived_at.is_(None),
            ).order_by(CodingSession.updated_at.desc(), CodingSession.id).limit(limit).offset(offset))).scalars())

    async def list_runs(self, *, user_id: int, session_id: str, limit: int = 50, offset: int = 0) -> list[CodingRun]:
        self._validate_page(limit, offset)
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            await self._session(db, user_id, session_id)
            return list((await db.execute(select(CodingRun).where(
                CodingRun.session_id == session_id, CodingRun.user_id == user_id,
            ).order_by(CodingRun.created_at.desc(), CodingRun.id).limit(limit).offset(offset))).scalars())

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if not 1 <= limit <= 100 or offset < 0:
            raise CodingServiceError("invalid_request", "Invalid coding history page")

    async def get_run(self, *, user_id: int, run_id: str) -> CodingRun:
        async with self._db_lock, self._sessions() as db:
            return await self._run(db, user_id, run_id)

    async def submit(
        self, *, user_id: int, session_id: str, message: str, client_request_id: str | None = None,
    ) -> CodingRun:
        self._validate_message(message)
        if client_request_id is not None and (not client_request_id.strip() or len(client_request_id) > 100):
            raise CodingServiceError("invalid_request", "Invalid client request identifier")
        async with self._lock:
            if self._closing:
                raise CodingServiceError("shutting_down", "Coding service is shutting down")
            async with self._db_lock, self._sessions() as db:
                session = await self._session(db, user_id, session_id)
                if client_request_id:
                    previous = (await db.execute(select(CodingRun).where(
                        CodingRun.session_id == session_id, CodingRun.client_request_id == client_request_id,
                    ))).scalar_one_or_none()
                    if previous is not None:
                        if previous.prompt != message:
                            raise CodingServiceError("request_conflict", "Client request identifier was used for a different prompt")
                        return previous
                if session.status == "closed":
                    raise CodingServiceError("session_closed", "Coding session is closed")
                if session.status in ACTIVE_STATUSES:
                    raise CodingServiceError("session_busy", "This coding session already has an active run")
                if session.runtime_kind != self.runtime_kind:
                    raise CodingServiceError("runtime_unavailable", "The coding session's runtime is unavailable")
                await self._require_user(db, user_id)
                workspace = await self._workspace(db, user_id, session.workspace_id)
                repository_grants = select(CodingWorkspace.id).where(CodingWorkspace.canonical_path == workspace.canonical_path)
                if (await db.execute(select(CodingCheckout.id).where(
                    CodingCheckout.workspace_id.in_(repository_grants), CodingCheckout.status.in_(("committing", "applying")),
                ))).first():
                    raise CodingServiceError("checkout_conflict", "Repository delivery is in progress. Wait before starting another run.")
                model, tools = await self._model(db, user_id, session.model_profile_id)
                write_tools = await self._write_tools(db, user_id, session.model_profile_id) if session.edit_mode else ()
                command_tools = await self._command_tools(db, user_id, session.model_profile_id) if session.command_mode else ()
                checkout = await self._checkout(db, session_id)
                if checkout is not None:
                    await self._assert_checkout_idle(db, checkout)
                if session.edit_mode and (not write_tools or checkout is None or checkout.status != "ready"):
                    raise CodingServiceError("runtime_denied", "Approved edits require authorized tools and a ready isolated checkout")
                if session.command_mode and (not command_tools or self._commands is None or self._command_cleanup_failed or checkout is None or checkout.status != "ready"):
                    raise CodingServiceError("runtime_denied", "Container commands require an available runner and a ready isolated checkout")
                request = CodingRuntimeStart(await self._execution_path(db, session, workspace), model, tools, write_tools, command_tools)
                peers = [item.id for item in await self._attached_sessions(db, checkout) if item.id != session_id] if checkout else []
            # Handoffs retire peer process memory. When that conversation returns
            # it reconstructs history and rereads the now-shared working files.
            for peer in peers:
                if retained := self._slots.get(peer):
                    await self._evict(peer, retained)
            slot = self._slots.get(session_id)
            if slot is not None and (slot.request != request or slot.quarantined):
                await self._evict(session_id, slot)
                slot = None
            new_process = slot is None
            if slot is None:
                if len(self._slots) >= self.limits.max_active_sessions:
                    idle = [(key, item) for key, item in self._slots.items() if item.task is None]
                    if idle:
                        key, item = min(idle, key=lambda pair: pair[1].last_used)
                        await self._evict(key, item)
                    else:
                        raise CodingServiceError("capacity_reached", "All coding runtimes are busy")
                slot = _RuntimeSlot(self._factory(), request, workspace.id, user_id)
                if not slot.runtime.capabilities().read_only:
                    raise CodingServiceError("runtime_denied", "The initial coding service requires a read-only runtime")
                if request.write_tools and not slot.runtime.capabilities().approved_edits:
                    raise CodingServiceError("runtime_denied", "This runtime does not support host-approved edits")
                if request.command_tools and not slot.runtime.capabilities().container_commands:
                    raise CodingServiceError("runtime_denied", "This runtime does not support container commands")
                self._slots[session_id] = slot
            try:
                async with self._db_lock, self._sessions() as db, db.begin():
                    session = await self._session(db, user_id, session_id)
                    claimed = await db.execute(update(CodingSession).where(
                        CodingSession.id == session_id,
                        CodingSession.status.in_(("idle", "interrupted")),
                    ).values(status="starting"))
                    if claimed.rowcount != 1:
                        raise CodingServiceError("session_busy", "This coding session already has an active run")
                    if new_process:
                        session.context_generation += 1
                    run = CodingRun(
                        id=str(uuid4()), session_id=session_id, user_id=user_id, prompt=message,
                        client_request_id=client_request_id, context_generation=session.context_generation,
                        model_profile_id=session.model_profile_id,
                        model_id=request.model.model_id,
                        created_at=_now(),
                    )
                    db.add(run)
                    await db.flush()
                    self._add_event(db, run, "run.accepted", {"context_generation": run.context_generation})
                    history = await self._history(db, session_id) if new_process else ()
            except BaseException:
                if new_process:
                    await self._evict(session_id, slot)
                raise
            slot.stop_requested = False
            slot.entered = False
            slot.finalizing = False
            slot.run_id = run.id
            slot.task = asyncio.create_task(
                self._execute(session_id, slot, run.id, CodingPrompt(message, history)), name=f"coding-run-{run.id}",
            )
            return run

    async def stop(self, *, user_id: int, run_id: str) -> bool:
        async with self._lock:
            async with self._db_lock, self._sessions() as db, db.begin():
                run = await self._run(db, user_id, run_id)
                if run.status not in ACTIVE_STATUSES:
                    return False
                slot = self._slots.get(run.session_id)
                if slot is None or slot.run_id != run_id or slot.task is None or slot.finalizing:
                    return False
                slot.stop_requested = True
                run.status = "stopping"
                (await db.get(CodingSession, run.session_id)).status = "stopping"
                self._add_event(db, run, "run.stopping", {})
            await self._cancel_commands(run_id)
            if slot.ready:
                try:
                    await asyncio.wait_for(slot.runtime.abort(), self.limits.stop_seconds)
                except Exception:
                    slot.task.cancel()
            else:
                if slot.entered:
                    slot.task.cancel()
            slot.changed.set()
            task = slot.task
        try:
            await asyncio.wait_for(asyncio.shield(task), self.limits.stop_seconds)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True

    async def steer(self, *, user_id: int, run_id: str, message: str, follow_up: bool = False) -> None:
        self._validate_message(message)
        if len(json.dumps({"message": message}, ensure_ascii=False).encode()) > self.limits.max_payload_bytes:
            raise CodingServiceError("invalid_request", "Coding steering message is too large")
        async with self._lock:
            async with self._db_lock, self._sessions() as db:
                run = await self._run(db, user_id, run_id)
                session = await self._session(db, user_id, run.session_id)
                await self._require_user(db, user_id)
                await self._workspace(db, user_id, session.workspace_id)
                await self._model(db, user_id, session.model_profile_id)
            slot = self._slots.get(run.session_id)
            if run.status not in {"running", "waiting_approval"} or slot is None or slot.run_id != run_id:
                raise CodingServiceError("run_inactive", "Coding run is not active")
            capabilities = slot.runtime.capabilities()
            if not (capabilities.follow_up if follow_up else capabilities.steering):
                raise CodingServiceError("unsupported", "The selected runtime does not support this action")
            action = slot.runtime.follow_up if follow_up else slot.runtime.steer
            try:
                await asyncio.wait_for(action(message), self.limits.stop_seconds)
            except Exception as exc:
                raise CodingServiceError("control_delivery_failed", "Coding control could not be delivered; check run state before retrying") from exc
            await self._record(run_id, "user.follow_up" if follow_up else "user.steered", {"message": message})

    async def resolve_approval(self, *, user_id: int, approval_id: str, approved: bool, change_hash: str | None = None, command_hash: str | None = None) -> None:
        if await self._resolve_command(user_id=user_id, approval_id=approval_id, approved=approved, command_hash=command_hash):
            return
        root = None
        if approved:
            async with self._db_lock, self._sessions() as db:
                approval = await db.get(CodingApproval, approval_id)
                if approval is None:
                    raise CodingServiceError("not_found", "Coding approval not found")
                run = await self._run(db, user_id, approval.run_id)
                if approval.change:
                    session_id = run.session_id
                    root = True
            if root:
                root = await self._execution_root(user_id=user_id, session_id=session_id)
        async with self._lock:
            async with self._db_lock, self._sessions() as db, db.begin():
                approval = await db.get(CodingApproval, approval_id)
                if approval is None:
                    raise CodingServiceError("not_found", "Coding approval not found")
                run = await self._run(db, user_id, approval.run_id)
                session = await self._session(db, user_id, run.session_id)
                await self._require_user(db, user_id)
                await self._workspace(db, user_id, session.workspace_id)
                await self._model(db, user_id, session.model_profile_id)
                if approval.status != "pending" or (approval.expires_at is not None and _utc(approval.expires_at) <= _now()):
                    raise CodingServiceError("approval_expired", "Coding approval is expired or already answered")
                slot = self._slots.get(run.session_id)
                if run.status not in {"running", "waiting_approval"} or slot is None or slot.run_id != run.id or slot.stop_requested or self._closing:
                    raise CodingServiceError("run_inactive", "Coding run is no longer active")
                change = (await db.execute(select(CodingChange).where(CodingChange.approval_id == approval_id))).scalar_one_or_none()
                if change is not None:
                    if approved and change_hash != change.after_hash:
                        raise CodingServiceError("change_review_required", "Review this exact change in an updated client before approving")
                    checkout = await self._checkout(db, run.session_id)
                    tools = await self._write_tools(db, user_id, session.model_profile_id)
                    if approved and (not session.edit_mode or checkout is None or checkout.id != change.checkout_id
                                     or checkout.status != "ready" or root != slot.request.workspace
                                     or checkout.canonical_path != str(root)
                                     or change.tool not in tools or change.status != "proposed"):
                        raise CodingServiceError("runtime_denied", "Edit authorization changed; no file was written")
                    change.status = "applying" if approved else "denied"
                approval.status = "approved" if approved else "denied"
                approval.resolved_by_user_id = user_id
                approval.resolved_at = _now()
                self._add_event(db, run, "approval.resolved", {"approval_id": approval_id, "approved": approved, "status": approval.status})
                request_id = approval.runtime_request_id
            if change is not None and approved:
                # Persist 'applying' BEFORE touching disk. A crash cannot be
                # mistaken for a verified success or silently retried.
                async with self._db_lock, self._sessions() as db, db.begin():
                    try:
                        await self._require_user(db, user_id)
                        await self._workspace(db, user_id, session.workspace_id)
                        tools = await self._write_tools(db, user_id, session.model_profile_id)
                        if change.tool not in tools or (approval.expires_at is not None and _utc(approval.expires_at) <= _now()) or slot.stop_requested:
                            raise CodingServiceError("authorization_changed", "Change approval is no longer valid")
                        changes.apply(root, change.path, change.before_text, change.after_text)
                        change_status, change_error = "applied", None
                    except (CodingServiceError, RepositoryError, OSError, UnicodeError) as exc:
                        change_status = "uncertain" if getattr(exc, "code", "") == "change_uncertain" else "failed"
                        change_error = getattr(exc, "code", "change_failed")
                        approved = False
                    stored = await db.get(CodingChange, change.id)
                    stored.status, stored.error_code = change_status, change_error
                    stored.applied_at = _now() if change_status == "applied" else None
                    self._add_event(db, await db.get(CodingRun, run.id), "change.recorded", {
                        "change_id": change.id, "path": change.path, "status": change_status,
                    })
            try:
                await asyncio.wait_for(slot.runtime.confirm(request_id, approved), self.limits.stop_seconds)
            except Exception as exc:
                slot.task.cancel()
                raise CodingServiceError("approval_delivery_failed", "Approval was recorded but could not be delivered") from exc
            if slot.deadline is not None:
                slot.deadline.resolve(approval_id)
            slot.changed.set()

    async def list_approvals(self, *, user_id: int, run_id: str) -> list[CodingApproval]:
        async with self._db_lock, self._sessions() as db:
            await self._run(db, user_id, run_id)
            events = (await db.execute(select(CodingRunEvent).where(
                CodingRunEvent.run_id == run_id, CodingRunEvent.event_type == "approval.requested",
            ).order_by(CodingRunEvent.sequence))).scalars()
            positions = {event.payload.get("approval_id"): event.sequence for event in events}
            approvals = list((await db.execute(select(CodingApproval).where(CodingApproval.run_id == run_id))).scalars())
            for approval in approvals:
                approval.request_sequence = positions.get(approval.id)
            return sorted(approvals, key=lambda item: (item.request_sequence or 2**63, _utc(item.created_at), item.id))

    async def events(
        self, *, user_id: int, run_id: str, after: int = 0, follow: bool = False,
    ) -> AsyncIterator[StoredCodingEvent]:
        if after < 0:
            raise CodingServiceError("invalid_request", "Event cursor must be nonnegative")
        while True:
            async with self._db_lock, self._sessions() as db:
                await self._require_user(db, user_id)
                run = await self._run(db, user_id, run_id)
                rows = list((await db.execute(select(CodingRunEvent).where(
                    CodingRunEvent.run_id == run_id, CodingRunEvent.sequence > after,
                ).order_by(CodingRunEvent.sequence).limit(100))).scalars())
                status = run.status
            for row in rows:
                after = row.sequence
                yield StoredCodingEvent(row.sequence, row.event_type, row.payload, row.schema_version)
            if rows:
                continue
            if not follow or status in TERMINAL_STATUSES:
                return
            slot = self._slots.get(run.session_id)
            if slot is not None:
                slot.changed.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(slot.changed.wait(), 0.25)
            else:
                await asyncio.sleep(0.1)

    async def close_session(self, *, user_id: int, session_id: str) -> None:
        async with self._lock:
            async with self._db_lock, self._sessions() as db, db.begin():
                session = await self._session(db, user_id, session_id)
                if session.status in ACTIVE_STATUSES:
                    raise CodingServiceError("session_busy", "Stop the active run before closing its session")
                checkout = await self._checkout(db, session_id)
                if checkout is not None and checkout.status in {"creating", "removing", "committing", "applying"}:
                    raise CodingServiceError("checkout_conflict", "Wait for the managed checkout operation before closing the session")
                session.status = "closed"
            slot = self._slots.get(session_id)
            if slot is not None:
                await self._evict(session_id, slot)

    async def expire_idle(self) -> int:
        async with self._lock:
            expired = [(key, slot) for key, slot in self._slots.items()
                       if slot.task is None and time.monotonic() - slot.last_used >= self.limits.idle_seconds]
            for key, slot in expired:
                await self._evict(key, slot)
            return len(expired)

    async def shutdown(self) -> None:
        async with self._lock:
            self._closing = True
            maintenance, self._maintenance = self._maintenance, None
            if maintenance is not None:
                maintenance.cancel()
            tasks = []
            checkout_ids = list(self._checkout_jobs)
            checkout_tasks = list(self._checkout_jobs.values())
            for task in checkout_tasks:
                task.cancel()
            for slot in self._slots.values():
                if slot.task is not None:
                    slot.stop_requested = True
                    if slot.entered and not slot.finalizing:
                        slot.task.cancel()
                    tasks.append(slot.task)
        if maintenance is not None:
            await asyncio.gather(maintenance, return_exceptions=True)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*checkout_tasks, return_exceptions=True)
        await self._interrupt_checkouts(checkout_ids, "backend_shutdown")
        async with self._lock:
            results = await asyncio.gather(
                *(self._evict(key, slot) for key, slot in list(self._slots.items())),
                return_exceptions=True,
            )
            if any(isinstance(result, BaseException) for result in results):
                raise CodingServiceError("cleanup_failed", "One or more coding runtimes could not be closed")

    async def _execute(self, session_id: str, slot: _RuntimeSlot, run_id: str, prompt: CodingPrompt) -> None:
        status, error_code = "failed", "incomplete_stream"
        response = ""
        pending_text = ""
        last_text_flush = 0.0
        last_reasoning_status = None
        healthy = False
        try:
            slot.entered = True
            if slot.stop_requested or self._closing:
                raise asyncio.CancelledError
            async with self._db_lock, self._sessions() as db:
                session = await self._session(db, slot.owner_id, session_id)
                await self._require_user(db, slot.owner_id)
                await self._workspace(db, slot.owner_id, slot.workspace_id)
                current_model, current_tools = await self._model(db, slot.owner_id, session.model_profile_id)
                current_writes = await self._write_tools(db, slot.owner_id, session.model_profile_id) if session.edit_mode else ()
                current_commands = await self._command_tools(db, slot.owner_id, session.model_profile_id) if session.command_mode else ()
                if current_model != slot.request.model or current_tools != slot.request.read_tools or current_writes != slot.request.write_tools or current_commands != slot.request.command_tools:
                    raise CodingRuntimeError("authorization_changed", "Coding model authorization changed before execution")
            if await self._execution_root(user_id=slot.owner_id, session_id=session_id) != slot.request.workspace:
                raise CodingRuntimeError("authorization_changed", "Coding execution root changed before startup")
            if not slot.ready:
                await asyncio.wait_for(slot.runtime.start(slot.request), self.limits.startup_seconds)
                slot.ready = True
            await self._set_running(run_id)
            await self._record(run_id, "session.ready", {
                "context_reconstructed": bool(prompt.history), "history_messages": len(prompt.history),
            })
            slot.deadline = RunDeadline(self.limits.run_seconds)
            async with slot.deadline:
                async for event in slot.runtime.run(prompt):
                    if event.kind == "interaction.requested":
                        await self._approval(run_id, slot, event.payload)
                        continue
                    payload = _safe_payload(event)
                    if payload is None:
                        continue
                    if event.kind == "assistant.reasoning_status":
                        if payload.get("status") == last_reasoning_status:
                            continue
                        last_reasoning_status = payload.get("status")
                    if event.kind == "assistant.text_delta":
                        response += payload.get("delta", "")
                    elif event.kind == "assistant.message":
                        response = payload.get("text", "")
                    if len(response) > self.limits.max_response_chars:
                        raise CodingRuntimeError("response_limit", "Coding response exceeded its character limit")
                    if event.kind == "assistant.text_delta":
                        pending_text += payload.get("delta", "")
                        if len(pending_text) < 1024 and time.monotonic() - last_text_flush < 0.2:
                            continue
                        await self._record(run_id, "assistant.text_delta", {"delta": pending_text})
                        pending_text = ""
                        last_text_flush = time.monotonic()
                        slot.changed.set()
                        continue
                    if pending_text:
                        await self._record(run_id, "assistant.text_delta", {"delta": pending_text})
                        pending_text = ""
                        last_text_flush = time.monotonic()
                        slot.changed.set()
                    if event.kind == "run.completed":
                        status = "cancelled" if slot.stop_requested or payload.get("aborted") else "completed"
                        error_code = None
                        healthy = True
                        break
                    if event.kind in {"run.failed", "session.failed"}:
                        if slot.stop_requested:
                            status, error_code = "cancelled", None
                        else:
                            error_code = "model_failed" if event.kind == "run.failed" else "runtime_failed"
                        break
                    await self._record(run_id, event.kind, payload)
                    slot.changed.set()
        except asyncio.CancelledError:
            status = "interrupted" if self._closing else "cancelled"
            error_code = "server_shutdown" if self._closing else None
        except TimeoutError:
            error_code = "execution_timeout" if slot.ready else "startup_timeout"
        except CodingRuntimeError as exc:
            error_code = exc.code
        except CodingServiceError as exc:
            error_code = exc.code
        except Exception:
            error_code = "runtime_failed"
        finally:
            slot.finalizing = True
            async with self._lock:
                if not healthy:
                    try:
                        await asyncio.wait_for(slot.runtime.close(), self.limits.stop_seconds)
                    except Exception:
                        slot.quarantined = True
                    slot.ready = False
                await self._finish(run_id, status, error_code, response)
                slot.task = None
                slot.run_id = None
                slot.last_used = time.monotonic()
                slot.changed.set()
                if not healthy and not slot.quarantined and self._slots.get(session_id) is slot:
                    self._slots.pop(session_id)

    async def _approval(self, run_id: str, slot: _RuntimeSlot, payload: dict) -> None:
        if payload.get("method") == "input":
            await self._command_approval(run_id, slot, payload)
            return
        if payload.get("title") == "Approve container command?":
            raise CodingRuntimeError("command_invalid", "Container commands require the host result protocol")
        if payload.get("method") != "confirm" or not slot.runtime.capabilities().confirmations:
            raise CodingRuntimeError("unsupported_interaction", "Runtime requested an unsupported interaction")
        request_id = str(payload.get("id") or "")
        if not request_id or len(request_id) > 100:
            raise CodingRuntimeError("protocol_violation", "Runtime supplied an invalid interaction identifier")
        timeout = payload.get("timeout")
        expiry = None if timeout is None else _now() + timedelta(
            seconds=min(max(float(timeout) / 1000, 0.01), self.limits.run_seconds))
        summary = {key: str(payload.get(key) or "")[:2000] for key in ("title", "message")}
        proposal = None
        message = payload.get("message")
        if isinstance(message, str) and len(message.encode("utf-8")) <= changes.PROPOSAL_BYTES:
            try:
                candidate = json.loads(message)
                operation_id = candidate.get("operation_id") if isinstance(candidate, dict) else None
                if isinstance(operation_id, str) and 0 < len(operation_id) <= 200:
                    summary["operation_id"] = operation_id
                if isinstance(candidate, dict) and candidate.get("protocol") == changes.CHANGE_PROTOCOL:
                    proposal = candidate
            except ValueError:
                pass
        if payload.get("title") == "Approve file change?" and proposal is None:
            raise CodingRuntimeError("change_invalid", "A complete file-change proposal is required")
        if proposal is not None:
            if proposal.get("tool") not in slot.request.write_tools or slot.stop_requested or self._closing:
                raise CodingRuntimeError("runtime_denied", "This run cannot propose file changes")
            root = await self._execution_root(user_id=slot.owner_id, session_id=await self._run_session(run_id))
            try:
                changes.validate(root, proposal.get("path"), proposal.get("before"), proposal.get("after"))
            except (RepositoryError, OSError, UnicodeError) as exc:
                raise CodingRuntimeError("change_invalid", "The proposed file change could not be validated") from exc
        async with self._db_lock, self._sessions() as db, db.begin():
            run = await db.get(CodingRun, run_id)
            if slot.stop_requested or run.status == "stopping":
                raise CodingRuntimeError("run_inactive", "Run stopped before approval")
            approval = CodingApproval(
                run_id=run_id, runtime_request_id=request_id, payload_json=json.dumps(summary),
                expires_at=expiry,
            )
            db.add(approval)
            await db.flush()
            if proposal is not None:
                checkout = await self._checkout(db, run.session_id)
                if checkout is None or checkout.status != "ready":
                    raise CodingRuntimeError("runtime_denied", "Edits require a ready checkout")
                count = len(list((await db.execute(select(CodingChange.id).where(CodingChange.run_id == run_id))).scalars()))
                if count >= 32:
                    raise CodingRuntimeError("change_limit", "This run reached its file-change limit")
                change = CodingChange(
                    id=str(uuid4()), run_id=run_id, approval_id=approval.id, checkout_id=checkout.id,
                    path=proposal["path"], tool=proposal["tool"], before_text=proposal.get("before"), after_text=proposal["after"],
                    before_hash=changes.digest(proposal.get("before")), after_hash=changes.digest(proposal["after"]),
                    diff=changes.patch(proposal["path"], proposal.get("before"), proposal["after"]), status="proposed",
                )
                db.add(change)
                summary = {**summary, "title": "Approve file change?", "message": f"{change.tool}: {change.path}", "change": {
                    "id": change.id, "path": change.path, "diff": change.diff,
                    "before_hash": change.before_hash, "after_hash": change.after_hash,
                }}
                if payload_size(summary) > changes.APPROVAL_BYTES:
                    raise CodingRuntimeError("change_limit", "Change preview exceeds the complete approval limit")
                approval.payload_json = json.dumps(summary)
            if run.status != "stopping":
                run.status = "waiting_approval"
                (await db.get(CodingSession, run.session_id)).status = "waiting_approval"
            # Include complete public approval data in the stream so rendering
            # another prompt never waits for a second HTTP request to finish.
            public = {key: summary[key] for key in ("title", "message", "change") if key in summary}
            event_payload = {
                "approval_id": approval.id, "expires_at": approval.expires_at.isoformat() if approval.expires_at else None, **public,
            }
            approval_limit = changes.APPROVAL_BYTES if proposal is not None else self.limits.max_payload_bytes
            if payload_size(event_payload) > approval_limit:
                raise CodingRuntimeError("change_limit", "Complete approval metadata exceeds its event limit")
            self._add_event(db, run, "approval.requested", event_payload)
        if slot.deadline is not None:
            slot.deadline.pause(approval.id)
        slot.changed.set()

    async def _record(self, run_id: str, kind: str, payload: dict) -> None:
        if kind in {"operation.output", "operation.completed"}:
            payload = operation_preview(payload, self.limits.max_payload_bytes)
        if payload_size(payload) > self.limits.max_payload_bytes:
            if kind in {"assistant.message", "assistant.text_delta"}:
                field = "text" if kind == "assistant.message" else "delta"
                text = payload.get(field)
                if isinstance(text, str):
                    if kind == "assistant.message":
                        # Preserve snapshot/reset semantics without truncating the answer.
                        reset = {**payload, "text": ""}
                        if payload_size(reset) > self.limits.max_payload_bytes:
                            raise CodingRuntimeError("event_payload_limit", "Coding text metadata exceeded its byte limit")
                        await self._record(run_id, kind, reset)
                    while text:
                        prefix = text_prefix(text, {}, "delta", self.limits.max_payload_bytes)
                        if not prefix:
                            raise CodingRuntimeError("event_payload_limit", "Coding text metadata exceeded its byte limit")
                        await self._record(run_id, "assistant.text_delta", {"delta": prefix})
                        text = text[len(prefix):]
                    return
            raise CodingRuntimeError("event_payload_limit", "Coding event metadata exceeded its byte limit")
        async with self._db_lock, self._sessions() as db, db.begin():
            run = await db.get(CodingRun, run_id)
            if run.last_event_sequence >= self.limits.max_run_events:
                raise CodingRuntimeError("event_limit", "Coding event count exceeded its limit")
            self._add_event(db, run, kind, payload)
            if kind == "operation.completed" and run.status in {"running", "waiting_approval"}:
                if operation_id := payload.get("operation_id"):
                    await self._resolve_pending(db, run_id, operation_id=operation_id)
                pending = (await db.execute(select(CodingApproval.id).where(
                    CodingApproval.run_id == run_id, CodingApproval.status == "pending",
                ).limit(1))).scalar_one_or_none()
                run.status = "waiting_approval" if pending else "running"
                (await db.get(CodingSession, run.session_id)).status = run.status

    def _add_event(self, db: AsyncSession, run: CodingRun, kind: str, payload: dict) -> None:
        run.last_event_sequence += 1
        db.add(CodingRunEvent(
            run_id=run.id, sequence=run.last_event_sequence, schema_version=1,
            event_type=kind, payload_json=json.dumps(payload, ensure_ascii=False),
        ))

    async def _set_running(self, run_id: str) -> None:
        async with self._db_lock, self._sessions() as db, db.begin():
            run = await db.get(CodingRun, run_id)
            if run.status != "stopping":
                run.status = "running"
                (await db.get(CodingSession, run.session_id)).status = "running"

    async def _finish(self, run_id: str, status: str, error_code: str | None, response: str) -> None:
        await self._cancel_commands(run_id)
        async with self._db_lock, self._sessions() as db, db.begin():
            run = await db.get(CodingRun, run_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return
            run.status, run.error_code = status, error_code
            run.response_text = response[:self.limits.max_response_chars]
            run.finished_at = _now()
            session = await db.get(CodingSession, run.session_id)
            session.status = "interrupted" if status == "interrupted" else "idle"
            session.updated_at = _now()
            await self._resolve_pending(db, run_id)
            await self._recover_command_records(db, run_id, reason=error_code or status)
            self._add_event(db, run, f"run.{status}", {"error_code": error_code})

    async def _resolve_pending(self, db: AsyncSession, run_id: str, *, operation_id: str | None = None) -> None:
        approvals = (await db.execute(select(CodingApproval).where(
            CodingApproval.run_id == run_id, CodingApproval.status == "pending",
        ))).scalars()
        resolved = []
        for approval in approvals:
            if operation_id is not None and json.loads(approval.payload_json).get("operation_id") != operation_id:
                continue
            approval.status = "expired" if approval.expires_at is not None and _utc(approval.expires_at) <= _now() else "cancelled"
            approval.resolved_at = _now()
            summary = json.loads(approval.payload_json)
            if "command" in summary:
                summary["command"]["status"] = approval.status
                approval.payload_json = json.dumps(summary)
            resolved.append(approval.id)
            run = await db.get(CodingRun, run_id)
            slot = self._slots.get(run.session_id)
            if slot is not None and slot.run_id == run_id and slot.deadline is not None:
                slot.deadline.resolve(approval.id)
            self._add_event(db, await db.get(CodingRun, run_id), "approval.resolved", {
                "approval_id": approval.id, "status": approval.status,
            })
        query = update(CodingChange).where(CodingChange.run_id == run_id, CodingChange.status == "proposed")
        if operation_id is not None:
            query = query.where(CodingChange.approval_id.in_(resolved))
        await db.execute(query.values(status="cancelled"))

    async def _run_session(self, run_id: str) -> str:
        async with self._db_lock, self._sessions() as db:
            return (await db.get(CodingRun, run_id)).session_id

    async def _write_tools(self, db: AsyncSession, user_id: int, profile_id: int | None) -> tuple[str, ...]:
        await self._model(db, user_id, profile_id)
        profile = await db.get(ModelProfile, profile_id)
        return tuple("fruitcake_" + tool for tool in ("edit", "write") if tool not in profile.blocked_tools
                     and "fruitcake_" + tool not in profile.blocked_tools and (not profile.allowed_tools
                     or tool in profile.allowed_tools or "fruitcake_" + tool in profile.allowed_tools))

    async def _evict(self, session_id: str, slot: _RuntimeSlot) -> None:
        slot.quarantined = True
        await asyncio.wait_for(slot.runtime.close(), self.limits.stop_seconds)
        self._slots.pop(session_id, None)

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(min(self.limits.idle_seconds, 30))
            with suppress(Exception):
                await self.expire_idle()

    async def _history(self, db: AsyncSession, session_id: str) -> tuple[tuple[str, str], ...]:
        runs = list((await db.execute(select(CodingRun).where(
            CodingRun.session_id == session_id, CodingRun.status == "completed",
        ).order_by(CodingRun.created_at.desc(), CodingRun.id.desc()).limit(3))).scalars())
        groups = []
        total_chars = 0
        for run in runs:
            controls = list((await db.execute(select(CodingRunEvent).where(
                CodingRunEvent.run_id == run.id,
                CodingRunEvent.event_type.in_(("user.steered", "user.follow_up")),
            ).order_by(CodingRunEvent.sequence.desc()).limit(3))).scalars())
            group = [("user", run.prompt[:2000])]
            group.extend(("user", event.payload["message"][:1000]) for event in reversed(controls))
            group.append(("assistant", run.response_text[:3000]))
            size = sum(len(content) for _, content in group)
            if total_chars + size > 15000:
                break
            groups.append(group)
            total_chars += size
        return tuple(message for group in reversed(groups) for message in group)

    def _validate_message(self, message: str) -> None:
        if not isinstance(message, str) or not message.strip() or len(message) > self.limits.max_prompt_chars:
            raise CodingServiceError("invalid_request", "Coding prompt is empty or too large")

    async def _require_user(self, db: AsyncSession, user_id: int) -> User:
        user = await db.get(User, user_id)
        if user is None or not user.is_active or user.role in {"guest", "restricted"}:
            raise CodingServiceError("access_denied", "Coding access is unavailable for this user")
        return user

    async def _session(self, db: AsyncSession, user_id: int, session_id: str) -> CodingSession:
        session = (await db.execute(select(CodingSession).where(
            CodingSession.id == session_id, CodingSession.user_id == user_id,
        ))).scalar_one_or_none()
        if session is None:
            raise CodingServiceError("not_found", "Coding session not found")
        if session.deleted_at is not None:
            raise CodingServiceError("not_found", "Coding session not found")
        return session

    async def _run(self, db: AsyncSession, user_id: int, run_id: str) -> CodingRun:
        run = (await db.execute(select(CodingRun).where(
            CodingRun.id == run_id, CodingRun.user_id == user_id,
        ))).scalar_one_or_none()
        if run is None:
            raise CodingServiceError("not_found", "Coding run not found")
        await self._session(db, user_id, run.session_id)
        return run

    async def _workspace(self, db: AsyncSession, user_id: int, workspace_id: str) -> CodingWorkspace:
        workspace = await db.get(CodingWorkspace, workspace_id)
        if workspace is None or workspace.user_id != user_id or not workspace.is_active:
            raise CodingServiceError("workspace_denied", "Coding workspace is not authorized")
        try:
            resolved = Path(workspace.canonical_path).resolve(strict=True)
            if str(resolved) != workspace.canonical_path or not resolved.is_dir():
                raise ValueError("workspace moved")
        except (OSError, ValueError) as exc:
            raise CodingServiceError("workspace_denied", "Coding workspace is missing or changed") from exc
        return workspace

    async def _model(self, db: AsyncSession, user_id: int, profile_id: int | None) -> tuple[CodingModel, tuple[str, ...]]:
        profile = await db.get(ModelProfile, profile_id) if profile_id is not None else None
        policy = list((await db.execute(select(UserModelAccess).where(UserModelAccess.user_id == user_id))).scalars())
        if profile is None or not profile.enabled or (policy and not any(
            row.model_profile_id == profile_id and row.allowed for row in policy
        )):
            raise CodingServiceError("model_denied", "Coding model profile is unavailable for this user")
        if not profile.supports_text or not profile.supports_tools or profile.tool_mode == "text_only":
            raise CodingServiceError("model_denied", "Coding requires a tool-capable text model")
        tools = tuple(tool for tool in READ_TOOLS if tool not in profile.blocked_tools and (
            not profile.allowed_tools or tool in profile.allowed_tools
        ))
        if not tools:
            raise CodingServiceError("model_denied", "Coding inspection tools are disabled for this model")
        return CodingModel(
            profile.id, profile.model_id, profile.provider_family, profile.is_local,
            profile.context_window_tokens, profile.output_reserve_tokens,
        ), tools


_EVENT_FIELDS = {
    "run.started": (), "turn.started": (), "turn.completed": (),
    "assistant.text_delta": ("delta",), "assistant.message": ("text", "stop_reason"),
    "assistant.reasoning_status": ("status",),
    "operation.started": ("operation_id", "tool", "arguments"),
    "operation.output": ("operation_id", "tool", "text", "mode", "preview_truncated", "original_text_bytes"),
    "operation.completed": ("operation_id", "tool", "text", "is_error", "duration_ms", "preview_truncated", "original_text_bytes"),
    "run.retrying": ("attempt", "delay_ms"),
    "run.completed": ("aborted", "handled"), "run.failed": (), "session.failed": (),
    "runtime.protocol_error": (),
}


def _safe_payload(event: CodingRuntimeEvent) -> dict | None:
    keys = _EVENT_FIELDS.get(event.kind)
    if keys is None:
        return None
    payload = {key: event.payload[key] for key in keys if key in event.payload}
    if event.kind == "operation.started" and isinstance(payload.get("arguments"), dict):
        payload["arguments"] = {key: value for key, value in payload["arguments"].items()
                                if key in {"path", "pattern", "glob", "ignoreCase", "literal", "context", "limit", "offset"}}
    return payload


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
