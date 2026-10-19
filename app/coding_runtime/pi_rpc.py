from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.coding_runtime.contracts import CodingRuntimeEvent
from app.coding_runtime.changes import PROPOSAL_BYTES
from app.coding_runtime.container_commands import PROPOSAL_BYTES as COMMAND_PROPOSAL_BYTES
from app.coding_runtime.pi_profile import PiModelProfile


class PiRpcError(RuntimeError):
    """Base error for the private Pi RPC adapter."""


class PiRpcCommandError(PiRpcError):
    pass


class PiRpcProtocolError(PiRpcError):
    pass


class PiRpcProcessError(PiRpcError):
    pass


MINIMUM_PI_VERSION = (1, 3, 0)
MAXIMUM_PI_VERSION = (1, 3, 1)
GUARD_PROTOCOL = "fruitcake.workspace_guard.v1"
GUARD_PATH = Path(__file__).with_name("workspace_guard.mjs")


@dataclass(frozen=True, slots=True)
class PiPromptResult:
    disposition: Literal["started", "queued", "handled"]
    settled: bool
    aborted: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PiRpcConfig:
    workspace: Path
    command: tuple[str, ...] = ("pi",)
    rpc_arguments: tuple[str, ...] = ("--mode", "rpc", "--no-session")
    provider: str | None = None
    model: str | None = None
    model_profile: PiModelProfile | None = None
    tool_allowlist: tuple[str, ...] = ("read", "grep", "find", "ls")
    edit_tools: tuple[str, ...] = ()
    command_tools: tuple[str, ...] = ()
    isolate_resources: bool = True
    workspace_guard: bool = True
    require_read_approval: bool = False
    review_mode: bool = False
    approval_timeout_seconds: float | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    inherit_environment: bool = False
    command_timeout_seconds: float = 10.0
    shutdown_timeout_seconds: float = 2.0
    stderr_limit_bytes: int = 32 * 1024
    record_limit_bytes: int = 4 * 1024 * 1024
    event_limit_records: int = 256

    def __post_init__(self) -> None:
        if self.review_mode and (not self.workspace_guard or not self.isolate_resources or self.inherit_environment
                                 or self.tool_allowlist != ("fruitcake_review_read",) or self.edit_tools or self.command_tools):
            raise ValueError("Reviews require isolated capture-only read tools")
        if self.command_tools not in {(), ("fruitcake_run",)} or (self.command_tools and not self.workspace_guard):
            raise ValueError("Container commands require the Fruitcake guard")
        if any(tool not in {"fruitcake_edit", "fruitcake_write"} for tool in self.edit_tools) or (self.edit_tools and not self.workspace_guard):
            raise ValueError("Approved edit tools require the Fruitcake guard")
        if min(self.command_timeout_seconds, self.shutdown_timeout_seconds) <= 0 or (
            self.approval_timeout_seconds is not None and self.approval_timeout_seconds <= 0
        ):
            raise ValueError("Runtime deadlines must be positive")
        if self.record_limit_bytes <= 0 or self.event_limit_records < 2:
            raise ValueError("Runtime record and event limits are invalid")
        if self.model_profile and (self.provider or self.model):
            raise ValueError("Use either a model profile or provider/model arguments")

    def argv(self) -> tuple[str, ...]:
        argv = [*self.command, *self.rpc_arguments]
        provider = self.model_profile.provider if self.model_profile else self.provider
        model = self.model_profile.model if self.model_profile else self.model
        if provider:
            argv.extend(("--provider", provider))
        if model:
            argv.extend(("--model", model))
        if self.tool_allowlist or self.edit_tools or self.command_tools:
            argv.extend(("--tools", ",".join((*self.tool_allowlist, *self.edit_tools, *self.command_tools))))
        else:
            argv.append("--no-tools")
        if self.isolate_resources:
            argv.extend(
                (
                    "--no-mcp",
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "--no-context-files",
                    "--no-approve",
                )
            )
        if self.workspace_guard:
            argv.extend(("--extension", str(GUARD_PATH)))
        return tuple(argv)

    def resolved_workspace(self) -> Path:
        workspace = self.workspace.expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError(f"Coding workspace is not a directory: {workspace}")
        return workspace

    def process_environment(self, *, runtime_config_dir: Path | None = None) -> dict[str, str]:
        if self.inherit_environment:
            env = dict(os.environ)
        else:
            env = {
                key: os.environ[key]
                for key in ("HOME", "LANG", "LC_ALL", "PATH", "SSL_CERT_FILE", "TMPDIR")
                if key in os.environ
            }
        env.update(self.environment)
        if runtime_config_dir is not None:
            env["PI_CODING_AGENT_DIR"] = str(runtime_config_dir)
        env["PI_OFFLINE"] = "1"
        env["PI_TELEMETRY"] = "0"
        if self.model_profile:
            env["FRUITCAKE_PI_MODEL_KEY"] = self.model_profile.api_key
        return env


_EVENT_SENTINEL = object()


class PiRpcClient:
    """Async subprocess client for Pi's private LF-delimited JSON RPC protocol."""

    def __init__(self, config: PiRpcConfig):
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending: dict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._events: asyncio.Queue[CodingRuntimeEvent | object] = asyncio.Queue(
            maxsize=config.event_limit_records
        )
        self._write_lock = asyncio.Lock()
        self._settled_condition = asyncio.Condition()
        self._settled_sequence = 0
        self._last_settled_aborted = False
        self._request_sequence = 0
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_size = 0
        self._exit_error: PiRpcProcessError | None = None
        self._closed = False
        self._version: tuple[int, int, int] | None = None
        self._guard_ready = asyncio.Event()
        self._guard_token = secrets.token_urlsafe(24)
        self._reader_tasks: list[asyncio.Task[Any]] = []
        self._run_error: str | None = None
        self._overflowed = False
        self._close_task: asyncio.Task[None] | None = None
        self._interactions: dict[str, str] = {}
        self._events_finished = False
        self._runtime_config = tempfile.TemporaryDirectory(prefix="fruitcake-pi-")
        self._runtime_config_dir = Path(self._runtime_config.name)
        if config.model_profile:
            (self._runtime_config_dir / "models.json").write_text(
                json.dumps(config.model_profile.models_document()), encoding="utf-8"
            )

    def _environment(self) -> dict[str, str]:
        env = self.config.process_environment(runtime_config_dir=self._runtime_config_dir)
        env["FRUITCAKE_PI_WORKSPACE"] = str(self.config.resolved_workspace())
        env["FRUITCAKE_PI_GUARD_TOKEN"] = self._guard_token
        env["FRUITCAKE_PI_CONFIRM_READS"] = "1" if self.config.require_read_approval else "0"
        env["FRUITCAKE_PI_APPROVAL_TIMEOUT_MS"] = str(int(self.config.approval_timeout_seconds * 1000)) if self.config.approval_timeout_seconds is not None else "0"
        env["FRUITCAKE_PI_READ_TOOLS"] = json.dumps(self.config.tool_allowlist)
        env["FRUITCAKE_PI_EDIT_TOOLS"] = json.dumps(self.config.edit_tools)
        env["FRUITCAKE_PI_COMMAND_TOOLS"] = json.dumps(self.config.command_tools)
        env["FRUITCAKE_PI_REVIEW_MODE"] = "1" if self.config.review_mode else "0"
        return env

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def stderr(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", errors="replace")

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def settled_sequence(self) -> int:
        return self._settled_sequence

    @property
    def version(self) -> tuple[int, int, int] | None:
        return self._version

    async def probe_version(self) -> tuple[int, int, int]:
        workspace = self.config.resolved_workspace()
        try:
            process = await asyncio.create_subprocess_exec(
                *self.config.command,
                "--version",
                cwd=workspace,
                env=self._environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise PiRpcProcessError(f"Unable to probe Pi version: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.command_timeout_seconds,
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise PiRpcProcessError("Timed out probing Pi version") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise PiRpcProcessError(f"Pi version probe failed with code {process.returncode}: {detail}")
        version = _parse_version(stdout.decode("utf-8", errors="replace"))
        if not (MINIMUM_PI_VERSION <= version < MAXIMUM_PI_VERSION):
            rendered = ".".join(str(part) for part in version)
            raise PiRpcProcessError(
                f"Unsupported Pi version {rendered}; this spike is verified against 1.3.0"
            )
        self._version = version
        return version

    async def start(self) -> None:
        if self._process is not None or self._closed:
            raise PiRpcError("Pi RPC client has already been started")
        try:
            await self.probe_version()
        except BaseException:
            self._runtime_config.cleanup()
            raise
        workspace = self.config.resolved_workspace()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.config.argv(),
                cwd=workspace,
                env=self._environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            self._runtime_config.cleanup()
            raise PiRpcProcessError(f"Unable to start Pi RPC process: {exc}") from exc

        self._reader_tasks = [
            self._spawn_task(self._read_stdout(), "pi-rpc-stdout"),
            self._spawn_task(self._read_stderr(), "pi-rpc-stderr"),
        ]
        self._spawn_task(self._watch_process(), "pi-rpc-process")
        try:
            state = await self.get_state()
            if self.config.workspace_guard:
                await asyncio.wait_for(self._guard_ready.wait(), self.config.command_timeout_seconds)
                if self._exit_error:
                    raise self._exit_error
            if self.config.model_profile:
                selected = state.get("model") or {}
                profile = self.config.model_profile
                if selected.get("provider") != profile.provider or selected.get("id") != profile.model:
                    raise PiRpcProtocolError("Pi did not select the authorized coding model")
        except BaseException:
            await self.close()
            raise

    async def get_state(self) -> dict[str, Any]:
        response = await self.send_command("get_state")
        return _response_data(response)

    async def prompt(self, message: str) -> str:
        if self.config.workspace_guard and not self._guard_ready.is_set():
            raise PiRpcError("Workspace guard is not ready")
        self._run_error = None
        response = await self.send_command("prompt", message=message)
        disposition = _response_data(response).get("disposition")
        if disposition not in {"started", "queued", "handled"}:
            raise PiRpcProtocolError(f"Unexpected prompt disposition: {disposition!r}")
        return disposition

    async def prompt_and_wait(self, message: str, *, timeout: float | None = None) -> PiPromptResult:
        settled_after = self._settled_sequence
        try:
            disposition = await self.prompt(message)
            if disposition == "handled":
                return PiPromptResult(disposition=disposition, settled=False)
            aborted = await self.wait_until_settled(after=settled_after, timeout=timeout)
        except (asyncio.CancelledError, PiRpcError):
            await self.close()
            raise
        return PiPromptResult(disposition=disposition, settled=True, aborted=aborted, error=self._run_error)

    async def steer(self, message: str) -> str:
        response = await self.send_command("steer", message=message)
        return str(_response_data(response).get("disposition", ""))

    async def follow_up(self, message: str) -> str:
        response = await self.send_command("follow_up", message=message)
        return str(_response_data(response).get("disposition", ""))

    async def abort(self) -> None:
        await self.send_command("clear_queue")
        await self.send_command("abort")

    async def respond_to_interaction(self, request_id: str, *, confirmed: bool = False, value: str | None = None) -> None:
        method = "input" if value is not None else "confirm"
        if self._interactions.get(request_id) != method:
            raise PiRpcCommandError("Confirmation request is unknown or has already been answered")
        process = self._require_running_process()
        assert process.stdin is not None
        payload = {"type": "extension_ui_response", "id": request_id, **({"value": value} if method == "input" else {"confirmed": confirmed})}
        async with asyncio.timeout(self.config.command_timeout_seconds):
            async with self._write_lock:
                if self._interactions.pop(request_id, None) != method:
                    raise PiRpcCommandError("Confirmation request has already been answered")
                process.stdin.write(json.dumps(payload).encode() + b"\n")
                await process.stdin.drain()

    async def wait_until_settled(self, *, after: int | None = None, timeout: float | None = None) -> bool:
        target = self._settled_sequence if after is None else after

        async def wait() -> bool:
            async with self._settled_condition:
                await self._settled_condition.wait_for(
                    lambda: self._settled_sequence > target or self._exit_error is not None or self._closed
                )
                if self._closed and self._settled_sequence <= target:
                    raise PiRpcProcessError("Pi RPC process was closed before settlement")
                if self._exit_error is not None:
                    raise self._exit_error
                return self._last_settled_aborted

        deadline = timeout if timeout is not None else self.config.command_timeout_seconds
        try:
            return await asyncio.wait_for(wait(), timeout=deadline)
        except TimeoutError as exc:
            raise PiRpcError("Timed out waiting for Pi to become settled") from exc

    async def send_command(self, command: str, **fields: Any) -> dict[str, Any]:
        if self.config.review_mode and command not in {"get_state", "prompt", "abort", "clear_queue", "get_available_thinking_levels", "set_thinking_level"}:
            raise PiRpcCommandError("Command is unavailable in the capture-only reviewer")
        if self.config.workspace_guard and command in {"bash", "export_html", "switch_session", "new_session", "fork", "clone"}:
            raise PiRpcCommandError(f"Command {command!r} is unavailable in the read-only spike")
        process = self._require_running_process()
        assert process.stdin is not None
        self._request_sequence += 1
        request_id = f"fruitcake-{self._request_sequence}"
        payload = {**fields, "id": request_id, "type": command}
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = (command, response_future)
        try:
            async with asyncio.timeout(self.config.command_timeout_seconds):
                async with self._write_lock:
                    process.stdin.write(line)
                    await process.stdin.drain()
                return await asyncio.shield(response_future)
        except TimeoutError as exc:
            raise PiRpcError(f"Timed out waiting for Pi command {command!r}") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise PiRpcProcessError("Pi RPC input pipe closed") from exc
        finally:
            self._pending.pop(request_id, None)
            if not response_future.done():
                response_future.cancel()
            elif not response_future.cancelled():
                response_future.exception()

    async def events(self) -> AsyncIterator[CodingRuntimeEvent]:
        while True:
            if (self._closed or self._events_finished) and self._events.empty():
                return
            event = await self._events.get()
            if event is _EVENT_SENTINEL:
                break
            assert isinstance(event, CodingRuntimeEvent)
            yield event

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._shutdown(), name="pi-rpc-shutdown")
        await asyncio.shield(self._close_task)

    async def _shutdown(self) -> None:
        self._closed = True
        error = PiRpcProcessError("Pi RPC process was closed")
        for _, future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        async with self._settled_condition:
            self._settled_condition.notify_all()
        self._interactions.clear()
        process = self._process
        if process is None:
            self._runtime_config.cleanup()
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=self.config.shutdown_timeout_seconds)
            except TimeoutError:
                self._signal_process_group(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.config.shutdown_timeout_seconds)
                except TimeoutError:
                    self._signal_process_group(signal.SIGKILL)
                    await asyncio.wait_for(process.wait(), self.config.shutdown_timeout_seconds)
        # A child may have kept a pipe open after the Pi parent exited.
        # macOS can report EPERM for a group already being reaped after TERM.
        # The required TERM/KILL escalation above still reports real failures.
        with suppress(PermissionError):
            self._signal_process_group(signal.SIGKILL)
        await self._finish_tasks()
        self._process = None
        self._runtime_config.cleanup()

    async def __aenter__(self) -> PiRpcClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _require_running_process(self) -> asyncio.subprocess.Process:
        if self._exit_error is not None:
            raise self._exit_error
        if self._closed or not self.running or self._process is None:
            raise PiRpcProcessError("Pi RPC process is not running")
        return self._process

    def _signal_process_group(self, sig: signal.Signals) -> None:
        if self._process is not None:
            with suppress(ProcessLookupError):
                os.killpg(self._process.pid, sig)

    def _spawn_task(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        buffer = bytearray()
        discarding = False
        while chunk := await process.stdout.read(64 * 1024):
            if discarding:
                separator = chunk.find(b"\n")
                if separator < 0:
                    continue
                chunk = chunk[separator + 1:]
                discarding = False
            buffer.extend(chunk)
            if len(buffer) > self.config.record_limit_bytes and b"\n" not in buffer:
                await self._emit_protocol_error("Pi emitted an oversized unterminated JSONL record")
                buffer.clear()
                discarding = True
                continue
            while True:
                separator = buffer.find(b"\n")
                if separator < 0:
                    break
                raw_line = bytes(buffer[:separator])
                del buffer[: separator + 1]
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]
                if raw_line:
                    try:
                        await self._handle_record(raw_line)
                    except (AttributeError, KeyError, TypeError, ValueError) as exc:
                        await self._emit_protocol_error(f"Invalid Pi record shape: {type(exc).__name__}")
        if buffer:
            await self._emit_protocol_error("Pi stdout ended with a partial JSONL record")

    async def _handle_record(self, raw_line: bytes) -> None:
        if len(raw_line) > self.config.record_limit_bytes:
            await self._emit_protocol_error("Pi emitted an oversized JSONL record")
            return
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            await self._emit_protocol_error(f"Invalid Pi JSONL record: {exc}")
            return
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            await self._emit_protocol_error("Pi JSONL record is not a typed object")
            return
        if record["type"] == "response" and isinstance(record.get("id"), str):
            pending = self._pending.get(record["id"])
            if pending is None:
                await self._publish(_normalize_event(record))
                return
            expected_command, future = pending
            if future.done():
                return
            if record.get("command") != expected_command:
                future.set_exception(
                    PiRpcProtocolError(
                        f"Response command {record.get('command')!r} did not match {expected_command!r}"
                    )
                )
            elif record.get("success") is not True:
                future.set_exception(PiRpcCommandError(str(record.get("error") or "Pi command failed")))
            else:
                future.set_result(record)
            return
        if record["type"] == "extension_ui_request" and record.get("method") == "notify":
            try:
                acknowledgement = json.loads(record.get("message", ""))
            except (ValueError, TypeError):
                acknowledgement = None
            if isinstance(acknowledgement, dict) and acknowledgement.get("protocol") == GUARD_PROTOCOL:
                if (
                    acknowledgement.get("token") == self._guard_token
                    and acknowledgement.get("workspace") == str(self.config.resolved_workspace())
                ):
                    self._guard_ready.set()
                return
        if record["type"] == "message_end":
            message = record.get("message") or {}
            if message.get("role") == "assistant":
                self._run_error = (
                    str(message.get("errorMessage") or "Coding model failed")[:2048]
                    if message.get("stopReason") == "error" else None
                )
        if record["type"] == "extension_ui_request" and record.get("method") == "confirm":
            self._interactions[str(record["id"])] = "confirm"
        if record["type"] == "extension_ui_request" and record.get("method") == "input" and (
            (record.get("title") == "Approve container command?" and self.config.command_tools)
            or (record.get("title") == "Read captured review file?" and self.config.review_mode)
        ):
            self._interactions[str(record["id"])] = "input"
        if record["type"] == "agent_settled":
            if not isinstance(record.get("aborted"), bool):
                await self._emit_protocol_error("Pi settlement record is missing its aborted flag")
                return
            async with self._settled_condition:
                self._settled_sequence += 1
                self._last_settled_aborted = bool(record.get("aborted", False))
                self._settled_condition.notify_all()
            self._interactions.clear()
        event = _normalize_event(record)
        if record["type"] == "agent_settled" and self._run_error:
            event = CodingRuntimeEvent("run.failed", {"message": self._run_error}, "agent_settled")
        await self._publish(event)

    async def _publish(self, event: CodingRuntimeEvent | object) -> None:
        if isinstance(event, CodingRuntimeEvent) and event.kind == "assistant.reasoning_status" and event.payload.get("status") == "thinking_delta":
            return
        if self._overflowed and event is not _EVENT_SENTINEL:
            return
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            if not self._overflowed:
                self._overflowed = True
                self._exit_error = PiRpcProcessError("Coding runtime event buffer exceeded its limit")
                self._signal_process_group(signal.SIGKILL)
            while not self._events.empty():
                self._events.get_nowait()
            self._events.put_nowait(CodingRuntimeEvent("session.failed", {"reason": "event_buffer_overflow"}))
            if event is _EVENT_SENTINEL:
                self._events.put_nowait(event)

    async def _emit_protocol_error(self, message: str) -> None:
        await self._publish(
            CodingRuntimeEvent(
                kind="runtime.protocol_error",
                payload={"message": message},
                provider_event="protocol_error",
            )
        )

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while chunk := await process.stderr.read(16 * 1024):
            self._append_stderr(chunk)

    def _append_stderr(self, chunk: bytes) -> None:
        limit = max(0, self.config.stderr_limit_bytes)
        if limit == 0:
            return
        self._stderr_chunks.append(chunk[-limit:])
        self._stderr_size += min(len(chunk), limit)
        while self._stderr_size > limit and self._stderr_chunks:
            overflow = self._stderr_size - limit
            first = self._stderr_chunks.popleft()
            if len(first) > overflow:
                first = first[overflow:]
                self._stderr_chunks.appendleft(first)
                self._stderr_size = limit
            else:
                self._stderr_size -= len(first)

    async def _watch_process(self) -> None:
        process = self._process
        assert process is not None
        return_code = await process.wait()
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*self._reader_tasks, return_exceptions=True),
                self.config.shutdown_timeout_seconds,
            )
        if not self._closed:
            self._exit_error = self._exit_error or PiRpcProcessError(
                f"Pi RPC process exited unexpectedly with code {return_code}; stderr: {self.stderr}"
            )
            for _, future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(self._exit_error)
            async with self._settled_condition:
                self._settled_condition.notify_all()
            await self._publish(
                CodingRuntimeEvent(
                    kind="session.failed",
                    payload={"exit_code": return_code, "stderr": self.stderr},
                    provider_event="process_exit",
                )
            )
        self._guard_ready.set()
        await self._publish(_EVENT_SENTINEL)
        self._events_finished = True

    async def _finish_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        if tasks:
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    self.config.shutdown_timeout_seconds,
                )


def _response_data(response: Mapping[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise PiRpcProtocolError("Successful Pi response did not contain an object data field")
    return data


def _normalize_event(record: Mapping[str, Any]) -> CodingRuntimeEvent:
    provider_event = str(record.get("type"))
    payload: dict[str, Any] = {}
    if provider_event == "agent_start":
        kind = "run.started"
    elif provider_event == "turn_start":
        kind = "turn.started"
    elif provider_event == "turn_end":
        kind = "turn.completed"
    elif provider_event == "agent_settled":
        kind = "run.completed"
        payload = {"aborted": bool(record.get("aborted"))}
    elif provider_event == "tool_execution_start":
        kind = "operation.started"
    elif provider_event == "tool_execution_update":
        kind = "operation.output"
    elif provider_event == "tool_execution_end":
        kind = "operation.completed"
    elif provider_event == "extension_ui_request":
        kind = "interaction.requested"
        change_request = record.get("method") == "confirm" and record.get("title") == "Approve file change?"
        command_request = record.get("method") == "input" and record.get("title") == "Approve container command?"
        payload = {
            key: _bounded(record[key], limit=PROPOSAL_BYTES if key == "message" and change_request else COMMAND_PROPOSAL_BYTES if key == "placeholder" and command_request else 32 * 1024)
            for key in ("id", "method", "title", "message", "options", "timeout", "placeholder")
            if key in record
        }
    elif provider_event == "auto_retry_start":
        kind = "run.retrying"
        payload = {"attempt": record.get("attempt"), "delay_ms": record.get("delayMs")}
    elif provider_event == "message_end":
        message = record.get("message") or {}
        kind = "assistant.message" if message.get("role") == "assistant" else "runtime.event"
        content = message.get("content") or []
        payload = {"text": _content_text(content, limit=None), "stop_reason": message.get("stopReason")}
        if kind == "runtime.event":
            payload = {}
    elif provider_event == "message_update":
        update = record.get("assistantMessageEvent")
        update_type = update.get("type") if isinstance(update, Mapping) else None
        if update_type == "text_delta":
            kind = "assistant.text_delta"
            delta = str(update.get("delta", ""))
            payload = {"delta": delta, "content_index": update.get("contentIndex")}
        elif update_type and str(update_type).startswith("thinking_"):
            kind = "assistant.reasoning_status"
            payload = {"status": update_type}
        else:
            kind = "runtime.event"
    else:
        kind = "runtime.event"
    if provider_event.startswith("tool_execution_"):
        payload = {
            "operation_id": record.get("toolCallId"),
            "tool": record.get("toolName"),
        }
        if provider_event == "tool_execution_start":
            payload["arguments"] = _bounded(record.get("args", {}))
        elif provider_event == "tool_execution_update":
            partial = record.get("partialResult") or {}
            payload.update({**_tool_preview(partial.get("content", [])), "mode": "snapshot"})
        else:
            result = record.get("result") or {}
            payload.update({
                **_tool_preview(result.get("content", [])),
                "is_error": bool(record.get("isError")),
                "duration_ms": record.get("durationMs"),
            })
    return CodingRuntimeEvent(kind=kind, payload=payload, provider_event=provider_event)


def _bounded(value: Any, limit: int = 32 * 1024) -> Any:
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= limit:
        return value
    return {"truncated": True, "preview": encoded[:limit].decode("utf-8", errors="ignore")}


def _tool_preview(content: Any) -> dict[str, Any]:
    from app.coding_runtime.output import TOOL_PREVIEW_SUFFIX

    text = _content_text(content, limit=None)
    encoded = text.encode("utf-8")
    if len(encoded) <= 32 * 1024:
        return {"text": text}
    prefix = encoded[:32 * 1024 - len(TOOL_PREVIEW_SUFFIX.encode())].decode("utf-8", errors="ignore")
    return {"text": prefix + TOOL_PREVIEW_SUFFIX, "preview_truncated": True, "original_text_bytes": len(encoded)}


def _content_text(content: Any, *, limit: int | None = 32 * 1024) -> str:
    if not isinstance(content, list):
        return ""
    text = "\n".join(
        str(block.get("text", "")) for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )
    return text if limit is None else text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _parse_version(output: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", output.strip())
    if not match:
        raise PiRpcProtocolError(f"Unable to parse Pi version from {output.strip()!r}")
    return tuple(int(part) for part in match.groups())
