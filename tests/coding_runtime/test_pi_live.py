"""Optional installed-Pi protocol checks against a scripted loopback model.

FRUITCAKE_TEST_PI=/path/to/pi .venv/bin/python -m pytest --confcutdir=tests/coding_runtime tests/coding_runtime
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

import pytest

from app.coding_runtime.pi_profile import PiModelProfile
from app.coding_runtime.pi_rpc import PiRpcClient, PiRpcConfig
from app.coding_runtime.pi_runtime import PiCodingRuntime
from app.coding_runtime.contracts import CodingModel, CodingPrompt, CodingRuntimeStart


PI_EXECUTABLE = os.environ.get("FRUITCAKE_TEST_PI")
pytestmark = pytest.mark.skipif(not PI_EXECUTABLE, reason="Set FRUITCAKE_TEST_PI for installed-Pi checks")


@contextmanager
def model_server(steps):
    pending = deque(steps)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            step = pending.popleft() if pending else {"text": "Finished."}
            if step.get("wait"):
                step["wait"].wait(5)
            delta = {"content": step.get("text", "Finished.")}
            reason = "stop"
            if "tool" in step:
                delta = {"tool_calls": [{
                    "index": 0, "id": "call_probe", "type": "function",
                    "function": {"name": step["tool"], "arguments": json.dumps(step["arguments"])},
                }]}
                reason = "tool_calls"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for update, finish in (({"role": "assistant"}, None), (delta, None), ({}, reason)):
                    chunk = {
                        "id": "completion_probe", "object": "chat.completion.chunk", "created": 1,
                        "model": "scripted-model",
                        "choices": [{"index": 0, "delta": update, "finish_reason": finish}],
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def live_config(workspace, endpoint, **overrides):
    return PiRpcConfig(
        workspace=workspace,
        command=(PI_EXECUTABLE,),
        model_profile=PiModelProfile("fruitcake-spike", "scripted-model", endpoint),
        command_timeout_seconds=10,
        **overrides,
    )


async def run_with_events(client, prompt="Inspect the fixture"):
    async def collect():
        seen = []
        async for event in client.events():
            seen.append(event)
            if event.kind in {"run.completed", "run.failed", "session.failed"}:
                return seen
        return seen

    collector = asyncio.create_task(collect())
    try:
        result = await client.prompt_and_wait(prompt, timeout=10)
        return result, await asyncio.wait_for(collector, 2)
    finally:
        if not collector.done():
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning", [False, True])
async def test_installed_pi_reasoning_levels_are_runtime_readback_not_provider_confirmation(tmp_path, reasoning, monkeypatch):
    # Exercise the installed protocol without changing production model profiles.
    original = PiModelProfile.models_document
    def document(profile):
        result = original(profile)
        result["providers"][profile.provider]["models"][0]["reasoning"] = reasoning
        return result
    monkeypatch.setattr(PiModelProfile, "models_document", document)
    with model_server([]) as (endpoint, requests):
        async with PiRpcClient(live_config(tmp_path, endpoint)) as client:
            response = await client.send_command("get_available_thinking_levels")
            levels = response["data"]["levels"]
            if reasoning:
                assert "high" in levels
                await client.send_command("set_thinking_level", level="high")
                assert (await client.get_state())["thinkingLevel"] == "high"
                await client.send_command("set_thinking_level", level="off")
                assert (await client.get_state())["thinkingLevel"] == "off"
            else:
                assert levels == ["off"]
                await client.send_command("set_thinking_level", level="high")
                assert (await client.get_state())["thinkingLevel"] == "off"
    # No prompt, inference, provider confirmation, or live reasoning UI claim.
    assert requests == []


@pytest.mark.asyncio
async def test_installed_pi_model_selection_stream_and_workspace_read(tmp_path):
    (tmp_path / "hello.txt").write_text("authorized fixture content")
    with model_server([{"tool": "read", "arguments": {"path": "hello.txt"}}, {"text": "Read complete."}]) as (endpoint, requests):
        async with PiRpcClient(live_config(tmp_path, endpoint)) as client:
            state = await client.get_state()
            result, events = await run_with_events(client)

    assert state["model"]["provider"] == "fruitcake-spike"
    assert state["model"]["id"] == "scripted-model"
    assert result.error is None
    assert result.settled
    assert any(event.kind == "assistant.text_delta" for event in events)
    operation = next(event for event in events if event.kind == "operation.completed")
    assert operation.payload["is_error"] is False
    assert "authorized fixture content" in operation.payload["text"]
    assert requests[0]["model"] == "scripted-model"
    assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {"read", "grep", "find", "ls"}
    assert not any('Fruitcake command approval workflow' in str(m.get('content')) for m in requests[0]['messages'])


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", ["relative", "absolute", "symlink", "directory_symlink", "sibling_prefix", "at_prefix"])
async def test_installed_pi_blocks_workspace_escape_before_read(tmp_path, escape):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "repo-private"
    outside.mkdir()
    private = outside / "private.txt"
    private.write_text("PRIVATE_FIXTURE_MUST_NOT_REACH_MODEL")
    paths = {
        "relative": "../repo-private/private.txt",
        "absolute": str(private),
        "symlink": "escape.txt",
        "directory_symlink": "external/private.txt",
        "sibling_prefix": str(private),
        "at_prefix": "@../repo-private/private.txt",
    }
    (workspace / "escape.txt").symlink_to(private)
    (workspace / "external").symlink_to(outside, target_is_directory=True)
    with model_server([{"tool": "read", "arguments": {"path": paths[escape]}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(workspace, endpoint)) as client:
            result, events = await run_with_events(client)

    assert result.error is None
    operation = next(event for event in events if event.kind == "operation.completed")
    assert operation.payload["is_error"] is True
    assert "Fruitcake workspace guard" in operation.payload["text"]
    assert "PRIVATE_FIXTURE_MUST_NOT_REACH_MODEL" not in json.dumps(requests)


@pytest.mark.asyncio
async def test_installed_pi_blocks_recursive_search_with_escape_link(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("PRIVATE_FIXTURE_MUST_NOT_REACH_MODEL")
    (workspace / ".ignore").symlink_to(outside)
    with model_server([{"tool": "grep", "arguments": {"path": ".", "pattern": "PRIVATE"}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(workspace, endpoint)) as client:
            _, events = await run_with_events(client)

    operation = next(event for event in events if event.kind == "operation.completed")
    assert operation.payload["is_error"] is True
    assert "PRIVATE_FIXTURE_MUST_NOT_REACH_MODEL" not in json.dumps(requests)


@pytest.mark.asyncio
async def test_installed_pi_aborts_during_provider_request(tmp_path):
    release = threading.Event()
    try:
        with model_server([{"wait": release}]) as (endpoint, _requests):
            async with PiRpcClient(live_config(tmp_path, endpoint)) as client:
                after = client.settled_sequence
                assert await client.prompt("Wait for abort") == "started"
                await asyncio.wait_for(client.abort(), timeout=2)
                assert await client.wait_until_settled(after=after, timeout=2) is True
                assert client.running
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny", "expire"])
async def test_installed_pi_waits_for_host_confirmation_before_read(tmp_path, decision):
    (tmp_path / "hello.txt").write_text("READ_ONLY_AFTER_APPROVAL")
    with model_server([{"tool": "read", "arguments": {"path": "hello.txt"}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(
            tmp_path, endpoint, require_read_approval=True, approval_timeout_seconds=0.5,
        )) as client:
            await client.prompt("Read the fixture after confirmation")
            seen = []
            async for event in client.events():
                seen.append(event)
                if event.kind == "interaction.requested" and event.payload.get("method") == "confirm":
                    assert len(requests) == 1
                    assert not any(item.kind == "operation.completed" for item in seen)
                    assert "READ_ONLY_AFTER_APPROVAL" not in json.dumps(requests)
                    if decision != "expire":
                        await client.respond_to_interaction(
                            event.payload["id"], confirmed=decision == "allow",
                        )
                if event.kind in {"run.completed", "run.failed"}:
                    break

    operation = next(event for event in seen if event.kind == "operation.completed")
    if decision == "allow":
        assert not operation.payload["is_error"]
        assert "READ_ONLY_AFTER_APPROVAL" in operation.payload["text"]
        assert "READ_ONLY_AFTER_APPROVAL" in json.dumps(requests)
    else:
        assert operation.payload["is_error"]
        assert "READ_ONLY_AFTER_APPROVAL" not in json.dumps(requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
async def test_installed_pi_blocks_special_files(tmp_path, kind):
    target = tmp_path / "target"
    if kind == "fifo":
        os.mkfifo(target)
    else:
        source = tmp_path.parent / (tmp_path.name + "-private")
        source.write_text("PRIVATE_HARDLINK")
        os.link(source, target)
    with model_server([{"tool": "read", "arguments": {"path": "target"}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(tmp_path, endpoint)) as client:
            _, events = await run_with_events(client)

    operation = next(event for event in events if event.kind == "operation.completed")
    assert operation.payload["is_error"]
    assert "PRIVATE_HARDLINK" not in json.dumps(requests)


@pytest.mark.asyncio
async def test_guard_rechecks_path_after_host_confirmation(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("Authorized fixture")
    private = tmp_path / "private.txt"
    private.write_text("SECRET_SWAPPED_DURING_APPROVAL")
    with model_server([{"tool": "read", "arguments": {"path": "target.txt"}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(workspace, endpoint, require_read_approval=True)) as client:
            await client.prompt("Read the fixture")
            seen = []
            async for event in client.events():
                seen.append(event)
                if event.kind == "interaction.requested" and event.payload.get("method") == "confirm":
                    target.unlink()
                    target.symlink_to(private)
                    await client.respond_to_interaction(event.payload["id"], confirmed=True)
                if event.kind in {"run.completed", "run.failed"}:
                    break
    operation = next(event for event in seen if event.kind == "operation.completed")
    assert operation.payload["is_error"]
    assert "SECRET_SWAPPED_DURING_APPROVAL" not in json.dumps(requests)


@pytest.mark.asyncio
async def test_unguarded_pi_demonstrates_why_working_directory_is_insufficient(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    private = tmp_path / "private.txt"
    private.write_text("DISPOSABLE_OUTSIDE_BASELINE")
    with model_server([{"tool": "read", "arguments": {"path": "../private.txt"}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(workspace, endpoint, workspace_guard=False)) as client:
            _, events = await run_with_events(client)
    operation = next(event for event in events if event.kind == "operation.completed")
    assert not operation.payload["is_error"]
    assert "DISPOSABLE_OUTSIDE_BASELINE" in json.dumps(requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("write", {"path": "mutation.txt", "content": "MUTATED"}),
    ("bash", {"command": "printf MUTATED > mutation.txt"}),
])
async def test_mutation_and_shell_tools_are_unavailable(tmp_path, tool, args):
    with model_server([{"tool": tool, "arguments": args}]) as (endpoint, requests):
        async with PiRpcClient(live_config(tmp_path, endpoint)) as client:
            result, _events = await run_with_events(client)
    assert result.error is None
    assert not (tmp_path / "mutation.txt").exists()
    assert tool not in {item["function"]["name"] for item in requests[0]["tools"]}


@pytest.mark.asyncio
async def test_guard_rejects_unicode_normalization_alias(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "two\u202fwords.txt").write_text("Authorized fixture")
    private = tmp_path / "private.txt"
    private.write_text("PRIVATE_NORMALIZATION_ALIAS")
    (workspace / "two words.txt").symlink_to(private)
    with model_server([{"tool": "read", "arguments": {"path": "two\u202fwords.txt"}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(workspace, endpoint)) as client:
            _, events = await run_with_events(client)
    operation = next(event for event in events if event.kind == "operation.completed")
    assert operation.payload["is_error"]
    assert "PRIVATE_NORMALIZATION_ALIAS" not in json.dumps(requests)


@pytest.mark.asyncio
async def test_provider_neutral_adapter_preserves_tool_policy_and_restores_context(tmp_path):
    (tmp_path / "hello.txt").write_text("Adapter fixture")
    with model_server([{"tool": "read", "arguments": {"path": "hello.txt"}}]) as (endpoint, requests):
        runtime = PiCodingRuntime(executable=PI_EXECUTABLE, base_url=endpoint, api_key="fixture")
        try:
            await runtime.start(CodingRuntimeStart(
                tmp_path, CodingModel(1, "scripted-model", "local", True), read_tools=("read",),
            ))
            events = [event async for event in runtime.run(CodingPrompt(
                "Continue", history=(("user", "Prior question"), ("assistant", "Prior answer")),
            ))]
        finally:
            await runtime.close()
    assert events[-1].kind == "run.completed"
    assert all(event.provider_event is None for event in events)
    assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {"read"}
    assert "Prior question" in json.dumps(requests[0]["messages"])
    assert "Prior answer" in json.dumps(requests[0]["messages"])
    assert "Current authorized workspace:" in json.dumps(requests[0]["messages"])
    assert "reread files" in json.dumps(requests[0]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['allow', 'deny', 'expire', 'stop', 'no_host_write'])
@pytest.mark.parametrize('absolute', [False, True])
async def test_installed_pi_approved_tools_never_write_themselves(tmp_path, decision, absolute):
    from app.coding_runtime import changes
    target = tmp_path / 'small.txt'
    target.write_text('before\n')
    path = str(target) if absolute else 'small.txt'
    with model_server([{'tool': 'fruitcake_edit', 'arguments': {'path': path, 'oldText': 'before', 'newText': 'after'}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(tmp_path, endpoint, edit_tools=('fruitcake_edit', 'fruitcake_write'),
                                          approval_timeout_seconds=0.4)) as client:
            await client.prompt('Propose the change for host approval')
            seen = []
            async with asyncio.timeout(15):
                async for event in client.events():
                    seen.append(event)
                    if event.kind == 'interaction.requested' and event.payload.get('method') == 'confirm':
                        assert target.read_text() == 'before\n'
                        proposal = json.loads(event.payload['message'])
                        assert proposal['path'] == 'small.txt'
                        assert proposal['before'] == 'before\n' and proposal['after'] == 'after\n'
                        if decision == 'allow':
                            changes.apply(tmp_path, proposal['path'], proposal['before'], proposal['after'])
                        if decision == 'stop':
                            await client.abort()
                        elif decision != 'expire':
                            await client.respond_to_interaction(event.payload['id'], confirmed=decision in {'allow', 'no_host_write'})
                    if event.kind in {'run.completed', 'run.failed'}:
                        break
    assert any(event.kind == 'interaction.requested' and event.payload.get('method') == 'confirm' for event in seen)
    assert target.read_text() == ('after\n' if decision == 'allow' else 'before\n')
    if decision != 'stop':
        operation = next(event for event in seen if event.kind == 'operation.completed')
        assert operation.payload['is_error'] == (decision != 'allow')
    tools = {tool['function']['name'] for tool in requests[0]['tools']}
    assert {'fruitcake_edit', 'fruitcake_write'} <= tools
    assert not {'bash', 'edit', 'write'} & tools


@pytest.mark.asyncio
@pytest.mark.parametrize('large', [False, True])
async def test_installed_pi_service_approves_and_records_host_applied_edit(tmp_path, large):
    from app.coding_runtime import changes
    from test_changes import enable
    from test_service import harness, wait_for_status, finished
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        source, checkout = await enable(service, session, tmp_path)
        target = Path(checkout.canonical_path) / 'file.txt'
        before = 'original\n' + ('x' * (changes.FILE_BYTES - 10) + '\n' if large else '')
        target.write_text(before)
        with model_server([{'tool': 'fruitcake_edit', 'arguments': {'path': 'file.txt', 'oldText': 'original', 'newText': 'approved'}}]) as (endpoint, requests):
            service._factory = lambda: PiCodingRuntime(executable=PI_EXECUTABLE, base_url=endpoint, api_key='fixture')
            run = await service.submit(user_id=2, session_id=session.id, message='Propose this edit for review')
            async with asyncio.timeout(10):
                while True:
                    current = await service.get_run(user_id=2, run_id=run.id)
                    if current.status == 'waiting_approval':
                        break
                    assert current.status in {'starting', 'running'}, (current.status, current.error_code, requests)
                    await asyncio.sleep(0.02)
            assert target.read_text() == before
            approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
            assert '+approved' in approval.change['diff']
            assert approval.change['diff'] == changes.patch('file.txt', before, before.replace('original', 'approved'))
            await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True, change_hash=approval.change["after_hash"])
            result, _ = await finished(service, run)
            assert result.status == 'completed'
            assert target.read_text() == before.replace('original', 'approved')
            assert (source / 'file.txt').read_text() == 'original\n'
            record = (await service.list_changes(user_id=2, run_id=run.id))[0]
            assert record.status == 'applied' and record.after_hash == approval.change['after_hash']


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['allow', 'deny', 'stop', 'docker'])
async def test_installed_pi_container_command_broker_returns_host_result(tmp_path, decision):
    from test_commands import command_setup
    from test_service import harness, finished
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        source, checkout, runner = await command_setup(service, session, tmp_path, 'block' if decision == 'stop' else 'complete')
        if decision == 'docker':
            if os.environ.get('FRUITCAKE_TEST_DOCKER') != '1':
                pytest.skip('Opt-in Docker fixture test')
            from app.coding_runtime.container_commands import DockerCommandRunner
            service._commands = runner = DockerCommandRunner()
        with model_server([{'tool': 'fruitcake_run', 'arguments': {'command': 'python --version'}}]) as (endpoint, requests):
            service._factory = lambda: PiCodingRuntime(executable=PI_EXECUTABLE, base_url=endpoint, api_key='fixture')
            run = await service.submit(user_id=2, session_id=session.id, message='Propose a container command')
            async with asyncio.timeout(15):
                while True:
                    current = await service.get_run(user_id=2, run_id=run.id)
                    if current.status == 'waiting_approval':
                        break
                    assert current.status in {'starting', 'running'}, (current.status, current.error_code)
                    await asyncio.sleep(0.02)
            if decision != 'docker':
                assert not runner.calls
            approval = (await service.list_commands(user_id=2, run_id=run.id))[0]
            await service.resolve_approval(user_id=2, approval_id=approval['approval_id'], approved=decision != 'deny', command_hash=approval['hash'])
            if decision == 'stop':
                await asyncio.wait_for(runner.started.wait(), 5)
                await service.stop(user_id=2, run_id=run.id)
            result, _ = await finished(service, run)
            assert result.status == ('cancelled' if decision == 'stop' else 'completed'), result.error_code
            saved = (await service.list_commands(user_id=2, run_id=run.id))[0]
            assert saved['status'] == {'allow': 'completed', 'deny': 'denied', 'stop': 'cancelled', 'docker': 'completed'}[decision]
            assert (source / 'file.txt').read_text() == 'original\n'
            tools = {tool['function']['name'] for tool in requests[0]['tools']}
            assert 'fruitcake_run' in tools and 'bash' not in tools
            assert any('CALL fruitcake_run' in str(message.get('content')) for message in requests[0]['messages'] if message.get('role') == 'system')
            if decision in {'allow', 'docker'}:
                tool_results = [message for request in requests[1:] for message in request['messages'] if message.get('role') == 'tool']
                assert any(('Python' if decision == 'docker' else 'OK') in str(message.get('content')) for message in tool_results)


@pytest.mark.asyncio
async def test_installed_pi_invalid_command_can_be_corrected_before_host_approval(tmp_path):
    from test_commands import command_setup
    from test_service import harness, finished
    async with harness(tmp_path) as (service, _, _, _, session):
        _, _, runner = await command_setup(service, session, tmp_path)
        steps = [
            {'tool': 'fruitcake_run', 'arguments': {'command': 'python --version', 'working_directory': '/workspace'}},
            {'tool': 'fruitcake_run', 'arguments': {'command': 'python --version', 'working_directory': '.'}},
        ]
        with model_server(steps) as (endpoint, requests):
            service._factory = lambda: PiCodingRuntime(executable=PI_EXECUTABLE, base_url=endpoint, api_key='fixture')
            run = await service.submit(user_id=2, session_id=session.id, message='Run the test command')
            async with asyncio.timeout(15):
                while (current := await service.get_run(user_id=2, run_id=run.id)).status != 'waiting_approval':
                    assert current.status in {'starting', 'running'}, (current.status, current.error_code)
                    await asyncio.sleep(0.02)
            assert not runner.calls
            approvals = await service.list_commands(user_id=2, run_id=run.id)
            assert len(approvals) == 1
            assert approvals[0]['working_directory'] == '.'
            tool_errors = [m for request in requests[1:] for m in request['messages'] if m.get('role') == 'tool']
            assert any('checkout-relative' in str(m.get('content')) for m in tool_errors)
            await service.resolve_approval(user_id=2, approval_id=approvals[0]['approval_id'], approved=True, command_hash=approvals[0]['hash'])
            result, _ = await finished(service, run)
            assert result.status == 'completed'
            assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_installed_pi_prose_approval_is_never_treated_as_a_command(tmp_path):
    from test_commands import command_setup
    from test_service import harness, finished
    async with harness(tmp_path) as (service, _, _, _, session):
        _, _, runner = await command_setup(service, session, tmp_path)
        with model_server([{'text': 'Please approve: python --version'}]) as (endpoint, _):
            service._factory = lambda: PiCodingRuntime(executable=PI_EXECUTABLE, base_url=endpoint, api_key='fixture')
            run = await service.submit(user_id=2, session_id=session.id, message='Run a test')
            result, _ = await finished(service, run)
            assert result.status == 'completed'
            assert not runner.calls
            assert await service.list_approvals(user_id=2, run_id=run.id) == []


@pytest.mark.asyncio
async def test_installed_pi_new_file_absolute_path_uses_relative_approval(tmp_path):
    from app.coding_runtime import changes
    (tmp_path / 'js').mkdir()
    target = tmp_path / 'js/AgentMode.js'
    with model_server([{'tool': 'fruitcake_write', 'arguments': {'path': str(target), 'content': 'export class AgentMode {}\n'}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(tmp_path, endpoint, edit_tools=('fruitcake_write',))) as client:
            await client.prompt('Create the new file after host approval')
            seen = []
            async with asyncio.timeout(10):
                async for event in client.events():
                    seen.append(event)
                    if event.kind == 'interaction.requested' and event.payload.get('method') == 'confirm':
                        proposal = json.loads(event.payload['message'])
                        assert proposal['path'] == 'js/AgentMode.js'
                        assert proposal['before'] is None
                        assert not target.exists()
                        changes.apply(tmp_path, proposal['path'], proposal['before'], proposal['after'])
                        await client.respond_to_interaction(event.payload['id'], confirmed=True)
                    if event.kind in {'run.completed', 'run.failed'}:
                        break
    assert target.read_text() == 'export class AgentMode {}\n'
    assert not next(event for event in seen if event.kind == 'operation.completed').payload['is_error']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['outside', 'sibling_prefix', 'traversal', 'git_metadata', 'parent_link', 'file_link', 'hardlink'])
async def test_installed_pi_absolute_write_cannot_escape_checkout(tmp_path, kind):
    root = tmp_path / 'checkout'
    root.mkdir()
    private = tmp_path / 'private.txt'
    private.write_text('PRIVATE_SOURCE_FILE')
    (root / 'parent').symlink_to(tmp_path, target_is_directory=True)
    (root / 'alias').symlink_to(private)
    os.link(private, root / 'hardlink')
    paths = {'outside': private, 'sibling_prefix': tmp_path / 'checkout-other/file',
             'traversal': root / 'sub/../../private.txt', 'git_metadata': root / '.git/config',
             'parent_link': root / 'parent/private.txt', 'file_link': root / 'alias', 'hardlink': root / 'hardlink'}
    with model_server([{'tool': 'fruitcake_write', 'arguments': {'path': str(paths[kind]), 'content': 'DO_NOT_WRITE'}}]) as (endpoint, requests):
        async with PiRpcClient(live_config(root, endpoint, edit_tools=('fruitcake_write',))) as client:
            _, seen = await run_with_events(client)
    assert not any(event.kind == 'interaction.requested' and event.payload.get('method') == 'confirm' for event in seen)
    operation = next(event for event in seen if event.kind == 'operation.completed')
    assert operation.payload['is_error']
    assert private.read_text() == 'PRIVATE_SOURCE_FILE'
    assert 'PRIVATE_SOURCE_FILE' not in json.dumps(requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['read', 'fruitcake_edit', 'fruitcake_run'])
async def test_installed_pi_default_human_interactions_have_no_deadline(tmp_path, tool):
    (tmp_path / 'fixture.txt').write_text('before\n')
    arguments = {'path': 'fixture.txt'} if tool == 'read' else (
        {'path': 'fixture.txt', 'oldText': 'before', 'newText': 'after'} if tool == 'fruitcake_edit'
        else {'command': 'python --version'})
    with model_server([{'tool': tool, 'arguments': arguments}]) as (endpoint, requests):
        async with PiRpcClient(live_config(
            tmp_path, endpoint, require_read_approval=tool == 'read',
            edit_tools=('fruitcake_edit',) if tool == 'fruitcake_edit' else (),
            command_tools=('fruitcake_run',) if tool == 'fruitcake_run' else (),
        )) as client:
            await client.prompt('Request approval and wait')
            seen = []
            async with asyncio.timeout(15):
                async for event in client.events():
                    seen.append(event)
                    if event.kind == 'interaction.requested' and event.payload.get('method') in {'confirm', 'input'}:
                        assert event.payload.get('timeout') is None
                        await asyncio.sleep(0.6)
                        assert len(requests) == 1
                        assert (tmp_path / 'fixture.txt').read_text() == 'before\n'
                        if tool == 'fruitcake_run':
                            proposal = json.loads(event.payload['placeholder'])
                            value = {'protocol': proposal['protocol'], 'operation_id': proposal['operation_id'], 'status': 'denied'}
                            await client.respond_to_interaction(event.payload['id'], value=json.dumps(value))
                        else:
                            await client.respond_to_interaction(event.payload['id'], confirmed=False)
                    if event.kind in {'run.completed', 'run.failed'}:
                        break
            assert any(event.kind == 'operation.completed' for event in seen)
            assert any(event.kind == 'interaction.requested' for event in seen)
