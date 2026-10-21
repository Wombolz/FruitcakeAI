"""Owner-scoped persona selection and effective-policy routes; no inference."""
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.agent.context import UserContext
from app.db.models import User
from tests.conftest import TestSessionLocal


async def account(client, name, *, role="parent", persona="family_assistant"):
    created = await client.post("/auth/register", json={"username": name, "email": f"{name}@example.com",
                                                       "password": "pass123", "role": role})
    assert created.status_code == 201, created.text
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == name))).scalar_one()
        user.persona = persona
        user.library_scopes = ["family_docs", "kids_books", "homework", "work_docs"]
        user.calendar_access = ["family", "personal"]
        await db.commit()
    login = await client.post("/auth/login", json={"username": name, "password": "pass123"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.asyncio
async def test_neutral_is_discoverable_and_tools_require_auth_and_valid_persona(client):
    catalog = (await client.get("/chat/personas")).json()
    assert catalog["neutral"]["display_name"] == "Neutral"
    assert catalog["neutral"]["tone"] == ""
    assert catalog["neutral"]["personality_mode"] == "neutral"
    assert (await client.get("/chat/tools?persona=neutral")).status_code == 403
    headers = await account(client, "neutral_owner")
    assert (await client.get("/chat/tools?persona=missing", headers=headers)).status_code == 400
    default = (await client.get("/chat/tools", headers=headers)).json()
    neutral = (await client.get("/chat/tools?persona=neutral", headers=headers)).json()
    assert default["tools"] == neutral["tools"]
    assert default["blocked_tools"] == neutral["blocked_tools"]


@pytest.mark.asyncio
async def test_patch_and_persona_command_preserve_account_policy_and_ownership(client):
    headers = await account(client, "restricted_owner", role="restricted")
    other = await account(client, "other_owner")
    session = (await client.post("/chat/sessions", json={"title": "Policy test"}, headers=headers)).json()
    path = f"/chat/sessions/{session['id']}"
    assert (await client.patch(path + "/persona", json={"persona": "neutral"}, headers=other)).status_code == 404
    response = await client.patch(path + "/persona", json={"persona": "neutral"}, headers=headers)
    assert response.status_code == 200 and response.json()["persona"] == "neutral"
    policy = (await client.get("/chat/tools?persona=neutral", headers=headers)).json()
    assert policy["content_filter"] == "strict" and "web_search" in policy["blocked_tools"]
    with patch("app.api.chat.run_agent", side_effect=AssertionError("Persona commands must not run a model")):
        changed = await client.post(path + "/messages", json={"content": "/persona neutral"}, headers=headers)
    assert changed.status_code == 200 and changed.json()["persona_switched"] == "neutral"
    assert "web_search" in changed.json()["content"]
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "restricted_owner"))).scalar_one()
        assert user.persona == "family_assistant"  # session choice is not the account baseline
        assert UserContext.from_user(user, "neutral").content_filter == "strict"


@pytest.mark.asyncio
async def test_invalid_account_baseline_cannot_be_bypassed_by_valid_selection(client):
    headers = await account(client, "broken_baseline")
    session = (await client.post("/chat/sessions", json={}, headers=headers)).json()
    path = f"/chat/sessions/{session['id']}"
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "broken_baseline"))).scalar_one()
        user.persona = "missing"
        await db.commit()
    assert (await client.get("/chat/tools?persona=neutral", headers=headers)).status_code == 400
    assert (await client.patch(path + "/persona", json={"persona": "neutral"}, headers=headers)).status_code == 400
    with patch("app.api.chat.run_agent", side_effect=AssertionError("No inference")):
        changed = await client.post(path + "/messages", json={"content": "/persona neutral"}, headers=headers)
    assert changed.status_code == 200 and "persona_switched" not in changed.json()
    assert (await client.get(path, headers=headers)).json()["persona"] == session["persona"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [False, True])
@pytest.mark.parametrize("inactive", [False, True])
async def test_websocket_selection_and_next_turn_refresh_account_policy(client, command, inactive):
    import asyncio
    import json
    from unittest.mock import AsyncMock
    from app.api.chat import chat_websocket
    from app.chat_runtime import get_chat_run_manager

    headers = await account(client, "socket_policy")
    session = (await client.post("/chat/sessions", json={}, headers=headers)).json()
    session_id = session["id"]
    await client.patch(f"/chat/sessions/{session_id}/persona", json={"persona": "neutral"}, headers=headers)
    observed = []

    async def run_message(**kwargs):
        observed.append(UserContext.from_user(kwargs["current_user"], kwargs["session"].persona))

    class Socket:
        def __init__(self):
            self.headers = {"authorization": headers["Authorization"]}
            self.first, self.sent = True, []

        async def accept(self):
            pass

        async def close(self):
            pass

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive_text(self):
            if self.first:
                self.first = False
                return json.dumps({"content": "/persona neutral" if command else "hello", "client_send_id": "policy-fixture"})
            await asyncio.sleep(0)
            raise RuntimeError("fixture socket closed")

    socket = Socket()
    manager = get_chat_run_manager()
    await manager.clear(session_id)
    async with TestSessionLocal() as db:
        stale = (await db.execute(select(User).where(User.username == "socket_policy"))).scalar_one()
        await db.commit()
        async with TestSessionLocal() as update_db:
            changed = await update_db.get(User, stale.id)
            changed.role = "restricted"
            changed.library_scopes = ["kids_books", "homework"]
            changed.calendar_access = ["family"]
            changed.is_active = not inactive
            await update_db.commit()
        assert stale.role == "parent" and stale.is_active
        with patch("app.api.chat._run_websocket_message", new=AsyncMock(side_effect=run_message)):
            await chat_websocket(session_id, socket, db)
    await manager.clear(session_id)
    if inactive:
        assert not observed
        assert any("unavailable" in event.get("content", "") or "no longer active" in event.get("content", "") for event in socket.sent)
    elif command:
        assert not observed
        assert any("web_search" in event.get("content", "") for event in socket.sent)
    else:
        assert len(observed) == 1
        assert observed[0].content_filter == "strict" and "web_search" in observed[0].blocked_tools
        assert observed[0].library_scopes == ["kids_books", "homework"]
