from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


DEFAULT_BLOCKED_PATTERNS: list[tuple[str, str]] = [
    (r"(^|[\s;&|])(?:sudo|su|doas)\b", "privilege escalation is not allowed"),
    (r"\b(?:useradd|usermod|userdel|passwd|chsh|chown)\b", "user-management commands are not allowed"),
    (r"\b(?:curl|wget|nc|ncat|netcat|ssh|scp|sftp|ftp|telnet|ping|traceroute|dig|nslookup|host|socat)\b", "network commands are not allowed"),
    (r"\b(?:docker|podman|kubectl|nerdctl|ctr)\b", "container/runtime commands are not allowed"),
    (r"\b(?:systemctl|service|launchctl|initctl)\b", "service-management commands are not allowed"),
    (r"\b(?:kill|pkill|killall|nohup|disown|setsid)\b", "process-management commands are not allowed"),
    (r"(^|[\s;&|])rm\b", "destructive file-removal commands are not allowed"),
    (r"\b(?:mkfs|mount|umount|fdisk|parted|dd)\b", "low-level disk commands are not allowed"),
    (r"\bgit\s+(?:clone|fetch|pull|push)\b", "networked git operations are not allowed"),
    (r"\bgh\s+(?:auth|repo\s+clone)\b", "networked GitHub CLI operations are not allowed"),
    (r"(?<!&)&(?!&)", "background execution is not allowed"),
]

TOOL_SCHEMA = {
    "name": "shell_exec",
    "title": "Run Local Command",
    "description": (
        "Run one non-interactive command in the user's isolated local workspace. "
        "Use this for local tests, builds, formatters, status checks, and other bounded CLI work when a shell is needed. "
        "Do not use it for web access, downloads, services, background processes, privilege changes, or file deletion. "
        "The tool enforces those restrictions and returns a stable status, exit code, stdout, and stderr."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": (
                    "One non-interactive local shell command. Use workspace-relative paths and concise commands. "
                    "Network, destructive, privileged, service, container, and background commands are refused."
                ),
            },
            "working_directory": {
                "type": "string",
                "default": ".",
                "maxLength": 512,
                "description": (
                    "Optional existing directory relative to the user's workspace, for example 'src'. "
                    "Use '.' for the workspace root. Absolute paths and '..' escapes are refused."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "description": "Optional timeout for this command. The server's configured timeout remains the upper bound.",
            },
        },
        "required": ["command"],
    },
}


@dataclass
class ShellServerConfig:
    allowed_paths: list[Path]
    timeout_seconds: int = 30
    output_limit_bytes: int = 8192
    blocked_patterns: list[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        self.allowed_paths = [path.resolve() for path in self.allowed_paths]
        if self.blocked_patterns is None:
            self.blocked_patterns = list(DEFAULT_BLOCKED_PATTERNS)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fruitcake standalone shell MCP server")
    parser.add_argument(
        "--allowed-paths",
        nargs="+",
        required=True,
        help="Allowed workspace root(s). Fruitcake typically passes /workspace.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--output-limit-bytes", type=int, default=8192)
    parser.add_argument(
        "--blocked-policy-file",
        default="",
        help="Optional newline-delimited regex patterns or JSON list of {pattern, reason}.",
    )
    return parser.parse_args(argv)


def load_blocked_patterns(path_value: str) -> list[tuple[str, str]]:
    patterns = list(DEFAULT_BLOCKED_PATTERNS)
    if not path_value:
        return patterns
    policy_path = Path(path_value)
    if not policy_path.exists():
        raise FileNotFoundError(f"Blocked policy file not found: {policy_path}")
    raw = policy_path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict) and entry.get("pattern"):
                patterns.append((str(entry["pattern"]), str(entry.get("reason") or "blocked by policy file")))
            elif isinstance(entry, str):
                patterns.append((entry, "blocked by policy file"))
        return patterns
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        patterns.append((text, "blocked by policy file"))
    return patterns


def build_config(args: argparse.Namespace) -> ShellServerConfig:
    return ShellServerConfig(
        allowed_paths=[Path(value) for value in args.allowed_paths],
        timeout_seconds=args.timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
        blocked_patterns=load_blocked_patterns(args.blocked_policy_file),
    )


def serialize_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
    }


def detect_blocked_command(command: str, patterns: Iterable[tuple[str, str]]) -> Optional[str]:
    lowered = command.strip().lower()
    for pattern, reason in patterns:
        if re.search(pattern, lowered):
            return reason
    return None


def resolve_execution_dir(config: ShellServerConfig, user_context: Dict[str, Any] | None) -> Path:
    if not config.allowed_paths:
        raise ValueError("At least one allowed path is required")
    base = config.allowed_paths[0]
    user_id = None
    if user_context:
        raw = user_context.get("user_id")
        if raw is not None:
            user_id = str(raw)
    target = (base / user_id).resolve() if user_id else base
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("Resolved execution directory escaped the allowed path") from exc
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_working_directory(workspace: Path, working_directory: str | None) -> Path:
    requested = str(working_directory or ".").strip() or "."
    relative = Path(requested)
    if relative.is_absolute():
        raise ValueError("working_directory must be relative to the workspace")
    target = (workspace / relative).resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("working_directory escaped the workspace") from exc
    if not target.is_dir():
        raise ValueError(f"working_directory does not exist: {requested}")
    return target


def _result(
    *,
    status: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int,
    cwd: Path | None = None,
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": status == "completed",
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "cwd": str(cwd) if cwd is not None else "",
    }
    payload.update(extra)
    return payload


async def _read_stream_limited(stream: asyncio.StreamReader, limit: int) -> tuple[str, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(1024)
        if not chunk:
            break
        if total < limit:
            remaining = limit - total
            kept = chunk[:remaining]
            if kept:
                chunks.append(kept)
                total += len(kept)
            if len(chunk) > remaining:
                truncated = True
        else:
            truncated = True
    return b"".join(chunks).decode("utf-8", errors="replace"), truncated


async def execute_command(
    command: str,
    *,
    config: ShellServerConfig,
    user_context: Dict[str, Any] | None = None,
    working_directory: str | None = None,
    timeout_seconds: int | None = None,
) -> Dict[str, Any]:
    command = (command or "").strip()
    if not command:
        return _result(status="invalid_arguments", stderr="No command provided.", exit_code=2)
    if len(command) > 4096:
        return _result(status="invalid_arguments", stderr="Command exceeds 4096 characters.", exit_code=2)

    workspace = resolve_execution_dir(config, user_context)
    try:
        cwd = resolve_working_directory(workspace, working_directory)
    except ValueError as exc:
        return _result(status="invalid_arguments", stderr=str(exc), exit_code=2, cwd=workspace)

    blocked_reason = detect_blocked_command(command, config.blocked_patterns or [])
    if blocked_reason:
        return _result(
            status="refused",
            stderr=f"Command refused: {blocked_reason}.",
            exit_code=126,
            cwd=cwd,
            refused=True,
        )

    try:
        requested_timeout = config.timeout_seconds if timeout_seconds is None else int(timeout_seconds)
    except (TypeError, ValueError):
        return _result(
            status="invalid_arguments",
            stderr="timeout_seconds must be an integer.",
            exit_code=2,
            cwd=cwd,
        )
    if requested_timeout < 1:
        return _result(
            status="invalid_arguments",
            stderr="timeout_seconds must be at least 1.",
            exit_code=2,
            cwd=cwd,
        )
    effective_timeout = max(1, min(requested_timeout, config.timeout_seconds))
    started_at = time.monotonic()
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        executable="/bin/sh",
        start_new_session=True,
        env={
            "HOME": str(cwd),
            "PWD": str(cwd),
            "WORKSPACE_ROOT": str(cwd),
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        },
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_read_stream_limited(process.stdout, config.output_limit_bytes))
    stderr_task = asyncio.create_task(_read_stream_limited(process.stderr, config.output_limit_bytes))

    timed_out = False
    try:
        exit_code = await asyncio.wait_for(process.wait(), timeout=effective_timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        exit_code = await process.wait()

    stdout, stdout_truncated = await stdout_task
    stderr, stderr_truncated = await stderr_task

    status = "timed_out" if timed_out else "completed" if exit_code == 0 else "failed"
    result = _result(
        status=status,
        stdout=stdout,
        stderr=stderr if not timed_out else (stderr + ("\n" if stderr else "") + "Command timed out.").strip(),
        exit_code=124 if timed_out else int(exit_code),
        cwd=cwd,
        duration_ms=round((time.monotonic() - started_at) * 1000),
    )
    if timed_out:
        result["timed_out"] = True
    if stdout_truncated:
        result["stdout_truncated"] = True
    if stderr_truncated:
        result["stderr_truncated"] = True
    return result


def read_message(stdin: Any) -> Optional[Dict[str, Any]]:
    line = stdin.readline()
    if not line:
        return None
    if isinstance(line, bytes):
        first = line.decode(errors="replace").strip()
    else:
        first = str(line).strip()
    if not first:
        return None
    if first.lower().startswith("content-length:"):
        length = int(first.split(":", 1)[1].strip())
        while True:
            header_line = stdin.readline()
            if not header_line:
                return None
            if header_line in (b"\n", b"\r\n", "\n", "\r\n"):
                break
        payload = stdin.read(length)
        if isinstance(payload, bytes):
            payload = payload.decode(errors="replace")
        return json.loads(payload)
    return json.loads(first)


def write_message(stdout: Any, message: Dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stdout.write(payload + b"\n")
    stdout.flush()


def handle_message(message: Dict[str, Any], config: ShellServerConfig) -> Optional[Dict[str, Any]]:
    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "fruitcake-shell", "version": "0.2.0"},
                "capabilities": {"tools": {}},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [TOOL_SCHEMA]}}
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = dict(params.get("arguments") or {})
        if tool_name != "shell_exec":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }
        user_context = arguments.pop("_fruitcake_user_context", None)
        result = asyncio.run(
            execute_command(
                arguments.get("command", ""),
                config=config,
                user_context=user_context,
                working_directory=arguments.get("working_directory"),
                timeout_seconds=arguments.get("timeout_seconds"),
            )
        )
        return {"jsonrpc": "2.0", "id": msg_id, "result": serialize_result(result)}
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    config = build_config(args)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        try:
            message = read_message(stdin)
        except Exception as exc:
            error = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            write_message(stdout, error)
            continue

        if message is None:
            break

        response = handle_message(message, config)
        if response is not None:
            write_message(stdout, response)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
