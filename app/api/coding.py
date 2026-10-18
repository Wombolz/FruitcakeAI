"""Authenticated coding sessions, explicit host actions, and durable SSE replay."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
import json
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.routing import APIRoute
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_admin, security
from app.coding_runtime.service import CodingServiceError, CodingSessionService
from app.db.models import User
from app.db.session import get_db

log = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 15
MAX_CURSOR = 2**63 - 1
ERROR_STATUS = {
    "command_review_required": 409, "container_unavailable": 503,
    "not_found": 404,
    "access_denied": 403, "workspace_denied": 403, "model_denied": 403, "runtime_denied": 403,
    "invalid_request": 422, "command_invalid": 409,
    "request_conflict": 409, "session_closed": 409, "session_busy": 409,
    "capacity_reached": 409, "run_inactive": 409, "approval_expired": 409, "unsupported": 409,
    "shutting_down": 503, "runtime_unavailable": 503,
    "approval_delivery_failed": 502, "control_delivery_failed": 502, "cleanup_failed": 503,
    "not_repository": 409, "repository_changed": 409, "unsupported_file": 409,
    "repository_limit": 413, "repository_timeout": 504, "repository_unavailable": 503,
    "checkout_conflict": 409, "checkout_unavailable": 409, "checkout_invalid": 409,
    "change_review_required": 409,
    "repository_creation_failed": 409,
}


class CodingRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                response = await handler(request)
                response.headers.setdefault("Cache-Control", "no-store")
                return response
            except CodingServiceError as exc:
                raise HTTPException(ERROR_STATUS.get(exc.code, 500), {
                    "code": exc.code, "message": str(exc),
                }) from exc

        return handle


router = APIRouter(route_class=CodingRoute)


async def coding_user(
    credentials=Depends(security),
    db: AsyncSession = Depends(get_db, scope="function"),
) -> User:
    # Close the authentication transaction before a potentially long-lived stream.
    user = await get_current_user(credentials, db)
    if user.role in {"guest", "restricted"}:
        raise CodingServiceError("access_denied", "Coding access is unavailable for this user")
    return user


Owner = Annotated[User, Depends(coding_user)]
Identifier = Annotated[str, Path(min_length=1, max_length=64)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0)]


def coding_service(request: Request, user: Owner) -> CodingSessionService:
    service = getattr(request.app.state, "coding_sessions", None)
    if service is None:
        raise HTTPException(503, {"code": "coding_unavailable", "message": "Coding runtime is disabled or not started"})
    return service


Service = Annotated[CodingSessionService, Depends(coding_service)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class WorkspaceGrant(Input):
    user_id: int = Field(gt=0)
    path: str = Field(min_length=1, max_length=4096)
    require_repository: bool = False


class RepositoryCreate(Input):
    user_id: int = Field(gt=0)
    path: str = Field(min_length=1, max_length=4096)


class WorkspaceOut(Output):
    id: str
    user_id: int
    canonical_path: str
    is_active: bool
    created_at: datetime


class SessionCreate(Input):
    workspace_id: str = Field(min_length=1, max_length=64)
    model_profile_id: int = Field(gt=0)
    project_id: int | None = Field(default=None, gt=0)
    title: str = Field(default="Coding session", min_length=1, max_length=255)
    checkout_id: str | None = Field(default=None, min_length=1, max_length=64)


class SessionOut(Output):
    id: str
    workspace_id: str
    model_profile_id: int | None
    project_id: int | None
    title: str
    status: str
    context_generation: int
    edit_mode: bool = False
    command_mode: bool = False
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RunCreate(Input):
    message: str = Field(min_length=1, max_length=32000)
    client_request_id: str = Field(min_length=1, max_length=100)


class RunOut(Output):
    id: str
    session_id: str
    client_request_id: str | None
    status: str
    prompt: str
    response_text: str
    context_generation: int
    model_profile_id: int | None = None
    model_id: str | None = None
    last_event_sequence: int
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None


class RunControl(Input):
    action: Literal["steer", "follow_up"]
    message: str = Field(min_length=1, max_length=32000)


class ApprovalDecision(Input):
    approved: bool
    change_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    command_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class EditMode(Input):
    enabled: bool


class ChangePreview(Output):
    id: str
    path: str
    diff: str
    before_hash: str | None
    after_hash: str


class ChangeOut(ChangePreview):
    run_id: str
    tool: str
    status: str
    error_code: str | None
    created_at: datetime
    applied_at: datetime | None


class ApprovalOut(Output):
    id: str
    run_id: str
    status: str
    title: str
    message: str
    created_at: datetime
    expires_at: datetime | None
    resolved_at: datetime | None
    change: ChangePreview | None = None
    command: dict | None = None
    request_sequence: int | None = None


class StopOut(Output):
    stop_requested: bool


class StatusOut(Output):
    enabled: bool
    mode: Literal["read_only"] = "read_only"
    event_schema_version: int = 1
    managed_checkouts: bool = False
    approved_edits: bool = False
    container_commands: bool = False


class ModelOut(Output):
    id: int
    label: str
    model_id: str


class ChangedFileOut(Output):
    path: str
    index_status: str
    worktree_status: str
    original_path: str | None


class RepositoryOut(Output):
    basis: Literal["current_worktree"]
    observed_at: datetime
    head: str | None
    files: list[ChangedFileOut]


class DiffSectionOut(Output):
    kind: Literal["staged", "unstaged", "untracked"]
    text: str
    truncated: bool


class RepositoryDiffOut(Output):
    basis: Literal["current_worktree"]
    observed_at: datetime
    path: str
    sections: list[DiffSectionOut]


class CheckoutOut(Output):
    id: str
    session_id: str
    canonical_path: str
    baseline_commit: str | None
    status: str
    error_code: str | None
    workspace_id: str | None = None
    head_commit: str | None = None
    branch_name: str | None = None


def session_checkout(row, session_id):
    # The session route describes this attachment, not creation provenance.
    return None if row is None else CheckoutOut.model_validate(row).model_copy(update={"session_id": session_id})


class ModelChange(Input):
    model_profile_id: int = Field(gt=0)


class ArchiveChange(Input):
    archived: bool


class DeliveryInput(Input):
    action: Literal["commit", "apply"]
    files: list[str] = Field(default_factory=list, max_length=200)
    message: str = Field(default="", max_length=4096)
    review_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


@router.get("/status", response_model=StatusOut)
async def status(request: Request, user: Owner):
    service = getattr(request.app.state, "coding_sessions", None)
    return StatusOut(enabled=service is not None, managed_checkouts=service is not None and service.supports_managed_checkouts,
                     approved_edits=service is not None and service.supports_managed_checkouts,
                     container_commands=service is not None and service.supports_container_commands)


@router.get("/models", response_model=list[ModelOut])
async def models(user: Owner, service: Service):
    return await service.list_models(user_id=user.id)


@router.get("/workspaces", response_model=list[WorkspaceOut])
async def workspaces(user: Owner, service: Service):
    return await service.list_workspaces(user_id=user.id)


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201)
async def grant_workspace(body: WorkspaceGrant, user: Owner, service: Service):
    await require_admin(user)
    try:
        return await service.approve_workspace(operator_id=user.id, **body.model_dump())
    except (ValueError, OSError) as exc:
        raise HTTPException(422, {"code": "invalid_workspace", "message": "Select an existing, permitted repository directory"}) from exc


@router.post("/repositories", response_model=WorkspaceOut, status_code=201)
async def create_repository(body: RepositoryCreate, user: Owner, service: Service):
    await require_admin(user)
    try:
        return await service.create_repository(operator_id=user.id, **body.model_dump())
    except (ValueError, OSError) as exc:
        raise HTTPException(422, {"code": "invalid_workspace", "message": "Choose a new folder inside an existing, permitted parent directory on the backend machine"}) from exc


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def revoke_workspace(workspace_id: Identifier, user: Owner, service: Service):
    await require_admin(user)
    await service.revoke_workspace(operator_id=user.id, workspace_id=workspace_id)
    return Response(status_code=204)


@router.get("/sessions", response_model=list[SessionOut])
async def sessions(user: Owner, service: Service, limit: PageLimit = 50, offset: PageOffset = 0, archived: bool = False):
    return await service.list_sessions(user_id=user.id, limit=limit, offset=offset, archived=archived)


@router.get("/checkouts", response_model=list[CheckoutOut])
async def reusable_checkouts(user: Owner, service: Service):
    return await service.list_checkouts(user_id=user.id)


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(body: SessionCreate, user: Owner, service: Service):
    return await service.create_session(user_id=user.id, **body.model_dump())


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def session(session_id: Identifier, user: Owner, service: Service):
    return await service.get_session(user_id=user.id, session_id=session_id)


@router.post("/sessions/{session_id}/close", status_code=204)
async def close_session(session_id: Identifier, user: Owner, service: Service):
    await service.close_session(user_id=user.id, session_id=session_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/model", response_model=SessionOut)
async def change_model(session_id: Identifier, body: ModelChange, user: Owner, service: Service):
    return await service.change_model(user_id=user.id, session_id=session_id, **body.model_dump())


@router.post("/sessions/{session_id}/archive", response_model=SessionOut)
async def archive_session(session_id: Identifier, body: ArchiveChange, user: Owner, service: Service):
    return await service.archive_session(user_id=user.id, session_id=session_id, archived=body.archived)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: Identifier, user: Owner, service: Service):
    await service.delete_session(user_id=user.id, session_id=session_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/delivery")
async def delivery(session_id: Identifier, body: DeliveryInput, user: Owner, service: Service):
    return await service.delivery(user_id=user.id, session_id=session_id, **body.model_dump())


@router.post("/sessions/{session_id}/edit-mode", response_model=SessionOut)
async def edit_mode(session_id: Identifier, body: EditMode, user: Owner, service: Service):
    return await service.set_edit_mode(user_id=user.id, session_id=session_id, enabled=body.enabled)


@router.get("/runs/{run_id}/changes", response_model=list[ChangeOut])
async def saved_changes(run_id: Identifier, user: Owner, service: Service):
    return await service.list_changes(user_id=user.id, run_id=run_id)


@router.post("/sessions/{session_id}/command-mode", response_model=SessionOut)
async def command_mode(session_id: Identifier, body: EditMode, user: Owner, service: Service):
    return await service.set_command_mode(user_id=user.id, session_id=session_id, enabled=body.enabled)


@router.get("/runs/{run_id}/commands")
async def saved_commands(run_id: Identifier, user: Owner, service: Service):
    return await service.list_commands(user_id=user.id, run_id=run_id)


@router.get("/sessions/{session_id}/repository", response_model=RepositoryOut)
async def repository(
    session_id: Identifier, user: Owner, service: Service,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
):
    user_id = user.id
    # Unlike SSE, this route awaits its subprocess before returning a response.
    # Release the cached authentication session before that potentially long wait.
    await db.close()
    return await service.inspect_repository(user_id=user_id, session_id=session_id)


@router.get("/sessions/{session_id}/checkout", response_model=CheckoutOut | None)
async def checkout(session_id: Identifier, user: Owner, service: Service):
    return session_checkout(await service.get_checkout(user_id=user.id, session_id=session_id), session_id)


@router.post("/sessions/{session_id}/checkout", response_model=CheckoutOut, status_code=202)
async def create_checkout(session_id: Identifier, user: Owner, service: Service, body: Input = Body(default=Input())):
    return session_checkout(await service.create_checkout(user_id=user.id, session_id=session_id), session_id)


@router.post("/sessions/{session_id}/checkout/remove", response_model=CheckoutOut, status_code=202)
async def remove_checkout(session_id: Identifier, user: Owner, service: Service, body: Input = Body(default=Input())):
    return session_checkout(await service.remove_checkout(user_id=user.id, session_id=session_id), session_id)


@router.get("/sessions/{session_id}/repository/diff", response_model=RepositoryDiffOut)
async def repository_diff(
    session_id: Identifier, user: Owner, service: Service,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
):
    user_id = user.id
    await db.close()
    return await service.inspect_repository(user_id=user_id, session_id=session_id, path=path)


@router.get("/sessions/{session_id}/runs", response_model=list[RunOut])
async def runs(session_id: Identifier, user: Owner, service: Service, limit: PageLimit = 50, offset: PageOffset = 0):
    return await service.list_runs(user_id=user.id, session_id=session_id, limit=limit, offset=offset)


@router.post("/sessions/{session_id}/runs", response_model=RunOut, status_code=202)
async def submit(session_id: Identifier, body: RunCreate, user: Owner, service: Service):
    return await service.submit(user_id=user.id, session_id=session_id, **body.model_dump())


@router.get("/runs/{run_id}", response_model=RunOut)
async def run(run_id: Identifier, user: Owner, service: Service):
    return await service.get_run(user_id=user.id, run_id=run_id)


@router.post("/runs/{run_id}/stop", response_model=StopOut)
async def stop(run_id: Identifier, user: Owner, service: Service):
    return StopOut(stop_requested=await service.stop(user_id=user.id, run_id=run_id))


@router.post("/runs/{run_id}/control", status_code=204)
async def control(run_id: Identifier, body: RunControl, user: Owner, service: Service):
    await service.steer(user_id=user.id, run_id=run_id, message=body.message, follow_up=body.action == "follow_up")
    return Response(status_code=204)


@router.get("/runs/{run_id}/approvals", response_model=list[ApprovalOut])
async def approvals(run_id: Identifier, user: Owner, service: Service):
    rows = await service.list_approvals(user_id=user.id, run_id=run_id)
    result = []
    for row in rows:
        summary = json.loads(row.payload_json)
        result.append(ApprovalOut(
            id=row.id, run_id=row.run_id, status=row.status,
            title=summary.get("title", ""), message=summary.get("message", ""),
            created_at=row.created_at, expires_at=row.expires_at, resolved_at=row.resolved_at,
            change=summary.get("change"),
            command=summary.get("command"),
            request_sequence=row.request_sequence,
        ))
    return result


@router.post("/approvals/{approval_id}/resolve", status_code=204)
async def resolve_approval(approval_id: Identifier, body: ApprovalDecision, user: Owner, service: Service):
    await service.resolve_approval(user_id=user.id, approval_id=approval_id, approved=body.approved, change_hash=body.change_hash, command_hash=body.command_hash)
    return Response(status_code=204)


def _cursor(after: int, last_event_id: str | None) -> int:
    if last_event_id is None:
        return after
    if not last_event_id.isascii() or not last_event_id.isdecimal() or len(last_event_id) > 19:
        raise CodingServiceError("invalid_request", "Invalid Last-Event-ID")
    cursor = int(last_event_id)
    if cursor > MAX_CURSOR:
        raise CodingServiceError("invalid_request", "Invalid Last-Event-ID")
    return max(after, cursor)


async def _event_stream(service: CodingSessionService, *, user_id: int, run_id: str, after: int, follow: bool):
    events = service.events(user_id=user_id, run_id=run_id, after=after, follow=follow)
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(events))
            done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            data = json.dumps({
                "schema_version": event.schema_version, "run_id": run_id,
                "sequence": event.sequence, "kind": event.kind, "payload": event.payload,
            }, ensure_ascii=False, separators=(",", ":"))
            yield f"id: {event.sequence}\nevent: {event.kind}\ndata: {data}\n\n"
    except CodingServiceError as exc:
        yield f'event: stream.error\ndata: {json.dumps({"code": exc.code, "message": str(exc)})}\n\n'
    except Exception:
        log.warning("Coding event stream failed", extra={"run_id": run_id})
        yield 'event: stream.error\ndata: {"code":"stream_unavailable","message":"Reconnect to resume coding events"}\n\n'
    finally:
        # A disconnected observer owns only its read task, never the coding run.
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        with suppress(Exception):
            await events.aclose()


@router.get("/runs/{run_id}/events", response_class=StreamingResponse, responses={
    200: {"description": "Durable SSE replay and optional live following", "content": {"text/event-stream": {}}},
})
async def events(
    run_id: Identifier, user: Owner, service: Service,
    after: Annotated[int, Query(ge=0, le=MAX_CURSOR)] = 0,
    follow: bool = True,
    last_event_id: Annotated[str | None, Header()] = None,
):
    cursor = _cursor(after, last_event_id)
    current = await service.get_run(user_id=user.id, run_id=run_id)
    if cursor > current.last_event_sequence:
        raise HTTPException(409, {"code": "cursor_ahead", "message": "Event cursor is ahead of this run"})
    return StreamingResponse(
        _event_stream(service, user_id=user.id, run_id=run_id, after=cursor, follow=follow),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
