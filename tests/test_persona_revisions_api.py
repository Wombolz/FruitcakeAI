"""Saved-personality lifecycle and chat pinning; no inference or production data."""
import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db.models import (AdminPolicyEvent, ChatRun, ChatRunPersonaSnapshot, ChatSession,
                           PersonaRevision, User)
from app.personas import service
from tests.conftest import TestSessionLocal
from tests.test_persona_policy_api import account


async def create(client, headers, **fields):
    response = await client.post("/personas", headers=headers, json={"display_name": "Plain", **fields})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_revision_history_ownership_and_stale_save(client):
    owner = await account(client, "revision_owner")
    other = await account(client, "revision_other")
    assert (await client.get("/personas")).status_code == 403
    persona = await create(client, owner, identity="PRIVATE IDENTITY")
    path = f"/personas/{persona['id']}"
    assert (await client.get(path + "/revisions", headers=other)).status_code == 404
    assert (await client.put(path, headers=other, json={"display_name": "Other", "expected_version": 1})).status_code == 404
    revised = await client.put(path, headers=owner, json={"display_name": "Second", "expected_version": 1})
    assert revised.status_code == 200 and revised.json()["version"] == 2
    assert (await client.put(path, headers=owner, json={"display_name": "Stale", "expected_version": 1})).status_code == 409
    history = (await client.get(path + "/revisions", headers=owner)).json()["revisions"]
    assert [item["version"] for item in history] == [2, 1]
    assert history[1]["personality"]["identity"] == "PRIVATE IDENTITY"
    assert (await client.get(path + "/revisions?before=2&limit=1", headers=owner)).json()["revisions"] == history[1:]
    assert (await client.get("/personas", headers=other)).json()["custom"] == []
    async with TestSessionLocal() as db:
        events = (await db.execute(select(AdminPolicyEvent).where(AdminPolicyEvent.action.like("persona.%")))).scalars().all()
        assert len(events) == 2
        assert all("PRIVATE IDENTITY" not in item.summary_json and "Second" not in item.summary_json for item in events)


@pytest.mark.asyncio
async def test_defaults_are_separate_from_access_and_new_chats_pin_revision(client):
    headers = await account(client, "default_owner", role="restricted")
    persona = await create(client, headers, identity="Independent style")
    ref = {"persona": persona["persona"], "revision": 1}
    response = await client.put("/personas/default", headers=headers, json={**ref, "expected_version": 0})
    assert response.status_code == 200
    assert (await client.put("/personas/default", headers=headers, json={**ref, "expected_version": 0})).status_code == 409
    chat = (await client.post("/chat/sessions", headers=headers, json={})).json()
    assert chat["persona"] == persona["persona"]
    await client.put(f"/personas/{persona['id']}", headers=headers,
                     json={"display_name": "Revision Two", "identity": "Changed", "expected_version": 1})
    pinned = (await client.get(f"/chat/sessions/{chat['id']}/persona", headers=headers)).json()
    assert pinned["selection"]["revision"] == 1 and pinned["update_available"]
    assert (await client.get("/personas/default", headers=headers)).json()["selection"]["revision"] == 1
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "default_owner"))).scalar_one()
        session = await db.get(ChatSession, chat["id"])
        ctx, value = await service.session_context(db, user, session)
        assert value["revision"] == 1 and ctx.persona_identity == "Independent style"
        assert user.persona == "family_assistant" and ctx.content_filter == "strict"
        assert "web_search" in ctx.blocked_tools
        assert ctx.neutral_personality
        await db.commit()


@pytest.mark.asyncio
async def test_explicit_selection_archive_and_busy_turn(client):
    headers = await account(client, "select_owner")
    other = await account(client, "select_other")
    persona = await create(client, headers)
    chat = (await client.post("/chat/sessions", headers=headers, json={})).json()
    path = f"/chat/sessions/{chat['id']}/persona"
    ref = {"persona": persona["persona"], "revision": 1, "expected_version": 1}
    assert (await client.patch(path, headers=headers, json={"persona": persona["persona"]})).status_code == 422
    assert (await client.patch(path, headers=other, json=ref)).status_code == 404
    assert (await client.get(path, headers=other)).status_code == 404
    assert (await client.patch(path, headers=headers, json=ref)).status_code == 200
    assert (await client.patch(path, headers=headers, json=ref)).status_code == 409
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "select_owner"))).scalar_one()
        db.add(ChatRun(id="busy-persona-run", session_id=chat["id"], user_id=user.id,
                       status="waiting_approval", phase="waiting_approval"))
        await db.commit()
    assert (await client.patch(path, headers=headers, json={"persona": "neutral"})).status_code == 409
    with patch("app.api.chat.run_agent", side_effect=AssertionError("No inference")):
        changed = await client.post(f"/chat/sessions/{chat['id']}/messages", headers=headers,
                                    json={"content": "/persona neutral"})
    assert "finish" in changed.json()["content"] and "persona_switched" not in changed.json()
    archive = f"/personas/{persona['id']}/archive"
    assert (await client.post(archive, headers=headers, json={"expected_version": 1})).status_code == 204
    async with TestSessionLocal() as db:
        run = await db.get(ChatRun, "busy-persona-run")
        run.status = "completed"
        await db.commit()
    assert (await client.get(path, headers=headers)).json()["archived"]
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "select_owner"))).scalar_one()
        ctx, _ = await service.session_context(db, user, await db.get(ChatSession, chat["id"]))
        assert ctx.persona == persona["persona"]  # archival does not erase existing selections
    assert (await client.patch(path, headers=headers, json={**ref, "expected_version": 2})).status_code == 409
    assert (await client.put(f"/personas/{persona['id']}", headers=headers,
                             json={"display_name": "No", "expected_version": 1})).status_code == 409


@pytest.mark.asyncio
async def test_archive_default_requires_explicit_replacement_and_duplicate_only_style(client):
    headers = await account(client, "duplicate_owner")
    clone = await client.post("/personas/duplicate", headers=headers,
                              json={"persona": "restricted_assistant", "display_name": "Copy"})
    assert clone.status_code == 201
    persona = clone.json()
    history = (await client.get(f"/personas/{persona['id']}/revisions", headers=headers)).json()
    assert set(history["revisions"][0]["personality"]) == set(service.PersonaDraft.model_fields)
    ref = {"persona": persona["persona"], "revision": 1, "expected_version": 0}
    assert (await client.put("/personas/default", headers=headers, json=ref)).status_code == 200
    archive = f"/personas/{persona['id']}/archive"
    assert (await client.post(archive, headers=headers, json={"expected_version": 1})).status_code == 409
    assert (await client.put("/personas/default", headers=headers,
                             json={"persona": "neutral", "expected_version": 1})).status_code == 200
    assert (await client.post(archive, headers=headers, json={"expected_version": 1})).status_code == 204
    assert (await client.post("/personas/duplicate", headers=headers,
                              json={"persona": "neutral", "display_name": "   "})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"blocked_tools": []}, {"library_scopes": ["private"]}, {"content_filter": ""},
    {"display_name": " padded "}, {"identity": "bad\u0000"}, {"identity": "x" * 2001},
    {"behavior_instructions": ["x" * 1001]}, {"behavior_instructions": ["x" * 1000] * 24},
    {"behavior_instructions": [1]}, {"personality_mode": "admin"},
])
async def test_invalid_or_permission_bearing_drafts_rejected(client, extra):
    headers = await account(client, "invalid_draft_owner")
    assert (await client.post("/personas", headers=headers, json={"display_name": "Valid", **extra})).status_code == 422


@pytest.mark.asyncio
async def test_run_snapshot_survives_revision_and_selection_changes_but_policy_is_live(client):
    headers = await account(client, "snapshot_owner")
    persona = await create(client, headers, identity="Original")
    chat = (await client.post("/chat/sessions", headers=headers, json={})).json()
    path = f"/chat/sessions/{chat['id']}/persona"
    await client.patch(path, headers=headers, json={"persona": persona["persona"], "revision": 1, "expected_version": 1})
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "snapshot_owner"))).scalar_one()
        session = await db.get(ChatSession, chat["id"])
        ctx, value = await service.session_context(db, user, session)
        run = ChatRun(id="snapshot-run", session_id=session.id, user_id=user.id, status="completed", phase="done")
        db.add(run)
        await db.flush()
        service.save_run_snapshot(db, run, value)
        await db.commit()
    await client.put(f"/personas/{persona['id']}", headers=headers,
                     json={"display_name": "Changed", "identity": "Changed", "expected_version": 1})
    await client.patch(path, headers=headers, json={"persona": "neutral", "expected_version": 2})
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "snapshot_owner"))).scalar_one()
        user.role = "restricted"
        await db.flush()
        ctx = await service.resume_context(db, user, await db.get(ChatSession, chat["id"]), await db.get(ChatRun, "snapshot-run"))
        assert ctx.persona_identity == "Original" and ctx.content_filter == "strict"
        assert "web_search" in ctx.blocked_tools
        assert json.loads((await db.get(ChatRunPersonaSnapshot, "snapshot-run")).snapshot_json)["revision"] == 1


@pytest.mark.asyncio
async def test_custom_effective_tools_and_other_owner_reference(client):
    headers = await account(client, "tools_owner", role="restricted")
    other = await account(client, "tools_other")
    persona = await create(client, headers)
    url = f"/chat/tools?persona={persona['persona']}&revision=1"
    result = await client.get(url, headers=headers)
    assert result.status_code == 200 and result.json()["content_filter"] == "strict"
    assert "web_search" in result.json()["blocked_tools"]
    assert (await client.get(url, headers=other)).status_code == 404
    assert (await client.get(f"/chat/tools?persona={persona['persona']}", headers=headers)).status_code == 422
    assert (await client.get("/chat/tools?revision=1", headers=headers)).status_code == 422
    assert (await client.get("/chat/tools?persona=neutral&revision=1", headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_inheritance_is_resolved_at_save_and_explicit_empty_stays_empty(client):
    headers = await account(client, "inherit_owner")
    inherited = await create(client, headers, personality_mode="default")
    empty = await create(client, headers, personality_mode="default", identity="", tone="")
    neutral = await create(client, headers)
    async with TestSessionLocal() as db:
        for definition, should_be_empty in ((inherited, False), (empty, True), (neutral, True)):
            row = (await db.execute(select(PersonaRevision).where(PersonaRevision.persona_id == definition["id"]))).scalar_one()
            style = json.loads(row.payload_json)
            assert (style["identity"] == "") is should_be_empty
            assert (style["tone"] == "") is should_be_empty


@pytest.mark.asyncio
async def test_legacy_chat_pins_current_builtin_without_changing_history(client):
    headers = await account(client, "legacy_owner")
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "legacy_owner"))).scalar_one()
        session = ChatSession(user_id=user.id, persona="work_assistant", title="Legacy")
        db.add(session)
        await db.flush()
        ctx, value = await service.session_context(db, user, session)
        assert ctx.persona == "work_assistant" and value["revision"] is None
        await db.commit()
        session_id = session.id
    result = (await client.get(f"/chat/sessions/{session_id}/persona", headers=headers)).json()
    assert result["version"] == 1 and result["selection"]["persona"] == "work_assistant"


@pytest.mark.asyncio
async def test_rest_approval_uses_saved_run_personality_after_edit(client):
    from unittest.mock import AsyncMock
    from app.autonomy.approval import ApprovalRequired, build_blocked_tool_approval_payload

    headers = await account(client, "actual_replay_owner")
    persona = await create(client, headers, identity="Frozen for this run")
    chat = (await client.post("/chat/sessions", headers=headers, json={})).json()
    path = f"/chat/sessions/{chat['id']}"
    selected = await client.patch(path + "/persona", headers=headers,
        json={"persona": persona["persona"], "revision": 1, "expected_version": 1})
    assert selected.status_code == 200
    call = {"id": "persona-call", "type": "function", "function": {
        "name": "append_file", "arguments": json.dumps({"path": "test.md", "content": "fixture"})}}

    async def pause(history, ctx, *, pre_tool_callback, **kwargs):
        assert ctx.persona_identity == "Frozen for this run"
        await pre_tool_callback([call])
        raise ApprovalRequired("append_file", payload=build_blocked_tool_approval_payload(
            "append_file", {"path": "test.md", "content": "fixture"}))

    with patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=pause)):
        response = await client.post(path + "/messages", headers=headers,
            json={"content": "exercise the frozen persona approval fixture", "approval_mode": True})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    changed = await client.put(f"/personas/{persona['id']}", headers=headers,
        json={"display_name": "New style", "identity": "Not for this run", "expected_version": 1})
    assert changed.status_code == 200
    assert (await client.patch(path + "/persona", headers=headers, json={"persona": "neutral"})).status_code == 409

    async def finish(history, ctx, **kwargs):
        assert ctx.persona_identity == "Frozen for this run"
        return "Fixture complete."

    async def replay(payload, ctx):
        assert ctx.persona_identity == "Frozen for this run"
        return "Fixture result", {"tool_name": "append_file", "arguments": payload["arguments"]}

    with patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=finish)), \
         patch("app.api.chat.replay_waiting_approval_tool", new=AsyncMock(side_effect=replay)):
        response = await client.post(f"/chat/runs/{run_id}/approval", headers=headers, json={"approved": True})
    assert response.status_code == 200 and response.json()["state"] == "completed", response.text
    async with TestSessionLocal() as db:
        saved = json.loads((await db.get(ChatRunPersonaSnapshot, run_id)).snapshot_json)
        assert saved["revision"] == 1 and saved["personality"]["identity"] == "Frozen for this run"


@pytest.mark.asyncio
async def test_websocket_turn_persists_same_revision_used_by_agent(client):
    from unittest.mock import AsyncMock
    from app.api.chat import _run_websocket_message
    from app.chat_runtime import get_chat_run_manager

    headers = await account(client, "actual_stream_owner")
    persona = await create(client, headers, identity="Streaming style")
    chat = (await client.post("/chat/sessions", headers=headers, json={})).json()
    selected = await client.patch(f"/chat/sessions/{chat['id']}/persona", headers=headers,
        json={"persona": persona["persona"], "revision": 1, "expected_version": 1})
    assert selected.status_code == 200
    sent = []

    class Socket:
        async def send_json(self, payload):
            sent.append(payload)

    async def finish(history, ctx, **kwargs):
        assert ctx.persona_identity == "Streaming style"
        return "Streaming fixture complete."

    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "actual_stream_owner"))).scalar_one()
        session = await db.get(ChatSession, chat["id"])
        with patch("app.api.chat.should_validate_chat_response", return_value=True), \
             patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=finish)):
            await _run_websocket_message(session_id=session.id, websocket=Socket(), db=db,
                current_user=user, session=session, user_message="exercise the persona stream fixture",
                client_send_id="persona-stream-fixture", allowed_tools=None, blocked_tools=None)
    await get_chat_run_manager().clear(chat["id"])
    started = next(item for item in sent if item.get("type") == "run_started")
    async with TestSessionLocal() as db:
        run = await db.get(ChatRun, started["run_id"])
        assert run.status == "completed", sent
        value = json.loads((await db.get(ChatRunPersonaSnapshot, run.id)).snapshot_json)
        assert value["persona"] == persona["persona"] and value["revision"] == 1


@pytest.mark.asyncio
async def test_empty_persona_references_are_validation_errors_not_server_errors(client):
    headers = await account(client, "blank_ref_owner")
    chat = (await client.post("/chat/sessions", headers=headers, json={})).json()
    assert (await client.get("/chat/tools?persona=", headers=headers)).status_code == 422
    assert (await client.patch(f"/chat/sessions/{chat['id']}/persona", headers=headers,
                               json={"persona": "   "})).status_code == 422
