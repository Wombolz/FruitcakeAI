from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api import coding
from app.auth.jwt import create_access_token, create_refresh_token
from app.db.models import ModelProfile, User, UserModelAccess
from app.db.session import get_db
from test_service import finished, harness, wait_for_status
from test_repository import repository as init_repository


def headers(user_id=2):
    return {"Authorization": f"Bearer {create_access_token(user_id, 'fixture', 'admin')}"}


@asynccontextmanager
async def api_harness(tmp_path, **limits):
    async with harness(tmp_path, **limits) as (service, sessions, factory, workspace, session):
        app = FastAPI()
        app.include_router(coding.router, prefix="/coding")
        app.state.coding_sessions = service
        authentication = {"open": 0}

        async def database():
            async with sessions() as db:
                authentication["open"] += 1
                authentication["session"] = db
                try:
                    yield db
                finally:
                    authentication["open"] -= 1

        app.dependency_overrides[get_db] = database
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test", headers=headers()) as client:
            yield client, app, service, sessions, factory, workspace, session, authentication


def frames(text):
    result = []
    for frame in text.split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        fields["data"] = json.loads(fields["data"])
        result.append(fields)
    return result


@pytest.mark.asyncio
async def test_model_picker_uses_numeric_ids_and_respects_policy(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, *_):
        async with sessions() as db, db.begin():
            db.add_all([
                ModelProfile(id=2, model_id="hosted", display_name="Hosted", provider_family="openai", is_local=False),
                ModelProfile(id=3, model_id="vision", display_name="No tools", provider_family="ollama", is_local=True, supports_tools=False),
                ModelProfile(id=4, model_id="blocked", display_name="Blocked tools", provider_family="ollama", is_local=True, blocked_tools_json='["read","grep","find","ls"]'),
            ])
        response = await client.get("/coding/models")
        assert response.status_code == 200
        assert response.json() == [{"id": 1, "label": "Fixture", "model_id": "ollama_chat/fixture"}]
        async with sessions() as db, db.begin():
            db.add(UserModelAccess(user_id=2, model_profile_id=1, allowed=False))
        assert (await client.get("/coding/models")).json() == []
        assert not factory.instances


@pytest.mark.asyncio
async def test_auth_disabled_and_role_checks(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, *_):
        app.state.coding_sessions = None
        assert (await client.get("/coding/status", headers={"Authorization": ""})).status_code == 403
        assert (await client.get("/coding/status", headers={"Authorization": "Bearer invalid"})).status_code == 401
        refresh = {"Authorization": f"Bearer {create_refresh_token(2)}"}
        assert (await client.get("/coding/status", headers=refresh)).status_code == 401
        assert (await client.get("/coding/status")).json() == {
            "enabled": False, "mode": "read_only", "event_schema_version": 1, "managed_checkouts": False, "approved_edits": False, "container_commands": False,
            "review_captures": False, "ai_reviews": False,
        }
        disabled = await client.get("/coding/sessions")
        assert disabled.status_code == 503
        assert disabled.json()["detail"]["code"] == "coding_unavailable"
        assert not factory.instances
        app.state.coding_sessions = service
        for role, active in [("guest", True), ("restricted", True), ("parent", False)]:
            async with sessions() as db, db.begin():
                user = await db.get(User, 2)
                user.role, user.is_active = role, active
            assert (await client.get("/coding/sessions")).status_code == (403 if active else 401)


@pytest.mark.asyncio
async def test_workspace_grants_are_admin_only_and_owner_scoped(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, _):
        body = {"user_id": 2, "path": str(tmp_path / "repo")}
        # The JWT deliberately claims admin; the persisted user role wins.
        assert (await client.post("/coding/workspaces", json=body)).status_code == 403
        assert (await client.delete(f"/coding/workspaces/{workspace.id}")).status_code == 403
        response = await client.post("/coding/workspaces", json=body, headers=headers(1))
        assert response.status_code == 201
        assert response.json()["id"] == workspace.id
        assert "approved_by_user_id" not in response.json()
        for path in ["/", "relative", str(tmp_path / "missing")]:
            response = await client.post("/coding/workspaces", json={**body, "path": path}, headers=headers(1))
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "invalid_workspace"
        assert (await client.get("/coding/workspaces")).json()[0]["canonical_path"] == str(tmp_path / "repo")
        assert (await client.get("/coding/workspaces", headers=headers(3))).json() == []
        response = await client.post("/coding/sessions", headers=headers(3), json={
            "workspace_id": workspace.id, "model_profile_id": 1,
        })
        assert response.status_code == 403
        assert not factory.instances
        assert (await client.delete(f"/coding/workspaces/{workspace.id}", headers=headers(1))).status_code == 204
        assert not (await client.get("/coding/workspaces")).json()[0]["is_active"]
        assert (await client.post(f"/coding/sessions/{session.id}/runs", json={
            "message": "Read", "client_request_id": "revoked",
        })).status_code == 403


@pytest.mark.asyncio
async def test_session_submission_history_idempotency_and_safe_responses(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, _):
        response = await client.post("/coding/sessions", json={
            "workspace_id": workspace.id, "model_profile_id": 1, "title": "Inspect repo",
        })
        assert response.status_code == 201
        session_id = response.json()["id"]
        assert "runtime_kind" not in response.json()
        path = f"/coding/sessions/{session_id}/runs"
        body = {"message": "Inspect files", "client_request_id": "request-1"}
        response = await client.post(path, json=body)
        assert response.status_code == 202
        run_id = response.json()["id"]
        await finished(service, await service.get_run(user_id=2, run_id=run_id))
        repeated = await client.post(path, json=body)
        assert repeated.status_code == 202
        assert repeated.json()["id"] == run_id
        conflict = await client.post(path, json={**body, "message": "Different"})
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "request_conflict"
        assert len(factory.instances[0].prompts) == 1
        history = (await client.get(path)).json()
        assert len(history) == 1
        assert history[0]["response_text"] == "Visible answer"
        assert "user_id" not in history[0]
        assert (await client.get(f"{path}?offset=1")).json() == []
        assert len((await client.get("/coding/sessions?limit=1")).json()) == 1
        assert (await client.get("/coding/sessions?limit=101")).status_code == 422
        assert (await client.get(f"{path}?offset=-1")).status_code == 422
        for foreign_path in [f"/coding/sessions/{session_id}", path, f"/coding/runs/{run_id}",
                             f"/coding/runs/{run_id}/events", f"/coding/runs/{run_id}/approvals"]:
            assert (await client.get(foreign_path, headers=headers(3))).status_code == 404
        for suffix, payload in [("stop", None), ("control", {"action": "steer", "message": "x"})]:
            assert (await client.post(f"/coding/runs/{run_id}/{suffix}", headers=headers(3), json=payload)).status_code == 404
        assert (await client.post(f"/coding/sessions/{session_id}/close")).status_code == 204
        assert (await client.get(f"/coding/sessions/{session_id}")).json()["status"] == "closed"
        assert (await client.post(path, json={**body, "client_request_id": "new"})).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"message": "x"},
    {"message": "", "client_request_id": "id"},
    {"message": "  ", "client_request_id": "id"},
    {"message": "x", "client_request_id": " "},
    {"message": "x" * 32001, "client_request_id": "id"},
    {"message": 42, "client_request_id": "id"},
    {"message": "x", "client_request_id": "id", "tools": ["bash"]},
])
async def test_submission_validation_cannot_change_runtime_policy(tmp_path, body):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, _):
        assert (await client.post(f"/coding/sessions/{session.id}/runs", json=body)).status_code == 422
        assert not factory.instances


@pytest.mark.asyncio
async def test_sse_replay_reconnect_and_cursor_validation(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, auth):
        submitted = await service.submit(user_id=2, session_id=session.id, message="Inspect")
        result, stored = await finished(service, submitted)
        path = f"/coding/runs/{submitted.id}/events"
        original_stream = coding._event_stream

        async def check_connection(*args, **kwargs):
            assert auth["open"] == 0
            async for chunk in original_stream(*args, **kwargs):
                yield chunk

        # Check the response stream, not only the route's preflight query.
        from unittest.mock import patch
        with patch.object(coding, "_event_stream", check_connection):
            response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        events = frames(response.text)
        assert [int(frame["id"]) for frame in events] == [event.sequence for event in stored]
        assert events[-1]["event"] == "run.completed"
        assert events[-1]["data"]["run_id"] == submitted.id
        assert "DO_NOT_PERSIST" not in response.text
        cursor = events[-2]["id"]
        replay = await client.get(path + "?after=1&follow=false", headers={"Last-Event-ID": cursor})
        assert len(frames(replay.text)) == 1
        assert (await client.get(path, headers={"Last-Event-ID": events[-1]["id"]})).text == ""
        for invalid in ["-1", "wat", "1.0", "9" * 30]:
            assert (await client.get(path, headers={"Last-Event-ID": invalid})).status_code == 422
        assert (await client.get(path + "?after=-1")).status_code == 422
        assert (await client.get(path + f"?after={result.last_event_sequence + 1}")).status_code == 409


@pytest.mark.asyncio
async def test_controls_stop_and_concurrent_stream(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, _):
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect")
        await wait_for_status(service, run.id, "running")
        path = f"/coding/runs/{run.id}"
        assert (await client.post(f"/coding/sessions/{session.id}/close")).status_code == 409
        assert (await client.post(f"/coding/sessions/{session.id}/runs", json={
            "message": "Duplicate", "client_request_id": "other",
        })).status_code == 409
        for action in ["steer", "follow_up"]:
            assert (await client.post(path + "/control", json={"action": action, "message": "Look here"})).status_code == 204
        assert factory.instances[0].controls == [("steer", "Look here"), ("follow_up", "Look here")]
        observer = asyncio.create_task(client.get(path + "/events"))
        await asyncio.sleep(0.02)
        assert (await client.post(path + "/stop")).json() == {"stop_requested": True}
        response = await asyncio.wait_for(observer, 3)
        assert frames(response.text)[-1]["event"] == "run.cancelled"
        assert (await client.get(path)).json()["status"] == "cancelled"
        assert (await client.post(path + "/stop")).json() == {"stop_requested": False}
        assert (await client.post(path + "/control", json={"action": "steer", "message": "x"})).status_code == 409


@pytest.mark.asyncio
async def test_approval_dtos_decisions_and_foreign_access(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, _):
        factory.mode = "approval"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect")
        await wait_for_status(service, run.id, "waiting_approval")
        path = f"/coding/runs/{run.id}/approvals"
        response = await client.get(path)
        assert response.status_code == 200
        assert "runtime_request_id" not in response.text
        assert "private-runtime-request" not in response.text
        assert "payload_json" not in response.text
        approval = response.json()[0]
        assert approval["title"] == "Read fixture?"
        decision = f"/coding/approvals/{approval['id']}/resolve"
        assert (await client.post(decision, json={"approved": True}, headers=headers(3))).status_code == 404
        assert (await client.post(decision, json={"approved": "false"})).status_code == 422
        assert (await client.post(decision, json={"approved": False})).status_code == 204
        assert (await client.post(decision, json={"approved": True})).status_code == 409
        await finished(service, run)
        assert not factory.instances[0].approved
        assert (await client.get(path)).json()[0]["status"] == "denied"


@pytest.mark.asyncio
async def test_stream_heartbeat_disconnect_and_reauthorization(tmp_path, monkeypatch):
    monkeypatch.setattr(coding, "HEARTBEAT_SECONDS", 0.02)
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, _):
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect")
        await wait_for_status(service, run.id, "running")
        current = await service.get_run(user_id=2, run_id=run.id)
        stream = coding._event_stream(service, user_id=2, run_id=run.id, after=current.last_event_sequence, follow=True)
        async with asyncio.timeout(2):
            while await anext(stream) != ": keep-alive\n\n":
                pass  # running state may precede the committed run.started event.
        await stream.aclose()
        assert not factory.instances[0].aborted
        assert (await service.get_run(user_id=2, run_id=run.id)).status == "running"
        stream = coding._event_stream(service, user_id=2, run_id=run.id, after=current.last_event_sequence, follow=True)
        async with asyncio.timeout(2):
            while await anext(stream) != ": keep-alive\n\n":
                pass
        async with sessions() as db, db.begin():
            (await db.get(User, 2)).is_active = False
        error = await asyncio.wait_for(anext(stream), 1)
        # There may be another heartbeat before the next authorization poll.
        while error.startswith(":"):
            error = await asyncio.wait_for(anext(stream), 1)
        assert frames(error)[0]["data"]["code"] == "access_denied"
        await stream.aclose()


@pytest.mark.asyncio
async def test_control_transport_errors_are_not_exposed(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, _):
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect")
        await wait_for_status(service, run.id, "running")

        async def failed_control(message):
            raise RuntimeError("PRIVATE_PROVIDER_CREDENTIAL")

        factory.instances[0].steer = failed_control
        response = await client.post(f"/coding/runs/{run.id}/control", json={"action": "steer", "message": "x"})
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "control_delivery_failed"
        assert "PRIVATE_PROVIDER_CREDENTIAL" not in response.text


@pytest.mark.asyncio
async def test_asgi_disconnect_cleans_observer_but_preserves_run(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, auth):
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect")
        await wait_for_status(service, run.id, "running")
        path = f"/coding/runs/{run.id}/events"
        inbound = asyncio.Queue()
        await inbound.put({"type": "http.request", "body": b"", "more_body": False})
        output = []

        async def receive():
            return await inbound.get()

        async def send(message):
            output.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                assert auth["open"] == 0
                await inbound.put({"type": "http.disconnect"})

        await asyncio.wait_for(app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "scheme": "http", "method": "GET",
            "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "server": ("test", 80), "client": ("test", 1234),
            "headers": [(b"authorization", headers()["Authorization"].encode())],
        }, receive, send), 3)
        assert output[0]["status"] == 200
        assert not factory.instances[0].aborted
        assert (await service.get_run(user_id=2, run_id=run.id)).status == "running"
        assert auth["open"] == 0


@pytest.mark.asyncio
async def test_repository_review_is_owner_scoped_read_only_and_has_bounded_diffs(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, auth):
        root = init_repository(tmp_path / "repo")
        (root / "file.txt").write_text("changed\n")
        (root / "new&odd.txt").write_text("untracked\n")
        path = f"/coding/sessions/{session.id}/repository"
        result = await client.get(path)
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        assert result.json()["basis"] == "current_worktree"
        assert len(result.json()["files"]) == 2
        diff = await client.get(path + "/diff", params={"path": "new&odd.txt"})
        assert diff.status_code == 200
        assert diff.json()["path"] == "new&odd.txt"
        assert "+untracked" in diff.json()["sections"][0]["text"]
        assert (await client.get(path + "/diff")).status_code == 422
        assert (await client.get(path + "/diff", params={"path": "../private"})).status_code == 409
        for user_id in (1, 3):  # Admin is not an override for another owner's session.
            assert (await client.get(path, headers=headers(user_id))).status_code == 404
            assert (await client.get(path + "/diff", params={"path": "file.txt"}, headers=headers(user_id))).status_code == 404
        await service.revoke_workspace(operator_id=1, workspace_id=workspace.id)
        assert (await client.get(path)).status_code == 403
        assert (await client.get(path + "/diff", params={"path": "file.txt"})).status_code == 403
        assert not factory.instances


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_user", [False, True])
async def test_repository_rechecks_authorization_before_delivering_data(tmp_path, revoke_user):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, auth):
        init_repository(tmp_path / "repo")
        entered, release = asyncio.Event(), asyncio.Event()
        original = service._repository.inspect

        async def delayed(root, **kwargs):
            assert not auth["session"].in_transaction()
            entered.set()
            await release.wait()
            return await original(root, **kwargs)

        service._repository.inspect = delayed
        request = asyncio.create_task(client.get(f"/coding/sessions/{session.id}/repository"))
        await asyncio.wait_for(entered.wait(), 2)
        if revoke_user:
            async with sessions() as db, db.begin():
                (await db.get(User, 2)).is_active = False
        else:
            await service.revoke_workspace(operator_id=1, workspace_id=workspace.id)
        release.set()
        response = await asyncio.wait_for(request, 3)
        assert response.status_code == 403
        assert "files" not in response.json()


@pytest.mark.asyncio
async def test_repository_inspection_never_blocks_stop(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, workspace, session, auth):
        init_repository(tmp_path / "repo")
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect")
        await wait_for_status(service, run.id, "running")
        entered, release = asyncio.Event(), asyncio.Event()
        original = service._repository.inspect

        async def delayed(root, **kwargs):
            entered.set()
            await release.wait()
            return await original(root, **kwargs)

        service._repository.inspect = delayed
        request = asyncio.create_task(client.get(f"/coding/sessions/{session.id}/repository"))
        await asyncio.wait_for(entered.wait(), 2)
        response = await asyncio.wait_for(client.post(f"/coding/runs/{run.id}/stop"), 2)
        assert response.status_code == 200
        assert factory.instances[0].aborted
        release.set()
        assert (await asyncio.wait_for(request, 3)).status_code == 200
