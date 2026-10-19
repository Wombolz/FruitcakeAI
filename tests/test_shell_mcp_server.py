from __future__ import annotations

import json
import os
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
    resolve_working_directory,
)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("FRUITCAKE_TEST_DOCKER") != "1", reason="Opt-in Docker protocol test")
async def test_candidate_container_serves_shell_tools_over_real_mcp(tmp_path):
    from app.mcp.client import MCPClient

    image = os.environ.get("FRUITCAKE_TEST_DOCKER_IMAGE", "fruitcake/mcp-shell")
    client = MCPClient("shell-container-fixture", command="docker", args=[
        "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "256m",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={tmp_path},dst=/workspace",
        image, "--allowed-paths", "/workspace",
    ])
    try:
        assert await client.connect(), client.get_status()
        assert [tool["name"] for tool in client.get_tools()] == ["shell_exec"]
        response = await client.call_tool("shell_exec", {"command": "python --version"})
        assert response["success"], response
        result = json.loads(response["result"]["content"][0]["text"])
        assert result["status"] == "completed" and "Python 3.11.17" in result["stdout"]
        refused = await client.call_tool("shell_exec", {"command": "curl https://example.com"})
        result = json.loads(refused["result"]["content"][0]["text"])
        assert result["status"] == "refused" and result["exit_code"] == 126
    finally:
        await client.disconnect()


def test_detect_blocked_command_rejects_network_tool():
    reason = detect_blocked_command("curl https://example.com", DEFAULT_BLOCKED_PATTERNS)
    assert reason is not None
    assert "network" in reason


def test_shell_tool_description_instructs_tool_level_refusal():
    description = TOOL_SCHEMA["description"].lower()
    command_desc = TOOL_SCHEMA["inputSchema"]["properties"]["command"]["description"].lower()
    assert "local tests" in description
    assert "stable status" in description
    assert "network" in command_desc
    assert TOOL_SCHEMA["inputSchema"]["additionalProperties"] is False
    assert "working_directory" in TOOL_SCHEMA["inputSchema"]["properties"]
    assert "timeout_seconds" in TOOL_SCHEMA["inputSchema"]["properties"]


def test_resolve_execution_dir_uses_user_scoped_workspace(tmp_path):
    config = ShellServerConfig(allowed_paths=[tmp_path / "workspace"])
    target = resolve_execution_dir(config, {"user_id": 21})
    assert target == (tmp_path / "workspace" / "21").resolve()
    assert target.is_dir()


def test_resolve_working_directory_stays_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    nested = workspace / "src"
    nested.mkdir(parents=True)
    assert resolve_working_directory(workspace, "src") == nested.resolve()
    with pytest.raises(ValueError, match="escaped"):
        resolve_working_directory(workspace, "../outside")
    with pytest.raises(ValueError, match="does not exist"):
        resolve_working_directory(workspace, "missing")


@pytest.mark.asyncio
async def test_execute_command_succeeds_in_allowed_workspace(tmp_path):
    config = ShellServerConfig(allowed_paths=[tmp_path / "workspace"], timeout_seconds=5, output_limit_bytes=1024)
    result = await execute_command("pwd", config=config, user_context={"user_id": 7})
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert result["stdout"].strip().endswith("/7")


@pytest.mark.asyncio
async def test_execute_command_refuses_blocked_command(tmp_path):
    config = ShellServerConfig(allowed_paths=[tmp_path / "workspace"], timeout_seconds=5, output_limit_bytes=1024)
    result = await execute_command("curl https://example.com", config=config, user_context={"user_id": 7})
    assert result["exit_code"] == 126
    assert result["status"] == "refused"
    assert result["refused"] is True


@pytest.mark.asyncio
async def test_execute_command_times_out(tmp_path):
    config = ShellServerConfig(allowed_paths=[tmp_path / "workspace"], timeout_seconds=1, output_limit_bytes=1024)
    result = await execute_command("sleep 2", config=config, user_context={"user_id": 7})
    assert result["exit_code"] == 124
    assert result["status"] == "timed_out"
    assert result["timed_out"] is True


@pytest.mark.asyncio
async def test_execute_command_uses_relative_working_directory_and_bounded_timeout(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "9" / "src").mkdir(parents=True)
    config = ShellServerConfig(allowed_paths=[workspace], timeout_seconds=5, output_limit_bytes=1024)
    result = await execute_command(
        "pwd",
        config=config,
        user_context={"user_id": 9},
        working_directory="src",
        timeout_seconds=2,
    )
    assert result["status"] == "completed"
    assert result["stdout"].strip().endswith("/9/src")

    invalid = await execute_command(
        "pwd",
        config=config,
        user_context={"user_id": 9},
        timeout_seconds="later",
    )
    assert invalid["status"] == "invalid_arguments"
    assert invalid["exit_code"] == 2


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
        return json.loads(proc.stdout.readline())

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
