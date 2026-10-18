from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from app.coding_runtime import container_commands as commands
from app.coding_runtime.contracts import CodingRuntimeCapabilities, CodingRuntimeError, CodingRuntimeEvent
from app.coding_runtime.service import CodingServiceError
from app.db.models import CodingApproval, ModelProfile
from test_service import FakeRuntime, harness, wait_for_status, finished
from test_changes import enable
from test_api import api_harness, headers


@pytest.mark.parametrize("input", [
    {}, {"command": ""}, {"command": "x" * 8193}, {"command": "x\0"},
    {"command": "echo hi", "working_directory": "/workspace"},
    {"command": "echo hi", "working_directory": "../source"},
    {"command": "echo hi", "working_directory": ".git"},
    {"command": "echo hi", "timeout_seconds": 1.5},
    {"command": "echo hi", "timeout_seconds": True},
    {"command": "echo hi", "working_directory": None},
    {"command": "echo hi", "network": "host"},
])
def test_extension_rejects_bad_arguments_before_requesting_host_approval(input):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for extension tests")
    script = '''
      import assert from 'node:assert/strict';
      import { registerCommandTool } from './app/coding_runtime/command_tools.mjs';
      let tool; let requests = 0;
      registerCommandTool({on() {}, registerTool(t) { tool = t; }});
      await assert.rejects(tool.execute('operation', JSON.parse(process.argv[1]), null, null,
        {ui: {input() { requests++; throw new Error('Must not reach approval'); }}}));
      assert.equal(requests, 0);
    '''
    result = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(input)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_command_extension_supplies_explicit_approval_workflow_and_default_proposal():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for extension tests")
    script = '''
      import assert from 'node:assert/strict';
      import { registerCommandTool, commandProposal } from './app/coding_runtime/command_tools.mjs';
      const handlers = {};
      registerCommandTool({on(name, handler) { handlers[name] = handler; }, registerTool() {}});
      const prompt = handlers.before_agent_start({systemPrompt: 'BASE POLICY'}).systemPrompt;
      assert.ok(prompt.startsWith('BASE POLICY'));
      assert.ok(prompt.includes('CALL fruitcake_run'));
      assert.ok(prompt.includes("Chat replies such as 'approved' are not host approval"));
      assert.ok(prompt.includes('do not call the tool'));
      console.log(JSON.stringify(commandProposal('fixture', {command: 'python --version'})));
    '''
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert commands.validate_input(json.loads(result.stdout)) == {
        "command": "python --version", "working_directory": ".", "timeout_seconds": 120,
    }


class CommandRuntime(FakeRuntime):
    def __init__(self, proposal=None):
        super().__init__()
        self.proposal = proposal or {"protocol": commands.PROTOCOL, "operation_id": "command-op", "command": "python --version",
                                     "working_directory": ".", "timeout_seconds": 5}
        self.result = None

    def capabilities(self):
        return CodingRuntimeCapabilities(confirmations=True, approved_edits=True, container_commands=True)

    async def run(self, prompt):
        yield CodingRuntimeEvent("operation.started", {"operation_id": "command-op", "tool": commands.TOOL})
        yield CodingRuntimeEvent("interaction.requested", {"id": "private-command", "method": "input", "title": commands.TITLE,
                                                          "placeholder": json.dumps(self.proposal)})
        await self.confirmation.wait()
        yield CodingRuntimeEvent("operation.completed", {"operation_id": "command-op", "tool": commands.TOOL, "is_error": not self.result or self.result["status"] != "completed"})
        yield CodingRuntimeEvent("run.completed", {"aborted": self.aborted})

    async def command_result(self, request_id, result):
        assert request_id == "private-command"
        self.result = result
        self.confirmation.set()


class FixtureRunner:
    def __init__(self, mode="complete"):
        self.mode, self.calls, self.cancelled = mode, [], False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.image = "fruitcake/fixture"

    async def image_id(self):
        return "sha256:" + "a" * 64

    async def recover(self):
        return

    async def execute(self, root, command, output):
        self.calls.append((root, command))
        self.started.set()
        try:
            await output({"stdout": "running fixture", "stderr": ""})
            if self.mode == "block":
                await self.release.wait()
            if self.mode == "cleanup_failure":
                raise CodingRuntimeError("container_cleanup_failed", "fixture cleanup failure")
            return {"status": "completed", "exit_code": 0, "stdout": "OK" + "x" * 20000, "stderr": "", "output_truncated": False}
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def command_setup(service, session, tmp_path, mode="complete"):
    source, checkout = await enable(service, session, tmp_path)
    runner = FixtureRunner(mode)
    service._commands = runner
    await service.set_command_mode(user_id=2, session_id=session.id, enabled=True)
    return source, checkout, runner


@pytest.mark.parametrize("case", ["traversal", "absolute", "metadata", "empty", "too_long", "timeout", "bool_timeout", "unknown_field"])
def test_command_proposal_rejects_unsafe_metadata(case):
    proposal = CommandRuntime().proposal
    if case in {"traversal", "absolute", "metadata"}:
        proposal["working_directory"] = {"traversal": "../source", "absolute": "/tmp", "metadata": ".git"}[case]
    elif case in {"empty", "too_long"}:
        proposal["command"] = "" if case == "empty" else "x" * 8193
    elif case in {"timeout", "bool_timeout"}:
        proposal["timeout_seconds"] = 301 if case == "timeout" else True
    else:
        proposal["network"] = "host"
    with pytest.raises(CodingRuntimeError):
        commands.validate_input(proposal)


def test_command_container_policy_has_only_checkout_mounts_and_no_host_shell(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").write_text("gitdir: /not-mounted/source/.git/worktrees/test\n")
    command = {"id": str(uuid4()), "command": "echo $HOME; python -m unittest", "working_directory": ".",
               "timeout_seconds": 5, "image": "sha256:" + "a" * 64, "network": "none", "filesystem": "checkout_read_write"}
    runner = commands.DockerCommandRunner(executable="/fixture/docker")
    args = runner.create_arguments(root, command)
    assert args[-3:] == ("/bin/sh", "-c", command["command"])
    assert args[-6:-3] == (command["image"], "--kill-after=2", "5")
    for flag, value in [("--network", "none"), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                        ("--pull", "never"), ("--pids-limit", "128"), ("--memory", "512m"), ("--entrypoint", "/usr/bin/timeout")]:
        assert args[args.index(flag) + 1] == value
    assert "--read-only" in args and "--privileged" not in args
    assert sum(arg == "--mount" for arg in args) == 2
    assert f"type=bind,src={root / '.git'},dst=/workspace/.git,readonly" in args
    assert not any("docker.sock" in arg for arg in args)
    digest = commands.command_hash(command)
    command["timeout_seconds"] += 1
    assert commands.command_hash(command) != digest


def test_complete_command_proposal_survives_json_escaping_at_byte_limit():
    from app.coding_runtime.pi_rpc import _normalize_event
    proposal = {**CommandRuntime().proposal, "command": "\x01" * 8190 + "ok"}
    message = json.dumps(proposal)
    record = {"type": "extension_ui_request", "method": "input", "title": commands.TITLE, "placeholder": message}
    normalized = _normalize_event(record)
    assert json.loads(normalized.payload["placeholder"]) == proposal
    assert commands.validate_input(proposal)["command"] == proposal["command"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny", "stop", "expire", "policy", "cleanup_failure"])
async def test_container_commands_require_explicit_review_and_retain_results(tmp_path, decision):
    async with harness(tmp_path) as (service, sessions, _, grant, session):
        source, checkout, runner = await command_setup(service, session, tmp_path, "block" if decision == "stop" else decision)
        runtime = CommandRuntime()
        service._factory = lambda: runtime
        run = await service.submit(user_id=2, session_id=session.id, message="Run the approved fixture")
        await wait_for_status(service, run.id, "waiting_approval")
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        summary = json.loads(approval.payload_json)
        assert not runner.calls
        with pytest.raises(CodingServiceError):
            await service.resolve_approval(user_id=3, approval_id=approval.id, approved=True, command_hash=summary["command"]["hash"])
        for digest in (None, "0" * 64):
            with pytest.raises(CodingServiceError) as failure:
                await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True, command_hash=digest)
            assert failure.value.code == "command_review_required"
        if decision == "expire":
            from datetime import datetime, timedelta, timezone
            async with sessions() as db, db.begin():
                (await db.get(CodingApproval, approval.id)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            with pytest.raises(CodingServiceError):
                await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True, command_hash=summary["command"]["hash"])
            await service.stop(user_id=2, run_id=run.id)
        elif decision == "policy":
            async with sessions() as db, db.begin():
                (await db.get(ModelProfile, 1)).blocked_tools_json = '["bash"]'
            with pytest.raises(CodingServiceError):
                await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True, command_hash=summary["command"]["hash"])
            await service.stop(user_id=2, run_id=run.id)
        else:
            await service.resolve_approval(user_id=2, approval_id=approval.id, approved=decision != "deny", command_hash=summary["command"]["hash"])
            if decision == "stop":
                await asyncio.wait_for(runner.started.wait(), 3)
                await service.stop(user_id=2, run_id=run.id)
                assert runner.cancelled
        result, events = await finished(service, run)
        saved = (await service.list_commands(user_id=2, run_id=run.id))[0]
        assert saved["status"] == {"allow": "completed", "deny": "denied", "expire": "expired", "policy": "cancelled",
                                    "stop": "cancelled", "cleanup_failure": "uncertain"}[decision]
        assert len(runner.calls) == (1 if decision in {"allow", "stop", "cleanup_failure"} else 0)
        if decision == "allow":
            assert saved["result"]["stdout"].startswith("OK")
            assert len(runtime.result["stdout"].encode()) == 16384 and runtime.result["output_truncated"]
            assert any(event.kind == "operation.output" for event in events)
            with pytest.raises(CodingServiceError):
                await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True, command_hash=summary["command"]["hash"])
            assert len(runner.calls) == 1
        assert (source / "file.txt").read_text() == "original\n"
        await service.shutdown()
        await service.startup()
        assert (await service.list_commands(user_id=2, run_id=run.id))[0]["status"] == saved["status"]


@pytest.mark.asyncio
async def test_container_command_api_is_opt_in_and_owner_scoped(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, grant, session, _):
        path = f"/coding/sessions/{session.id}/command-mode"
        assert (await client.post(path, json={"enabled": True})).status_code == 409
        service._commands = FixtureRunner()
        assert (await client.post(path, json={"enabled": True})).status_code != 200
        await enable(service, session, tmp_path)
        assert (await client.post(path, json={"enabled": True})).json()["command_mode"]
        runtime = CommandRuntime()
        service._factory = lambda: runtime
        run = await service.submit(user_id=2, session_id=session.id, message="Run fixture")
        await wait_for_status(service, run.id, "waiting_approval")
        assert (await client.post(path, json={"enabled": False})).status_code == 409
        approval = (await client.get(f"/coding/runs/{run.id}/approvals")).json()[0]
        assert approval["command"]["network"] == "none"
        route = f"/coding/approvals/{approval['id']}/resolve"
        assert (await client.post(route, json={"approved": True})).status_code == 409
        assert (await client.post(route, json={"approved": True, "command_hash": approval["command"]["hash"]})).status_code == 204
        await finished(service, run)
        assert (await client.get(f"/coding/runs/{run.id}/commands")).json()[0]["status"] == "completed"
        assert (await client.get(f"/coding/runs/{run.id}/commands", headers=headers(3))).status_code == 404
        assert not (await client.post(path, json={"enabled": False})).json()["command_mode"]


@pytest.mark.asyncio
async def test_restart_marks_unfinished_command_uncertain_without_reexecution(tmp_path):
    async with harness(tmp_path) as (service, sessions, _, grant, session):
        _, _, runner = await command_setup(service, session, tmp_path)
        service._factory = CommandRuntime
        run = await service.submit(user_id=2, session_id=session.id, message="Propose command")
        await wait_for_status(service, run.id, "waiting_approval")
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        async with sessions() as db, db.begin():
            row = await db.get(CodingApproval, approval.id)
            summary = json.loads(row.payload_json)
            summary["command"]["status"] = "running"
            row.status = "approved"
            row.payload_json = json.dumps(summary)
        await service.shutdown()
        await service.startup()
        assert (await service.list_commands(user_id=2, run_id=run.id))[0]["status"] == "uncertain"
        assert not runner.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unopted", "wrong_method", "wrong_title"])
async def test_unavailable_command_interactions_cannot_fall_back_to_generic_approval(tmp_path, case):
    async with harness(tmp_path) as (service, sessions, _, grant, session):
        await command_setup(service, session, tmp_path)
        if case == "unopted":
            await service.set_command_mode(user_id=2, session_id=session.id, enabled=False)
        class InvalidRuntime(CommandRuntime):
            async def run(self, prompt):
                yield CodingRuntimeEvent("interaction.requested", {"id": "private-command",
                    "method": "confirm" if case == "wrong_method" else "input",
                    "title": "Read file?" if case == "wrong_title" else commands.TITLE,
                    "placeholder": json.dumps(self.proposal)})
                yield CodingRuntimeEvent("run.completed")
        service._factory = InvalidRuntime
        run = await service.submit(user_id=2, session_id=session.id, message="Invalid command")
        result, _ = await finished(service, run)
        assert result.status == "failed"
        assert not await service.list_approvals(user_id=2, run_id=run.id)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("FRUITCAKE_TEST_DOCKER") != "1", reason="Opt-in Docker fixture test")
async def test_real_docker_enforces_mount_network_and_process_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("FRUITCAKE_TEST_SECRET", "fixture-secret-not-for-container")
    runner = commands.DockerCommandRunner(image=os.environ.get("FRUITCAKE_TEST_DOCKER_IMAGE", "fruitcake/mcp-shell"))
    image = await runner.image_id()
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").write_text("gitdir: /source-not-mounted/.git\n")
    private = tmp_path / "not-mounted"
    private.write_text("HOST_PRIVATE")
    script = f'''import os, pathlib, socket
assert not pathlib.Path({str(private)!r}).exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
assert 'FRUITCAKE_TEST_SECRET' not in os.environ
try:
    pathlib.Path('/workspace/.git').write_text('changed')
    raise AssertionError('metadata was writable')
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1', 80), 1)
    raise AssertionError('network available')
except OSError:
    pass
pathlib.Path('/workspace/result').write_text('container-only')
print('ISOLATION_OK')
'''
    import shlex
    command = {"id": str(uuid4()), "command": "python -c " + shlex.quote(script), "working_directory": ".", "timeout_seconds": 10,
               "image": image, "network": "none", "filesystem": "checkout_read_write"}
    async def output(value):
        pass
    result = await runner.execute(root, command, output)
    assert result["status"] == "completed" and "ISOLATION_OK" in result["stdout"], result
    assert (root / "result").read_text() == "container-only"
    assert private.read_text() == "HOST_PRIVATE"
    assert not await runner._control("ps", "-aq", "--filter", f"name=^/{runner.name(command['id'])}$")


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("FRUITCAKE_TEST_DOCKER") != "1", reason="Opt-in Docker fixture test")
async def test_container_watchdog_stops_command_without_attached_host_runner(tmp_path):
    runner = commands.DockerCommandRunner(image=os.environ.get("FRUITCAKE_TEST_DOCKER_IMAGE", "fruitcake/mcp-shell"))
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").write_text("gitdir: /not-mounted\n")
    command = {"id": str(uuid4()), "command": "python -c 'import time; time.sleep(60)'", "working_directory": ".", "timeout_seconds": 1,
               "image": await runner.image_id(), "network": "none", "filesystem": "checkout_read_write"}
    try:
        await runner._control(*runner.create_arguments(root, command))
        await runner._control("start", runner.name(command["id"]))
        async with asyncio.timeout(7):
            while True:
                state = json.loads(await runner._control("inspect", "--format", "{{json .State}}", runner.name(command["id"])))
                if not state["Running"]:
                    break
                await asyncio.sleep(0.1)
        assert state["ExitCode"] in {124, 137}
    finally:
        await runner.remove(command["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel", "output_limit"])
@pytest.mark.skipif(os.environ.get("FRUITCAKE_TEST_DOCKER") != "1", reason="Opt-in Docker fixture test")
async def test_real_docker_bounds_output_and_removes_processes_on_timeout_or_cancel(tmp_path, mode):
    runner = commands.DockerCommandRunner(image=os.environ.get("FRUITCAKE_TEST_DOCKER_IMAGE", "fruitcake/mcp-shell"))
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").write_text("gitdir: /not-mounted\n")
    script = "python -c \"print('x' * 100000)\"" if mode == "output_limit" else "python -u -c \"import time; print('STARTED'); time.sleep(60)\""
    command = {"id": str(uuid4()), "command": script, "working_directory": ".", "timeout_seconds": 1 if mode == "timeout" else 10,
               "image": await runner.image_id(), "network": "none", "filesystem": "checkout_read_write"}
    started = asyncio.Event()
    async def output(value):
        started.set()
    task = asyncio.create_task(runner.execute(root, command, output))
    if mode == "cancel":
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["status"] == ("timed_out" if mode == "timeout" else "completed")
        if mode == "output_limit":
            assert result["output_truncated"] and len(result["stdout"].encode()) == commands.OUTPUT_BYTES
    assert not await runner._control("ps", "-aq", "--filter", f"name=^/{runner.name(command['id'])}$")
