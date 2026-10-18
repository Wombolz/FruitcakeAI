"""Durable approvals and results for host-brokered container commands."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4

from sqlalchemy import select

from app.coding_runtime import container_commands as commands
from app.coding_runtime.contracts import CodingRuntimeError
from app.db.models import CodingApproval, CodingRun, CodingSession, ModelProfile


def now():
    return datetime.now(timezone.utc)


def error(code, message):
    from app.coding_runtime.service import CodingServiceError
    return CodingServiceError(code, message)


class ContainerCommandService:
    @property
    def supports_container_commands(self):
        return self._commands is not None

    async def _command_tools(self, db, user_id, profile_id):
        await self._model(db, user_id, profile_id)
        profile = await db.get(ModelProfile, profile_id)
        aliases = {commands.TOOL, "bash", "shell_exec"}
        return (commands.TOOL,) if not aliases.intersection(profile.blocked_tools) and (
            not profile.allowed_tools or aliases.intersection(profile.allowed_tools)) else ()

    async def set_command_mode(self, *, user_id, session_id, enabled):
        if self._commands is None:
            raise error("unsupported", "Container commands are not configured")
        if enabled:
            await self._execution_root(user_id=user_id, session_id=session_id)
            if self._command_cleanup_failed:
                raise error("container_unavailable", "Docker recovery/cleanup failed; inspect Docker and restart the backend before enabling commands")
            try:
                await self._commands.image_id()
            except (CodingRuntimeError, OSError, TimeoutError) as exc:
                raise error("container_unavailable", "Start Docker and install the configured image explicitly; no host fallback or automatic pull is allowed") from exc
        async with self._lock:
            async with self._db_lock, self._sessions() as db, db.begin():
                await self._require_user(db, user_id)
                session = await self._session(db, user_id, session_id)
                await self._workspace(db, user_id, session.workspace_id)
                checkout = await self._checkout(db, session_id)
                if session.status not in {"idle", "interrupted"} or self._closing:
                    raise error("session_busy", "Change command mode only while idle")
                if enabled and (checkout is None or checkout.status != "ready" or not await self._command_tools(db, user_id, session.model_profile_id)):
                    raise error("runtime_denied", "Container commands require a ready checkout and an authorized model")
                session.command_mode = enabled
                session.updated_at = now()
            if slot := self._slots.get(session_id):
                await self._evict(session_id, slot)
            return session

    async def _command_approval(self, run_id, slot, payload):
        if (payload.get("method") != "input" or payload.get("title") != commands.TITLE or self._commands is None
                or not slot.request.command_tools or not slot.runtime.capabilities().container_commands
                or slot.stop_requested or self._closing or self._command_cleanup_failed):
            raise CodingRuntimeError("runtime_denied", "Container command requests are unavailable")
        request_id, message = payload.get("id"), payload.get("placeholder")
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 100 or not isinstance(message, str) or len(message.encode()) > commands.PROPOSAL_BYTES:
            raise CodingRuntimeError("command_invalid", "Invalid container command request")
        try:
            proposal = json.loads(message)
        except ValueError as exc:
            raise CodingRuntimeError("command_invalid", "Invalid container command proposal") from exc
        inputs = commands.validate_input(proposal)
        root = await self._execution_root(user_id=slot.owner_id, session_id=await self._run_session(run_id))
        commands.working_directory(root, inputs["working_directory"])
        image = await self._commands.image_id()
        preview = {"id": str(uuid4()), **inputs, "image": image, "network": "none", "filesystem": "checkout_read_write", "status": "proposed"}
        preview["hash"] = commands.command_hash(preview)
        async with self._db_lock, self._sessions() as db, db.begin():
            run = await db.get(CodingRun, run_id)
            session = await db.get(CodingSession, run.session_id)
            checkout = await self._checkout(db, run.session_id)
            await self._require_user(db, slot.owner_id)
            await self._workspace(db, slot.owner_id, session.workspace_id)
            if (not session.command_mode or commands.TOOL not in await self._command_tools(db, slot.owner_id, session.model_profile_id)
                    or checkout is None or checkout.status != "ready" or checkout.canonical_path != str(root)
                    or slot.stop_requested or run.status == "stopping"):
                raise CodingRuntimeError("runtime_denied", "Container command authorization changed")
            prior = (await db.execute(select(CodingApproval).where(CodingApproval.run_id == run_id))).scalars()
            if sum("command" in json.loads(row.payload_json) for row in prior) >= 16:
                raise CodingRuntimeError("command_limit", "This run reached its 16-command limit")
            summary = {"title": commands.TITLE, "message": "Offline Docker command; the entire checkout is writable. Review the exact command before approving.",
                       "operation_id": proposal["operation_id"], "checkout_id": checkout.id, "command": preview}
            approval = CodingApproval(run_id=run_id, runtime_request_id=request_id, payload_json=json.dumps(summary),
                                      expires_at=None)
            db.add(approval)
            await db.flush()
            run.status = session.status = "waiting_approval"
            self._add_event(db, run, "approval.requested", {"approval_id": approval.id, "expires_at": None,
                            **{key: summary[key] for key in ("title", "message", "command")}})
        if slot.deadline is not None:
            slot.deadline.pause(approval.id)
        slot.changed.set()

    async def _resolve_command(self, *, user_id, approval_id, approved, command_hash):
        async with self._db_lock, self._sessions() as db:
            approval = await db.get(CodingApproval, approval_id)
            if approval is None:
                return False
            run = await self._run(db, user_id, approval.run_id)
            summary = json.loads(approval.payload_json)
            if "command" not in summary:
                return False
            session_id = run.session_id
        root = await self._execution_root(user_id=user_id, session_id=session_id) if approved else None
        async with self._lock:
            async with self._db_lock, self._sessions() as db, db.begin():
                approval = await db.get(CodingApproval, approval_id)
                summary = json.loads(approval.payload_json)
                command = summary["command"]
                run = await self._run(db, user_id, approval.run_id)
                session = await self._session(db, user_id, run.session_id)
                await self._require_user(db, user_id)
                await self._workspace(db, user_id, session.workspace_id)
                await self._model(db, user_id, session.model_profile_id)
                expiry = approval.expires_at
                if expiry is not None and expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                slot = self._slots.get(session.id)
                if approval.status != "pending" or (expiry is not None and expiry <= now()):
                    raise error("approval_expired", "Container command approval expired or was already answered")
                if slot is None or slot.run_id != run.id or slot.stop_requested or self._closing or run.status not in {"running", "waiting_approval"}:
                    raise error("run_inactive", "Coding run is no longer active")
                if approved:
                    checkout = await self._checkout(db, session.id)
                    if command_hash != command["hash"] or command_hash != commands.command_hash(command):
                        raise error("command_review_required", "Review this exact command in an updated client before approving")
                    if (not session.command_mode or not slot.request.command_tools or self._commands is None or self._command_cleanup_failed
                            or commands.TOOL not in await self._command_tools(db, user_id, session.model_profile_id)
                            or checkout is None or checkout.id != summary["checkout_id"] or checkout.status != "ready"
                            or checkout.canonical_path != str(root) or slot.request.workspace != root):
                        raise error("runtime_denied", "Container command authorization changed")
                    try:
                        commands.working_directory(root, command["working_directory"])
                    except (CodingRuntimeError, OSError) as exc:
                        raise error("command_invalid", "Command directory is no longer available") from exc
                approval.status = "approved" if approved else "denied"
                approval.resolved_at, approval.resolved_by_user_id = now(), user_id
                command["status"] = "queued" if approved else "denied"
                approval.payload_json = json.dumps(summary)
                self._add_event(db, run, "approval.resolved", {"approval_id": approval_id, "status": approval.status, "approved": approved})
                request_id, operation_id = approval.runtime_request_id, summary["operation_id"]
            if approved:
                task = asyncio.create_task(self._execute_command(approval_id, run.id, slot, root, command, request_id, operation_id),
                                           name=f"coding-command-{command['id']}")
                self._command_jobs[approval_id] = (run.id, task)
                task.add_done_callback(lambda done: self._command_jobs.pop(approval_id, None))
            else:
                await asyncio.wait_for(slot.runtime.command_result(request_id, {"protocol": commands.PROTOCOL, "operation_id": operation_id, "status": "denied"}), self.limits.stop_seconds)
            if slot.deadline is not None:
                slot.deadline.resolve(approval_id)
            slot.changed.set()
        return True

    async def _execute_command(self, approval_id, run_id, slot, root, command, request_id, operation_id):
        result = {"status": "failed", "exit_code": None}
        async def output(value):
            text = value.get("stdout", "") + ("\nSTDERR:\n" + value["stderr"] if value.get("stderr") else "")
            if value.get("output_truncated"):
                text += "\n[Saved command output capped at 64 KiB per stream.]"
            await self._record(run_id, "operation.output", {"operation_id": operation_id, "tool": commands.TOOL, "text": text, "mode": "snapshot"})
        try:
            if await self._execution_root(user_id=slot.owner_id, session_id=await self._run_session(run_id)) != root:
                raise error("runtime_denied", "Command checkout changed before execution")
            if slot.stop_requested or self._closing:
                raise asyncio.CancelledError
            async with self._db_lock, self._sessions() as db, db.begin():
                approval = await db.get(CodingApproval, approval_id)
                summary = json.loads(approval.payload_json)
                run = await db.get(CodingRun, run_id)
                session = await db.get(CodingSession, run.session_id)
                if (not session.command_mode or not await self._command_tools(db, slot.owner_id, session.model_profile_id)
                        or slot.stop_requested or self._closing or self._command_cleanup_failed):
                    raise error("runtime_denied", "Command policy changed before execution")
                summary["command"]["status"] = "running"
                approval.payload_json = json.dumps(summary)
                pending = (await db.execute(select(CodingApproval.id).where(CodingApproval.run_id == run_id, CodingApproval.status == "pending").limit(1))).first()
                run.status = session.status = "waiting_approval" if pending else "running"
                self._add_event(db, run, "command.started", {"operation_id": operation_id, "command_id": command["id"], "run_status": run.status})
            slot.changed.set()
            result = await self._commands.execute(root, command, output)
            await output(result)
        except asyncio.CancelledError:
            result = {"status": "cancelled", "exit_code": None}
        except Exception as exc:
            if getattr(exc, "code", "") == "container_cleanup_failed":
                self._command_cleanup_failed = True
            result = {"status": "uncertain" if self._command_cleanup_failed else "failed", "exit_code": None,
                      "error_code": getattr(exc, "code", "container_failed")}
        try:
            async with self._db_lock, self._sessions() as db, db.begin():
                approval = await db.get(CodingApproval, approval_id)
                summary = json.loads(approval.payload_json)
                summary["command"].update(status=result["status"], result=result)
                approval.payload_json = json.dumps(summary, ensure_ascii=False)
                self._add_event(db, await db.get(CodingRun, run_id), "command.recorded", {"command_id": command["id"], "status": result["status"]})
            if not slot.stop_requested and not self._closing:
                visible = {**result}
                for key in ("stdout", "stderr"):
                    visible[key] = result.get(key, "").encode("utf-8")[:16384].decode("utf-8", errors="ignore")
                visible["output_truncated"] = result.get("output_truncated", False) or any(len(result.get(key, "").encode()) > 16384 for key in ("stdout", "stderr"))
                await asyncio.wait_for(slot.runtime.command_result(request_id, {"protocol": commands.PROTOCOL, "operation_id": operation_id, **visible}), self.limits.stop_seconds)
        except Exception:
            if slot.task is not None:
                slot.task.cancel()
        slot.changed.set()

    async def _cancel_commands(self, run_id):
        tasks = [task for identity, task in self._command_jobs.values() if identity == run_id]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _recover_command_records(self, db, run_id, reason="backend_restart"):
        rows = (await db.execute(select(CodingApproval).where(CodingApproval.run_id == run_id))).scalars()
        for row in rows:
            summary = json.loads(row.payload_json)
            command = summary.get("command")
            if command and command["status"] in {"queued", "running"}:
                command["status"] = "cancelled" if command["status"] == "queued" else "uncertain"
                command["result"] = {"status": command["status"], "error_code": reason}
                row.payload_json = json.dumps(summary)
                self._add_event(db, await db.get(CodingRun, run_id), "command.recorded", {"command_id": command["id"], "status": command["status"]})

    async def list_commands(self, *, user_id, run_id):
        async with self._db_lock, self._sessions() as db:
            await self._require_user(db, user_id)
            await self._run(db, user_id, run_id)
            rows = (await db.execute(select(CodingApproval).where(CodingApproval.run_id == run_id).order_by(CodingApproval.created_at, CodingApproval.id))).scalars()
            return [{"approval_id": row.id, **json.loads(row.payload_json)["command"]} for row in rows if "command" in json.loads(row.payload_json)]
