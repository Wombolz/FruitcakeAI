from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_shell_server.server import (
    DEFAULT_BLOCKED_PATTERNS,
    ShellServerConfig,
    TOOL_SCHEMA,
    detect_blocked_command,
    execute_command,
    resolve_execution_dir,
)


def test_detect_blocked_command_rejects_network_tool():
    reason = detect_blocked_command("curl https://example.com", DEFAULT_BLOCKED_PATTERNS)
    assert reason is not None
    assert "network" in reason


def test_shell_tool_description_instructs_tool_level_refusal():
    description = TOOL_SCHEMA["description"].lower()
    command_desc = TOOL_SCHEMA["inputSchema"]["properties"]["command"]["description"].lower()
    assert "report the tool's refusal result" in description
    assert "blocked-command" in command_desc


def test_resolve_execution_dir_uses_user_scoped_workspace(tmp_path):
    config = ShellServerConfig(allowed_paths=[tmp_path / "workspace"])
    target = resolve_execution_dir(config, {"user_id": 21})
    assert target == (tmp_path / "workspace" / "21").resolve()
    assert target.is_dir()


@pytest.mark.asyncio
async def test_execute_command_succeeds_in_allowed_workspace(tmp_path):
    config = ShellServerConfig(allowed_paths=[tmp_path / "workspace"], timeout_seconds=5, output_limit_bytes=1024)
    result = await execute_command("pwd", config=config, user_context={"user_id": 7})
    assert result["exit_code"] == 0
    assert result["stdout"].strip().endswith("/7")


@pytest.mark.asyncio
async def test_execute_command_refuses_blocked_command(tmp_path):
    config = ShellServerConfig(allowed_paths=[tmp_path / "workspace"], timeout_seconds=5, output_limit_bytes=1024)
    result = await execute_command("curl https://example.com", config=config, user_context={"user_id": 7})
    assert result["exit_code"] == 126
    assert result["refused"] is True


@pytest.mark.asyncio
async def test_execute_command_times_out(tmp_path):
    config = ShellServerConfig(allowed_paths=[tmp_path / "workspace"], timeout_seconds=1, output_limit_bytes=1024)
    result = await execute_command("sleep 2", config=config, user_context={"user_id": 7})
    assert result["exit_code"] == 124
    assert result["timed_out"] is True


@pytest.mark.asyncio
async def test_execute_command_truncates_large_output(tmp_path):
    config = ShellServerConfig(allowed_paths=[tmp_path / "workspace"], timeout_seconds=5, output_limit_bytes=64)
    command = "i=0; while [ $i -lt 500 ]; do printf x; i=$((i+1)); done"
    result = await execute_command(command, config=config, user_context={"user_id": 7})
    assert result["exit_code"] == 0
    assert len(result["stdout"].encode("utf-8")) <= 64
    assert result["stdout_truncated"] is True


def test_stdio_server_smoke(tmp_path):
    allowed = tmp_path / "workspace"
    cmd = [
        sys.executable,
        "-m",
        "mcp_shell_server.server",
        "--allowed-paths",
        str(allowed),
        "--timeout-seconds",
        "5",
        "--output-limit-bytes",
        "256",
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    def read_message() -> dict:
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            chunk = proc.stdout.read(1)
            assert chunk
            header += chunk
        length_line = header.decode().split("\r\n", 1)[0]
        length = int(length_line.split(":", 1)[1].strip())
        payload = proc.stdout.read(length)
        return json.loads(payload.decode())

    def send_message(payload: dict) -> None:
        encoded = json.dumps(payload).encode()
        proc.stdin.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode())
        proc.stdin.write(encoded)
        proc.stdin.flush()

    try:
        send_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
        )
        init = read_message()
        assert init["result"]["serverInfo"]["name"] == "fruitcake-shell"

        send_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )
        send_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        tools = read_message()
        assert tools["result"]["tools"][0]["name"] == "shell_exec"

        send_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "shell_exec",
                    "arguments": {
                        "command": "pwd",
                        "_fruitcake_user_context": {"user_id": 55},
                    },
                },
            }
        )
        result = read_message()
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["exit_code"] == 0
        assert payload["stdout"].strip().endswith("/55")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
