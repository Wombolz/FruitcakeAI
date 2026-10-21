"""Persona editor boundaries: private images, bounded preview and fresh test chats."""
import hashlib
import io
import json
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image, PngImagePlugin
from sqlalchemy import func, select

from app.db.models import ChatSession, PersonaDefinition, PersonaRevision, PersonaVisualAsset, User
from app.personas import service
from tests.conftest import TestSessionLocal
from tests.test_persona_policy_api import account


def png(color="red"):
    data = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Description", "PRIVATE FILE METADATA")
    Image.new("RGB", (30, 40), color).save(data, format="PNG", pnginfo=info)
    return data.getvalue()


async def upload(client, headers, data=None):
    response = await client.post("/personas/assets", headers=headers,
                                 files={"file": ("../../ignored.svg", data if data is not None else png(), "image/svg+xml")})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_reference_upload_is_normalized_private_and_not_a_path(client):
    owner = await account(client, "image_owner")
    other = await account(client, "image_other")
    asset = await upload(client, owner)
    response = await client.get(f"/personas/assets/{asset['id']}", headers=owner)
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert hashlib.sha256(response.content).hexdigest() == asset["sha256"]
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (30, 40) and not image.info
    assert b"PRIVATE FILE METADATA" not in response.content
    assert (await client.get(f"/personas/assets/{asset['id']}", headers=other)).status_code == 404
    assert (await client.get(f"/personas/assets/{asset['id']}")).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"<svg><script>alert(1)</script></svg>", b"", b"x" * (8 * 1024 * 1024 + 1)])
async def test_unsupported_empty_and_oversized_uploads_rejected(client, data):
    headers = await account(client, "bad_image_owner")
    response = await client.post("/personas/assets", headers=headers, files={"file": ("fake.png", data, "image/png")})
    assert response.status_code in (413, 422)
    async with TestSessionLocal() as db:
        assert (await db.execute(select(func.count(PersonaVisualAsset.id)))).scalar_one() == 0


@pytest.mark.asyncio
async def test_visual_refs_are_owner_scoped_and_saved_with_immutable_revision(client):
    owner = await account(client, "visual_owner")
    other = await account(client, "visual_other")
    first = await upload(client, owner)
    second = await upload(client, owner, png("blue"))
    body = {"display_name": "Character", "visual_identity": {"sheet_asset_id": first["id"]}}
    assert (await client.post("/personas", headers=other, json=body)).status_code == 404
    created = await client.post("/personas", headers=owner, json=body)
    assert created.status_code == 201
    item = created.json()
    changed = await client.put(f"/personas/{item['id']}", headers=owner,
        json={**body, "expected_version": 1, "visual_identity": {"sheet_asset_id": second["id"]}})
    assert changed.status_code == 200
    history = (await client.get(f"/personas/{item['id']}/revisions", headers=owner)).json()["revisions"]
    assert history[1]["personality"]["visual_identity"]["sheet_asset_id"] == first["id"]
    assert history[1]["personality"]["visual_asset_hashes"][first["id"]] == first["sha256"]
    assert (await client.get(f"/personas/assets/{first['id']}", headers=owner)).status_code == 200
    clone = await client.post("/personas/duplicate", headers=owner,
                              json={"persona": item["persona"], "revision": 1, "display_name": "Copy"})
    assert clone.status_code == 201
    bad = await client.put(f"/personas/{item['id']}", headers=owner,
        json={**body, "expected_version": 2, "visual_identity": {"sheet_asset_id": "00000000-0000-0000-0000-000000000000"}})
    assert bad.status_code == 404
    latest = (await client.get(f"/personas/{item['id']}/revisions", headers=owner)).json()
    assert latest["version"] == 2  # failed asset validation rolls back the head CAS


@pytest.mark.asyncio
async def test_asset_quota_and_pixel_limits(client, monkeypatch):
    headers = await account(client, "quota_owner")
    from app.personas import assets
    monkeypatch.setattr(assets, "MAX_IMAGE_PIXELS", 1)
    response = await client.post("/personas/assets", headers=headers, files={"file": ("image.png", png(), "image/png")})
    assert response.status_code == 413
    monkeypatch.setattr(assets, "MAX_IMAGE_PIXELS", 4_000_000)
    monkeypatch.setattr(assets, "MAX_OWNER_BYTES", 1)
    response = await client.post("/personas/assets", headers=headers, files={"file": ("image.png", png(), "image/png")})
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_preview_has_provenance_and_no_history_memory_or_image_payload(client):
    headers = await account(client, "preview_owner", role="restricted")
    draft = {"display_name": "Draft only", "identity": "A character", "behavior_instructions": ["Use concise replies."]}
    with patch("app.api.chat.run_agent", side_effect=AssertionError("No model in preview")):
        response = await client.post("/personas/preview", headers=headers, json={"draft": draft})
    assert response.status_code == 200, response.text
    result = response.json()
    assert [item["source"] for item in result["layers"]] == ["persona_identity", "session_context", "persona_behavior", "runtime_contract", "access_restrictions"]
    assert result["layers"][0]["content"] == "A character"
    assert "web_search" in result["blocked_tools"] and result["content_filter"] == "strict"
    assert result["visual_generation_available"] is False
    assert "Recalled memories" in result["exclusions"] and "preview_owner" not in json.dumps(result)
    async with TestSessionLocal() as db:
        assert (await db.execute(select(func.count(PersonaDefinition.id)))).scalar_one() == 0
    assert (await client.post("/personas/preview", headers=headers, json={"draft": draft, "reference": {"persona": "neutral"}})).status_code == 422
    assert (await client.post("/personas/preview", headers=headers, json={})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("recall", [False, True])
async def test_fresh_chat_pins_reference_model_and_only_toggles_automatic_recall(client, recall):
    headers = await account(client, f"fresh_owner_{recall}")
    models = (await client.get("/chat/models", headers=headers)).json()["models"]
    body = {"reference": {"persona": "neutral"}, "llm_model": models[0]["id"], "memory_recall_enabled": recall}
    created = await client.post("/personas/test-conversations", headers=headers, json=body)
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    history = (await client.get(f"/chat/sessions/{session_id}", headers=headers)).json()
    assert history["messages"] == [] and history["llm_model"] == models[0]["id"]
    assert history["persona_selection"]["test_options"]["memory_recall_enabled"] is recall
    observed = []

    async def finish(history, ctx, **kwargs):
        observed.append(ctx)
        return "Fresh test fixture."

    async def memory(history, *args, **kwargs):
        assert recall, "Automatic recall must be skipped for this test"
        return history, []

    with patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=finish)), \
         patch("app.api.chat._apply_memory_context", new=AsyncMock(side_effect=memory)) as injected:
        response = await client.post(f"/chat/sessions/{session_id}/messages", headers=headers, json={"content": f"fresh persona recall fixture {recall}"})
    assert response.status_code == 200, response.text
    assert injected.await_count == int(recall)
    assert observed[0].memory_recall_enabled is recall
    assert "create_memory" not in observed[0].blocked_tools  # recall is not a write-permission switch


@pytest.mark.asyncio
async def test_fresh_test_rejects_unavailable_models_before_creating_chat(client):
    headers = await account(client, "bad_test_model")
    result = await client.post("/personas/test-conversations", headers=headers,
                               json={"reference": {"persona": "neutral"}, "llm_model": "not-authorized"})
    assert result.status_code == 403
    async with TestSessionLocal() as db:
        assert (await db.execute(select(func.count(ChatSession.id)))).scalar_one() == 0


@pytest.mark.asyncio
async def test_fresh_socket_skips_automatic_recall_and_keeps_saved_revision(client):
    from app.api.chat import _run_websocket_message
    from app.chat_runtime import get_chat_run_manager
    from app.db.models import ChatRun
    headers = await account(client, "socket_fresh_owner")
    models = (await client.get("/chat/models", headers=headers)).json()["models"]
    created = (await client.post("/personas/test-conversations", headers=headers,
        json={"reference": {"persona": "neutral"}, "llm_model": models[0]["id"], "memory_recall_enabled": False})).json()
    sent = []

    class Socket:
        async def send_json(self, payload):
            sent.append(payload)

    async def finish(history, ctx, **kwargs):
        assert ctx.memory_recall_enabled is False
        return "Socket fresh test fixture."

    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "socket_fresh_owner"))).scalar_one()
        session = await db.get(ChatSession, created["session_id"])
        with patch("app.api.chat._apply_memory_context", new=AsyncMock(side_effect=AssertionError("No automatic recall"))) as memory, \
             patch("app.api.chat.should_validate_chat_response", return_value=True), \
             patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=finish)):
            await _run_websocket_message(session_id=session.id, websocket=Socket(), db=db, current_user=user,
                session=session, user_message="socket persona no recall fixture", client_send_id="no-recall-socket",
                allowed_tools=None, blocked_tools=None)
        assert memory.await_count == 0
        started = next(item for item in sent if item.get("type") == "run_started")
        run = await db.get(ChatRun, started["run_id"])
        assert run.status == "completed", sent
        resumed = await service.resume_context(db, user, session, run)
        assert resumed.memory_recall_enabled is False
    await get_chat_run_manager().clear(created["session_id"])


@pytest.mark.asyncio
async def test_animated_reference_is_rejected(client):
    headers = await account(client, "animated_owner")
    data = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(data, format="PNG", save_all=True,
        append_images=[Image.new("RGB", (10, 10), "blue")], duration=100, loop=0)
    response = await client.post("/personas/assets", headers=headers,
                                 files={"file": ("animated.png", data.getvalue(), "image/png")})
    assert response.status_code == 413
