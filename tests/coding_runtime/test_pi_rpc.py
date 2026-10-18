from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.coding_runtime.pi_rpc import (
    PiRpcClient,
    PiRpcCommandError,
    PiRpcConfig,
    PiRpcError,
    PiRpcProcessError,
    PiRpcProtocolError,
    _normalize_event,
)
from app.coding_runtime.pi_profile import PiModelProfile


PEER = Path(__file__).parent.parent / "fixtures" / "pi_rpc_peer.py"


def config(tmp_path: Path, **overrides: object) -> PiRpcConfig:
    values = {
        "workspace": tmp_path,
        "command": (sys.executable, str(PEER)),
        "rpc_arguments": (),
        "tool_allowlist": (),
        "isolate_resources": False,
        "workspace_guard": False,
        "command_timeout_seconds": 0.5,
        "shutdown_timeout_seconds": 0.5,
    }
    values.update(overrides)
    return PiRpcConfig(**values)


@pytest.mark.asyncio
async def test_state_and_prompt_wait_for_agent_settled(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        assert client.version == (1, 3, 0)
        state = await client.get_state()
        result = await client.prompt_and_wait("hello")

    assert state["sessionId"] == "fake-session"
    assert result.disposition == "started"
    assert result.settled is True
    assert result.aborted is False


@pytest.mark.asyncio
async def test_handled_prompt_does_not_wait_for_settled(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        result = await client.prompt_and_wait("handled")

    assert result.disposition == "handled"
    assert result.settled is False


@pytest.mark.asyncio
async def test_normalizes_stream_events_without_splitting_unicode_separator(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        result_task = asyncio.create_task(client.prompt_and_wait("hello"))
        seen = []
        async for event in client.events():
            seen.append(event)
            if event.kind == "run.completed":
                break
        await result_task

    assert [event.kind for event in seen] == [
        "run.started",
        "turn.started",
        "assistant.text_delta",
        "runtime.event",
        "run.completed",
    ]
    assert seen[2].payload["delta"] == "hello\u2028world"


@pytest.mark.asyncio
async def test_steer_follow_up_and_abort_commands(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        assert await client.steer("change direction") == "queued"
        assert await client.follow_up("also test") == "queued"
        settled_after = client.settled_sequence
        await client.abort()
        assert await client.wait_until_settled(after=settled_after) is True


@pytest.mark.asyncio
async def test_command_failure_and_mismatched_response_are_explicit(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        with pytest.raises(PiRpcCommandError, match="simulated failure"):
            await client.send_command("fail")
        with pytest.raises(PiRpcProtocolError, match="did not match"):
            await client.send_command("unknown")


@pytest.mark.asyncio
async def test_malformed_record_becomes_diagnostic_without_losing_settlement(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        result_task = asyncio.create_task(client.prompt_and_wait("malformed"))
        protocol_errors = 0
        async for event in client.events():
            if event.kind == "runtime.protocol_error":
                protocol_errors += 1
            if event.kind == "run.completed":
                break
        result = await result_task

    assert protocol_errors == 1
    assert result.settled is True


@pytest.mark.asyncio
async def test_command_timeout_is_bounded(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        # Measure command timeout, not interpreter startup latency under load.
        startup_config = client.config
        client.config = replace(client.config, command_timeout_seconds=0.05)
        try:
            async with asyncio.timeout(2):
                with pytest.raises(PiRpcError, match="Timed out"):
                    await client.send_command("hang")
        finally:
            client.config = startup_config


@pytest.mark.asyncio
async def test_unexpected_exit_rejects_active_command_and_captures_bounded_stderr(tmp_path):
    async with PiRpcClient(config(tmp_path, stderr_limit_bytes=32)) as client:
        with pytest.raises(PiRpcProcessError, match="code 17"):
            await client.prompt("exit")
        assert client.stderr.endswith("simulated crash")
        assert len(client.stderr.encode()) <= 32


@pytest.mark.asyncio
async def test_stderr_tail_is_bounded(tmp_path):
    async with PiRpcClient(config(tmp_path, stderr_limit_bytes=32)) as client:
        result = await client.prompt_and_wait("stderr")
        await asyncio.sleep(0)
        assert result.settled is True
        assert client.stderr.endswith("tail-marker")
        assert len(client.stderr.encode()) <= 32


@pytest.mark.asyncio
async def test_close_escalates_when_runtime_ignores_stdin_shutdown(tmp_path):
    client = PiRpcClient(
        config(
            tmp_path,
            rpc_arguments=("--stubborn",),
            shutdown_timeout_seconds=0.05,
        )
    )
    await client.start()

    await asyncio.wait_for(client.close(), timeout=0.5)

    assert client.running is False


@pytest.mark.asyncio
async def test_invalid_workspace_and_missing_executable_fail_cleanly(tmp_path):
    missing_workspace = PiRpcClient(config(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        await missing_workspace.start()

    missing_binary = PiRpcClient(
        PiRpcConfig(workspace=tmp_path, command=("fruitcake-command-that-does-not-exist",), rpc_arguments=())
    )
    with pytest.raises(PiRpcProcessError, match="Unable to probe"):
        await missing_binary.start()


def test_default_launch_profile_is_read_only_and_resource_isolated(tmp_path):
    argv = PiRpcConfig(workspace=tmp_path).argv()

    assert argv[:4] == ("pi", "--mode", "rpc", "--no-session")
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
    assert "--no-mcp" in argv
    assert "--no-extensions" in argv
    assert "--no-skills" in argv
    assert "--no-prompt-templates" in argv
    assert "--no-context-files" in argv
    assert "--no-approve" in argv


def test_process_environment_does_not_inherit_unrelated_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("FRUITCAKE_DATABASE_PASSWORD", "not-for-pi")
    config_value = PiRpcConfig(workspace=tmp_path, environment={"ANTHROPIC_API_KEY": "coding-only"})

    environment = config_value.process_environment()

    assert "FRUITCAKE_DATABASE_PASSWORD" not in environment
    assert environment["ANTHROPIC_API_KEY"] == "coding-only"


@pytest.mark.asyncio
async def test_runtime_uses_isolated_pi_config_directory(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        runtime_environment = client.config.process_environment(
            runtime_config_dir=client._runtime_config_dir
        )

        assert runtime_environment["PI_CODING_AGENT_DIR"] != str(Path.home() / ".pi" / "agent")
        assert Path(runtime_environment["PI_CODING_AGENT_DIR"]).is_dir()


@pytest.mark.asyncio
async def test_duplicate_response_does_not_stop_protocol_reader(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        await client.send_command("duplicate")
        assert (await client.get_state())["sessionId"] == "fake-session"


@pytest.mark.asyncio
async def test_close_wakes_settlement_waiter(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        await client.prompt("wait_for_abort")
        waiter = asyncio.create_task(client.wait_until_settled(timeout=30))
        await client.close()
        with pytest.raises(PiRpcProcessError, match="closed"):
            await asyncio.wait_for(waiter, 0.5)


@pytest.mark.asyncio
async def test_cancelled_prompt_closes_active_process(tmp_path):
    async with PiRpcClient(config(tmp_path)) as client:
        task = asyncio.create_task(client.prompt_and_wait("wait_for_abort", timeout=30))
        await client.send_command("get_state")
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.running


@pytest.mark.asyncio
async def test_guard_acknowledgement_is_required_before_start_returns(tmp_path):
    client = PiRpcClient(config(tmp_path, workspace_guard=True, command_timeout_seconds=0.2))
    with pytest.raises(TimeoutError):
        await client.start()
    assert not client.running


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["1.3.1", "2.0.0", "1.3.0-beta", "not-a-version"])
async def test_unverified_versions_are_rejected(tmp_path, version):
    client = PiRpcClient(config(tmp_path, environment={"FAKE_PI_VERSION": version}))
    with pytest.raises((PiRpcProcessError, PiRpcProtocolError)):
        await client.start()
    assert not client.running


@pytest.mark.parametrize("event_type", ["message_start", "message_end", "agent_end", "turn_end", "future_event"])
def test_reasoning_never_leaks_via_message_snapshots_or_unknown_events(event_type):
    message = {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "PRIVATE_REASONING"},
        {"type": "text", "text": "Visible answer"},
    ]}
    event = _normalize_event({"type": event_type, "message": message, "messages": [message]})
    assert "PRIVATE_REASONING" not in json.dumps(event.payload)
    if event.kind == "assistant.message":
        assert event.payload["text"] == "Visible answer"


@pytest.mark.asyncio
async def test_slow_event_consumer_fails_boundedly_instead_of_blocking_stdout(tmp_path):
    async with PiRpcClient(config(tmp_path, event_limit_records=2)) as client:
        with pytest.raises(PiRpcProcessError):
            await client.prompt_and_wait("hello")
        assert client._events.qsize() <= 2


def test_operation_updates_are_snapshots_and_bounded():
    event = _normalize_event({
        "type": "tool_execution_update", "toolCallId": "call-1", "toolName": "read",
        "partialResult": {"content": [{"type": "text", "text": "x" * 100000}]},
    })
    assert event.kind == "operation.output"
    assert event.payload["mode"] == "snapshot"
    assert len(event.payload["text"].encode()) <= 32768


@pytest.mark.parametrize('character', ['x', '\x01', '\U0001f600'])
def test_complete_change_proposal_survives_rpc_normalization(character):
    from app.coding_runtime import changes
    before = character * (changes.FILE_BYTES // len(character.encode('utf-8')))
    proposal = {'protocol': changes.CHANGE_PROTOCOL, 'operation_id': 'edit', 'tool': 'fruitcake_edit',
                'path': 'file', 'before': before, 'after': 'Y' + before[1:]}
    message = json.dumps(proposal, ensure_ascii=False)
    record = {'type': 'extension_ui_request', 'method': 'confirm', 'id': 'confirmation',
              'title': 'Approve file change?', 'message': message}
    assert len(json.dumps(record, ensure_ascii=False).encode()) < PiRpcConfig(workspace=Path('.')).record_limit_bytes
    event = _normalize_event(record)
    assert json.loads(event.payload['message']) == proposal
    # Ordinary interactions retain their existing small metadata budget.
    record['title'] = 'Read file?'
    assert _normalize_event(record).payload['message']['truncated']


def test_oversized_change_confirmation_is_not_a_partial_approvable_proposal():
    from app.coding_runtime import changes
    event = _normalize_event({'type': 'extension_ui_request', 'method': 'confirm',
                              'title': 'Approve file change?', 'message': 'x' * (changes.PROPOSAL_BYTES + 1)})
    assert isinstance(event.payload['message'], dict)
    assert event.payload['message']['truncated']


@pytest.mark.asyncio
async def test_reasoning_token_ticks_do_not_fill_the_rpc_event_queue(tmp_path):
    from app.coding_runtime.contracts import CodingRuntimeEvent

    client = PiRpcClient(config(tmp_path, event_limit_records=2))
    await client._publish(CodingRuntimeEvent("assistant.reasoning_status", {"status": "thinking_start"}))
    for _ in range(3000):
        await client._publish(CodingRuntimeEvent("assistant.reasoning_status", {"status": "thinking_delta"}))
    await client._publish(CodingRuntimeEvent("assistant.reasoning_status", {"status": "thinking_end"}))
    assert client._events.qsize() == 2
    assert not client._overflowed


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["invalid_shape", "split_utf8"])
async def test_partial_utf8_and_invalid_event_shape_do_not_break_reader(tmp_path, scenario):
    async with PiRpcClient(config(tmp_path)) as client:
        result = await client.prompt_and_wait(scenario)
        assert result.settled
        seen = []
        async for event in client.events():
            seen.append(event)
            if event.kind == "run.completed":
                break
    if scenario == "invalid_shape":
        assert any(event.kind == "runtime.protocol_error" for event in seen)
    else:
        assert any(event.payload.get("delta") == "before\u2028after" for event in seen)


def test_model_credentials_are_kept_out_of_arguments_config_file_and_repr(tmp_path):
    profile = PiModelProfile("test-provider", "test-model", "http://localhost:1234/v1", api_key="private-key")
    config_value = PiRpcConfig(workspace=tmp_path, model_profile=profile)
    assert "private-key" not in repr(profile)
    assert "private-key" not in repr(config_value)
    assert "private-key" not in json.dumps(profile.models_document())
    assert "private-key" not in " ".join(config_value.argv())
    assert config_value.process_environment()["FRUITCAKE_PI_MODEL_KEY"] == "private-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,failed", [("provider_error", True), ("recovered_error", False)])
async def test_settlement_preserves_provider_error_or_successful_recovery(tmp_path, scenario, failed):
    async with PiRpcClient(config(tmp_path)) as client:
        result = await client.prompt_and_wait(scenario)
        assert bool(result.error) is failed
        async for event in client.events():
            if event.kind in {"run.failed", "run.completed"}:
                assert (event.kind == "run.failed") is failed
                break


@pytest.mark.asyncio
async def test_shutdown_removes_descendants_holding_inherited_pipes(tmp_path):
    client = PiRpcClient(config(tmp_path, rpc_arguments=("--child",), shutdown_timeout_seconds=0.1))
    await client.start()
    child_pid = int((tmp_path / "child.pid").read_text())
    await asyncio.wait_for(client.close(), timeout=2)
    for _ in range(20):
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True).stdout.strip()
        if not state or state.startswith("Z"):
            break
        await asyncio.sleep(0.01)
    else:
        os.kill(child_pid, 9)
        pytest.fail("Runtime descendant survived process-group shutdown")
