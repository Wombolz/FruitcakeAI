"""Explicit deletion resets future style without erasing messages or widening access."""
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.db.models import (AdminPolicyEvent, ChatMessage, ChatRun, ChatRunPersonaSnapshot,
                           ChatSession, PersonaDefinition, PersonaRevision, PersonaSelection, User)
from app.personas import service
from tests.conftest import TestSessionLocal
from tests.test_persona_editor_api import png, upload
from tests.test_persona_policy_api import account
from tests.test_persona_revisions_api import create


async def preview(client, headers, persona):
    response = await client.get(f"/personas/{persona['id']}/deletion-preview", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def remove(client, headers, persona, impact=None):
    impact = impact or await preview(client, headers, persona)
    return await client.request("DELETE", f"/personas/{persona['id']}", headers=headers,
        json={"expected_version": impact["expected_version"], "confirmation_token": impact["confirmation_token"]})


async def selected_chat(client, headers, persona):
    chat = (await client.post("/chat/sessions", headers=headers, json={})).json()
    response = await client.patch(f"/chat/sessions/{chat['id']}/persona", headers=headers,
        json={"persona": persona["persona"], "revision": 1, "expected_version": 1})
    assert response.status_code == 200, response.text
    return chat


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["parent", "restricted"])
async def test_delete_resets_chat_default_and_run_snapshots_but_preserves_history_policy_and_other_chats(client, role):
    headers = await account(client, "delete_owner", persona="neutral", role=role)
    persona = await create(client, headers, identity="PRIVATE DELETED IDENTITY")
    chat = await selected_chat(client, headers, persona)
    unaffected = (await client.post("/chat/sessions", headers=headers, json={})).json()
    assert (await client.put("/personas/default", headers=headers,
        json={"persona": persona["persona"], "revision": 1, "expected_version": 0})).status_code == 200
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "delete_owner"))).scalar_one()
        user.library_scopes = ["external_scope", "kids_books"]
        user.calendar_access = ["external_calendar", "family"]
        session = await db.get(ChatSession, chat["id"])
        before, value = await service.session_context(db, user, session)
        db.add(ChatMessage(session_id=session.id, role="user", content="Keep my original message"))
        run = ChatRun(id="deleted-completed-run", session_id=session.id, user_id=user.id, status="completed", phase="done")
        db.add(run)
        await db.flush()
        service.save_run_snapshot(db, run, value)
        await db.commit()
    impact = await preview(client, headers, persona)
    assert impact["affected_conversations"] == 1 and impact["resets_default"]
    response = await remove(client, headers, persona, impact)
    assert response.status_code == 200, response.text
    assert response.json()["replacement"]["persona"] == "family_assistant"
    path = f"/chat/sessions/{chat['id']}"
    result = (await client.get(path, headers=headers)).json()
    assert result["messages"][0]["content"] == "Keep my original message"
    assert result["persona"] == "family_assistant"
    assert result["persona_selection"]["version"] == 3
    assert (await client.get(f"/chat/sessions/{unaffected['id']}/persona", headers=headers)).json()["selection"]["persona"] == "neutral"
    new_chat = (await client.post("/chat/sessions", headers=headers, json={})).json()
    assert new_chat["persona"] == "family_assistant"
    assert (await client.get(f"/personas/{persona['id']}/revisions", headers=headers)).status_code == 404
    assert (await remove(client, headers, persona, impact)).status_code == 404
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "delete_owner"))).scalar_one()
        assert user.persona == "neutral"
        ctx, _ = await service.session_context(db, user, await db.get(ChatSession, chat["id"]))
        assert (ctx.library_scopes, ctx.calendar_access, ctx.blocked_tools, ctx.content_filter) == (
            before.library_scopes, before.calendar_access, before.blocked_tools, before.content_filter)
        assert ctx.persona_identity != "PRIVATE DELETED IDENTITY"
        run = await db.get(ChatRun, "deleted-completed-run")
        resumed = await service.resume_context(db, user, await db.get(ChatSession, chat["id"]), run)
        assert resumed.persona_identity == ctx.persona_identity
        assert "PRIVATE DELETED IDENTITY" not in (await db.get(ChatRunPersonaSnapshot, run.id)).snapshot_json
        assert not (await db.execute(select(PersonaRevision).where(PersonaRevision.persona_id == persona["id"]))).first()
        events = (await db.execute(select(AdminPolicyEvent).where(AdminPolicyEvent.action == "persona.deleted"))).scalars().all()
        assert len(events) == 1 and "PRIVATE DELETED IDENTITY" not in events[0].summary_json


@pytest.mark.asyncio
async def test_deletion_confirmation_owner_scope_validation_and_usage_conflicts(client):
    owner = await account(client, "delete_scope_owner")
    other = await account(client, "delete_scope_other")
    persona = await create(client, owner)
    path = f"/personas/{persona['id']}"
    impact = await preview(client, owner, persona)
    assert (await client.get(path + "/deletion-preview", headers=other)).status_code == 404
    assert (await remove(client, other, persona, impact)).status_code == 404
    assert (await client.request("DELETE", path, json=impact)).status_code == 403
    assert (await client.request("DELETE", path, headers=owner, json={"expected_version": 1})).status_code == 422
    assert (await client.get("/personas/family_assistant/deletion-preview", headers=owner)).status_code == 404
    await selected_chat(client, owner, persona)
    assert (await remove(client, owner, persona, impact)).status_code == 409
    impact = await preview(client, owner, persona)
    await client.put(path, headers=owner, json={"display_name": "Revised", "expected_version": 1})
    assert (await remove(client, owner, persona, impact)).status_code == 409
    impact = await preview(client, owner, persona)
    await client.put("/personas/default", headers=owner,
        json={"persona": persona["persona"], "revision": 2, "expected_version": 0})
    assert (await remove(client, owner, persona, impact)).status_code == 409
    assert (await remove(client, owner, persona)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "waiting_approval", "stopping"])
async def test_deletion_is_atomic_and_rejected_during_any_affected_active_run(client, status):
    headers = await account(client, "delete_busy_owner")
    persona = await create(client, headers)
    chat = await selected_chat(client, headers, persona)
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "delete_busy_owner"))).scalar_one()
        db.add(ChatRun(id="delete-busy-run", session_id=chat["id"], user_id=user.id, status=status, phase=status))
        await db.commit()
    assert (await remove(client, headers, persona)).status_code == 409
    result = (await client.get(f"/chat/sessions/{chat['id']}/persona", headers=headers)).json()
    assert result["selection"]["persona"] == persona["persona"] and result["version"] == 2
    assert (await client.get(f"/personas/{persona['id']}/revisions", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_in_memory_busy_run_and_archived_or_inactive_chat_are_handled(client):
    headers = await account(client, "delete_archived_owner")
    persona = await create(client, headers)
    chat = await selected_chat(client, headers, persona)
    assert (await client.post(f"/personas/{persona['id']}/archive", headers=headers, json={"expected_version": 1})).status_code == 204
    with patch("app.chat_runtime.ChatRunManager.is_active", new=AsyncMock(return_value=True)):
        assert (await remove(client, headers, persona)).status_code == 409
    async with TestSessionLocal() as db:
        session = await db.get(ChatSession, chat["id"])
        session.is_active = False
        await db.commit()
    response = await remove(client, headers, persona)
    assert response.status_code == 200 and response.json()["affected_conversations"] == 1
    async with TestSessionLocal() as db:
        session = await db.get(ChatSession, chat["id"])
        assert not session.is_active and session.persona == "family_assistant"


@pytest.mark.asyncio
async def test_assets_deleted_only_when_no_other_revision_default_or_historical_snapshot_references_them(client):
    headers = await account(client, "delete_asset_owner")
    other = await account(client, "delete_asset_other")
    shared = await upload(client, headers)
    exclusive = await upload(client, headers, png("blue"))
    historical = await upload(client, headers, png("green"))
    foreign = await upload(client, other)
    persona = await create(client, headers, visual_identity={"portrait_asset_id": shared["id"], "sheet_asset_id": exclusive["id"]})
    keeper = await create(client, headers, visual_identity={"portrait_asset_id": shared["id"]})
    history_persona = await create(client, headers, visual_identity={"portrait_asset_id": historical["id"]})
    chat = await selected_chat(client, headers, history_persona)
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "delete_asset_owner"))).scalar_one()
        _, value = await service.session_context(db, user, await db.get(ChatSession, chat["id"]))
        run = ChatRun(id="retained-visual-run", session_id=chat["id"], user_id=user.id, status="completed", phase="done")
        db.add(run)
        await db.flush()
        service.save_run_snapshot(db, run, value)
        await db.commit()
    await client.put(f"/personas/{history_persona['id']}", headers=headers, json={"display_name": "New", "expected_version": 1})
    # This image is also a candidate for deletion, but another run still needs it.
    await client.put(f"/personas/{persona['id']}", headers=headers,
        json={"display_name": "Second", "expected_version": 1, "visual_identity": {"portrait_asset_id": historical["id"]}})
    assert (await remove(client, headers, persona)).status_code == 200
    for asset in (shared, historical):
        assert (await client.get(f"/personas/assets/{asset['id']}", headers=headers)).status_code == 200
    assert (await client.get(f"/personas/assets/{exclusive['id']}", headers=headers)).status_code == 404
    assert (await client.get(f"/personas/assets/{foreign['id']}", headers=other)).status_code == 200
    assert (await client.get(f"/personas/{keeper['id']}/revisions", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_delete_rewrites_old_run_but_not_current_other_persona(client):
    headers = await account(client, "delete_old_run_owner")
    persona = await create(client, headers)
    chat = await selected_chat(client, headers, persona)
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "delete_old_run_owner"))).scalar_one()
        _, value = await service.session_context(db, user, await db.get(ChatSession, chat["id"]))
        run = ChatRun(id="old-persona-run", session_id=chat["id"], user_id=user.id, status="completed", phase="done")
        db.add(run)
        await db.flush()
        service.save_run_snapshot(db, run, value)
        await db.commit()
    await client.patch(f"/chat/sessions/{chat['id']}/persona", headers=headers, json={"persona": "neutral", "expected_version": 2})
    response = await remove(client, headers, persona)
    assert response.status_code == 200 and response.json()["affected_conversations"] == 0
    selection = (await client.get(f"/chat/sessions/{chat['id']}/persona", headers=headers)).json()
    assert selection["selection"]["persona"] == "neutral" and selection["version"] == 3
    async with TestSessionLocal() as db:
        assert json.loads((await db.get(ChatRunPersonaSnapshot, "old-persona-run")).snapshot_json)["persona"] == "family_assistant"


def test_snapshot_matching_compiles_to_postgresql_json_comparison():
    from types import SimpleNamespace
    db = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()))
    expression = service._matches_persona(db, PersonaSelection.snapshot_json, "custom:fixture")
    compiled = str(expression.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "CAST(persona_selections.snapshot_json AS JSON)" in compiled and "->>" in compiled
