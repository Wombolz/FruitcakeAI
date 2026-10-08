"""
Auth endpoint integration tests.
Uses an in-memory SQLite database so no real postgres is needed.
"""

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select, func
from sqlalchemy.pool import StaticPool
from unittest.mock import AsyncMock, patch

from app.api.chat import _execute_chat_turn, _run_websocket_message, chat_websocket
from app.agent.context import UserContext
from app.chat_runtime import get_chat_run_manager
from app.config import settings
from app.db.session import Base, get_db
from app.db.models import ChatMessage, ChatSession, User
from app.main import app

# ── In-memory SQLite engine for tests ─────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest.fixture(autouse=True)
async def setup_db():
    """Create tables before each test, drop after."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register(client):
    resp = await client.post("/auth/register", json={
        "username": "alice",
        "email": "alice@example.com",
        "password": "secret123",
        "full_name": "Alice",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "alice"
    assert data["role"] == "parent"
    assert data["chat_routing_preference"] == "auto"


@pytest.mark.asyncio
async def test_register_duplicate(client):
    payload = {"username": "bob", "email": "bob@example.com", "password": "pass"}
    await client.post("/auth/register", json=payload)
    resp = await client.post("/auth/register", json=payload)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_login(client):
    await client.post("/auth/register", json={
        "username": "carol",
        "email": "carol@example.com",
        "password": "mypassword",
    })
    resp = await client.post("/auth/login", json={
        "username": "carol",
        "password": "mypassword",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    await client.post("/auth/register", json={
        "username": "dave",
        "email": "dave@example.com",
        "password": "correct",
    })
    resp = await client.post("/auth/login", json={"username": "dave", "password": "wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me(client):
    await client.post("/auth/register", json={
        "username": "eve",
        "email": "eve@example.com",
        "password": "pass123",
    })
    login_resp = await client.post("/auth/login", json={"username": "eve", "password": "pass123"})
    token = login_resp.json()["access_token"]

    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "eve"
    assert UUID(resp.json()["public_id"]).version == 4
    assert resp.json()["chat_routing_preference"] == "auto"


@pytest.mark.asyncio
async def test_update_my_chat_routing_preference(client):
    await client.post("/auth/register", json={
        "username": "prefuser",
        "email": "pref@example.com",
        "password": "pass123",
    })
    login_resp = await client.post("/auth/login", json={"username": "prefuser", "password": "pass123"})
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.patch("/auth/me/preferences", json={"chat_routing_preference": "deep"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["chat_routing_preference"] == "deep"


@pytest.mark.asyncio
async def test_websocket_refreshes_routing_preference_without_reconnect(client):
    await client.post("/auth/register", json={
        "username": "liveprefuser",
        "email": "livepref@example.com",
        "password": "pass123",
    })
    login = await client.post(
        "/auth/login",
        json={"username": "liveprefuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    create = await client.post("/chat/sessions", json={"title": "Live preference"}, headers=headers)
    session_id = create.json()["id"]

    captured_preferences = []

    async def fake_run_message(**kwargs):
        captured_preferences.append(kwargs["current_user"].chat_routing_preference)

    class FakeWebSocket:
        def __init__(self):
            self.headers = {"authorization": f"Bearer {token}"}
            self._first = True

        async def accept(self):
            return None

        async def receive_text(self):
            if self._first:
                self._first = False
                return '{"content":"hello","client_send_id":"live-pref-1"}'
            raise RuntimeError("socket closed")

        async def send_json(self, _payload):
            return None

        async def close(self):
            return None

    async with TestSessionLocal() as db:
        # Prime this long-lived session's identity map with the old value.
        stale_user = (
            await db.execute(select(User).where(User.username == "liveprefuser"))
        ).scalar_one()
        assert stale_user.chat_routing_preference == "auto"
        await db.commit()

        async with TestSessionLocal() as update_db:
            updated_user = (
                await update_db.execute(select(User).where(User.username == "liveprefuser"))
            ).scalar_one()
            updated_user.chat_routing_preference = "fast"
            await update_db.commit()

        assert stale_user.chat_routing_preference == "auto"
        with patch("app.api.chat._run_websocket_message", new=AsyncMock(side_effect=fake_run_message)):
            await chat_websocket(session_id, FakeWebSocket(), db)

    assert captured_preferences == ["fast"]


@pytest.mark.asyncio
async def test_me_unauthenticated(client):
    resp = await client.get("/auth/me")
    assert resp.status_code == 403


# ── Role enforcement ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_endpoint_requires_auth(client):
    """GET /admin/users without a token returns 403."""
    resp = await client.get("/admin/users")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_endpoint_rejects_non_admin(client):
    """A regular (parent-role) user cannot access /admin/users."""
    await client.post("/auth/register", json={
        "username": "regularuser",
        "email": "regular@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "regularuser", "password": "pass123"})
    token = login.json()["access_token"]

    resp = await client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# ── Session CRUD ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_and_list_sessions(client):
    """Create a session then verify it appears in GET /chat/sessions."""
    await client.post("/auth/register", json={
        "username": "chatuser",
        "email": "chat@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "My Session"}, headers=headers)
    assert create.status_code == 201
    session_id = create.json()["id"]
    assert create.json()["llm_model"] is not None

    sessions = await client.get("/chat/sessions", headers=headers)
    assert sessions.status_code == 200
    ids = [s["id"] for s in sessions.json()]
    assert session_id in ids
    created_session = next(s for s in sessions.json() if s["id"] == session_id)
    assert created_session["sort_order"] == 0


@pytest.mark.asyncio
async def test_sessions_default_to_newest_first(client):
    await client.post("/auth/register", json={
        "username": "orderuser",
        "email": "order@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "orderuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post("/chat/sessions", json={"title": "First"}, headers=headers)
    second = await client.post("/chat/sessions", json={"title": "Second"}, headers=headers)
    first_id = first.json()["id"]
    second_id = second.json()["id"]

    sessions = await client.get("/chat/sessions", headers=headers)
    assert sessions.status_code == 200
    data = sessions.json()
    assert [row["id"] for row in data] == [second_id, first_id]
    assert [row["sort_order"] for row in data] == [0, 1]


@pytest.mark.asyncio
async def test_reorder_sessions_persists_order(client):
    await client.post("/auth/register", json={
        "username": "reorderuser",
        "email": "reorder@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "reorderuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post("/chat/sessions", json={"title": "One"}, headers=headers)
    second = await client.post("/chat/sessions", json={"title": "Two"}, headers=headers)
    first_id = first.json()["id"]
    second_id = second.json()["id"]

    reorder = await client.patch(
        "/chat/sessions/order",
        json={"session_ids": [second_id, first_id]},
        headers=headers,
    )
    assert reorder.status_code == 200
    data = reorder.json()
    assert [row["id"] for row in data[:2]] == [second_id, first_id]
    assert [row["sort_order"] for row in data[:2]] == [0, 1]


@pytest.mark.asyncio
async def test_reorder_sessions_rejects_missing_ids(client):
    await client.post("/auth/register", json={
        "username": "reorderreject",
        "email": "reorderreject@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "reorderreject", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post("/chat/sessions", json={"title": "One"}, headers=headers)
    second = await client.post("/chat/sessions", json={"title": "Two"}, headers=headers)
    first_id = first.json()["id"]
    _second_id = second.json()["id"]

    reorder = await client.patch(
        "/chat/sessions/order",
        json={"session_ids": [first_id]},
        headers=headers,
    )
    assert reorder.status_code == 422


@pytest.mark.asyncio
async def test_reordering_overrides_default_order(client):
    await client.post("/auth/register", json={
        "username": "manualorder",
        "email": "manualorder@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "manualorder", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post("/chat/sessions", json={"title": "First"}, headers=headers)
    second = await client.post("/chat/sessions", json={"title": "Second"}, headers=headers)
    first_id = first.json()["id"]
    second_id = second.json()["id"]

    reorder = await client.patch(
        "/chat/sessions/order",
        json={"session_ids": [first_id, second_id]},
        headers=headers,
    )
    assert reorder.status_code == 200

    sessions = await client.get("/chat/sessions", headers=headers)
    assert sessions.status_code == 200
    data = sessions.json()
    assert [row["id"] for row in data] == [first_id, second_id]
    assert [row["sort_order"] for row in data] == [0, 1]


@pytest.mark.asyncio
async def test_chat_send_message_honors_deep_routing_preference(client):
    await client.post("/auth/register", json={
        "username": "deepuser",
        "email": "deep@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "deepuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    pref = await client.patch("/auth/me/preferences", json={"chat_routing_preference": "deep"}, headers=headers)
    assert pref.status_code == 200

    create = await client.post("/chat/sessions", json={"title": "Routing"}, headers=headers)
    session_id = create.json()["id"]

    with patch("app.api.chat._execute_chat_turn", new=AsyncMock(return_value="ok")) as execute_mock:
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "What's the weather today?"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert execute_mock.await_count == 1
    assert execute_mock.await_args.kwargs["mode"] == "chat_orchestrated"
    assert execute_mock.await_args.kwargs["stage"] == "chat_complex"


@pytest.mark.asyncio
async def test_list_llm_models_returns_configured_models(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.openai_api_key", "test-openai-key")
    monkeypatch.setattr("app.config.settings.openai_models", "gpt-5,gpt-5-mini")
    monkeypatch.setattr("app.config.settings.anthropic_api_key", "")
    monkeypatch.setattr("app.config.settings.anthropic_models", "claude-sonnet-4-6")
    monkeypatch.setattr("app.config.settings.local_models", "ollama_chat/qwen2.5:14b")

    await client.post("/auth/register", json={
        "username": "modeluser",
        "email": "model@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "modeluser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get("/llm/models", headers=headers)
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["models"]]
    assert "gpt-5" in ids
    assert "gpt-5-mini" in ids
    assert "ollama_chat/qwen2.5:14b" in ids
    assert "claude-sonnet-4-6" not in ids


@pytest.mark.asyncio
async def test_update_chat_session_model_and_use_override(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.openai_api_key", "test-openai-key")
    monkeypatch.setattr("app.config.settings.openai_models", "gpt-5,gpt-5-mini")

    await client.post("/auth/register", json={
        "username": "chatmodeluser",
        "email": "chatmodel@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatmodeluser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Model Session"}, headers=headers)
    session_id = create.json()["id"]

    update = await client.patch(
        f"/chat/sessions/{session_id}/model",
        json={"llm_model": "gpt-5"},
        headers=headers,
    )
    assert update.status_code == 200
    assert update.json()["llm_model"] == "gpt-5"

    with patch("app.api.chat._execute_chat_turn", new=AsyncMock(return_value="ok")) as execute_mock:
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Hello there"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert execute_mock.await_args.kwargs["model_override"] == "gpt-5"


@pytest.mark.asyncio
async def test_delete_session_removes_it(client):
    """DELETE /chat/sessions/{id} removes the session from the list."""
    await client.post("/auth/register", json={
        "username": "deluser",
        "email": "del@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "deluser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "To Delete"}, headers=headers)
    session_id = create.json()["id"]

    delete = await client.delete(f"/chat/sessions/{session_id}", headers=headers)
    assert delete.status_code == 204

    sessions = await client.get("/chat/sessions", headers=headers)
    ids = [s["id"] for s in sessions.json()]
    assert session_id not in ids


@pytest.mark.asyncio
async def test_delete_session_not_owned_returns_404(client):
    """A user cannot delete another user's session."""
    for username in ("owner", "other"):
        await client.post("/auth/register", json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "pass123",
        })

    owner_login = await client.post("/auth/login", json={"username": "owner", "password": "pass123"})
    owner_token = owner_login.json()["access_token"]
    other_login = await client.post("/auth/login", json={"username": "other", "password": "pass123"})
    other_token = other_login.json()["access_token"]

    create = await client.post(
        "/chat/sessions", json={"title": "Owner's session"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    session_id = create.json()["id"]

    resp = await client.delete(
        f"/chat/sessions/{session_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_session_updates_title(client):
    await client.post("/auth/register", json={
        "username": "renameuser",
        "email": "rename@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "renameuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Before"}, headers=headers)
    session_id = create.json()["id"]

    rename = await client.patch(
        f"/chat/sessions/{session_id}",
        json={"title": "After"},
        headers=headers,
    )
    assert rename.status_code == 200
    assert rename.json()["title"] == "After"

    sessions = await client.get("/chat/sessions", headers=headers)
    titles = {row["id"]: row["title"] for row in sessions.json()}
    assert titles[session_id] == "After"


@pytest.mark.asyncio
async def test_rename_session_not_owned_returns_404(client):
    for username in ("rename_owner", "rename_other"):
        await client.post("/auth/register", json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "pass123",
        })

    owner_login = await client.post("/auth/login", json={"username": "rename_owner", "password": "pass123"})
    owner_token = owner_login.json()["access_token"]
    other_login = await client.post("/auth/login", json={"username": "rename_other", "password": "pass123"})
    other_token = other_login.json()["access_token"]

    create = await client.post(
        "/chat/sessions",
        json={"title": "Owner title"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    session_id = create.json()["id"]

    rename = await client.patch(
        f"/chat/sessions/{session_id}",
        json={"title": "Hacked"},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert rename.status_code == 404


@pytest.mark.asyncio
async def test_update_session_persona(client):
    await client.post("/auth/register", json={
        "username": "personauser",
        "email": "persona@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "personauser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Persona Test"}, headers=headers)
    session_id = create.json()["id"]

    patch_resp = await client.patch(
        f"/chat/sessions/{session_id}/persona",
        json={"persona": "work_assistant"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["persona"] == "work_assistant"


@pytest.mark.asyncio
async def test_personas_endpoint_uses_restricted_naming(client):
    resp = await client.get("/chat/personas")
    assert resp.status_code == 200
    data = resp.json()
    assert data["family_assistant"]["display_name"] == "Personal Assistant"
    assert "restricted_assistant" in data
    assert "kids_assistant" not in data
    assert data["restricted_assistant"]["content_filter"] == "strict"


@pytest.mark.asyncio
async def test_agents_endpoint_lists_built_in_agent_definitions(client):
    resp = await client.get("/chat/agents")
    assert resp.status_code == 200
    data = resp.json()
    categories = {item["id"]: item for item in data["categories"]}
    assert "verify" in categories
    assert "monitor" in categories
    verify_presets = {item["id"]: item for item in categories["verify"]["presets"]}
    monitor_presets = {item["id"]: item for item in categories["monitor"]["presets"]}
    assert "roadmap_verifier" in verify_presets
    assert "runtime_inspector" in verify_presets
    assert "recent_run_analyzer" in verify_presets
    assert "document_sync_manager" in monitor_presets
    assert "repo_map_manager" in monitor_presets
    assert "general_agent" not in verify_presets
    assert verify_presets["roadmap_verifier"]["execution_mode"] == "task"
    assert monitor_presets["document_sync_manager"]["background"] is True


@pytest.mark.asyncio
async def test_chat_tools_endpoint_returns_tools(client):
    await client.post("/auth/register", json={
        "username": "tooluser",
        "email": "tooluser@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "tooluser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get("/chat/tools", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "tools" in data
    assert "search_library" in data["tools"]


@pytest.mark.asyncio
async def test_send_message_applies_tool_overrides(client):
    await client.post("/auth/register", json={
        "username": "overrideuser",
        "email": "override@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "overrideuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Override Test"}, headers=headers)
    session_id = create.json()["id"]

    with patch("app.api.chat.run_agent", new_callable=AsyncMock, return_value="ok") as mock_run:
        send = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "hello", "allowed_tools": ["search_library"]},
            headers=headers,
        )

    assert send.status_code == 200
    user_context = mock_run.await_args.args[1]
    assert "create_memory" in user_context.blocked_tools
    assert "search_library" not in user_context.blocked_tools


@pytest.mark.asyncio
async def test_admin_push_test_endpoint(client):
    await client.post("/auth/register", json={
        "username": "adminpush",
        "email": "adminpush@example.com",
        "password": "pass123",
        "role": "admin",
    })
    login = await client.post("/auth/login", json={"username": "adminpush", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Register one device token for this admin user
    reg = await client.post(
        "/devices/register",
        json={"token": "deadbeef-token", "environment": "sandbox"},
        headers=headers,
    )
    assert reg.status_code == 200

    fake_pusher = type("FakePusher", (), {"send": AsyncMock(return_value=True)})()
    with patch("app.api.admin.get_apns_pusher", return_value=fake_pusher):
        resp = await client.post("/admin/push/test", headers=headers, json={})

    assert resp.status_code == 200
    data = resp.json()
    assert data["attempted"] == 1
    assert data["delivered"] == 1


# ── Token validation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_token_rejected(client):
    """A malformed JWT returns 401 or 403."""
    resp = await client.get("/auth/me", headers={"Authorization": "Bearer notavalidtoken"})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_missing_bearer_prefix_rejected(client):
    """Token without 'Bearer' prefix is rejected."""
    await client.post("/auth/register", json={
        "username": "tokentest",
        "email": "tt@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "tokentest", "password": "pass123"})
    token = login.json()["access_token"]

    resp = await client.get("/auth/me", headers={"Authorization": token})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_session_history_includes_message_timestamps(client):
    await client.post("/auth/register", json={
        "username": "historyuser",
        "email": "history@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "historyuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "History"}, headers=headers)
    session_id = create.json()["id"]

    with patch("app.api.chat.run_agent", new_callable=AsyncMock, return_value="ok"):
        sent = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "hello history timestamp check"},
            headers=headers,
        )
    assert sent.status_code == 200

    history = await client.get(f"/chat/sessions/{session_id}", headers=headers)
    assert history.status_code == 200
    messages = history.json()["messages"]
    assert len(messages) >= 2
    assert all("created_at" in msg for msg in messages)


@pytest.mark.asyncio
async def test_chat_history_persists_assistant_task_draft_metadata(client):
    await client.post("/auth/register", json={
        "username": "draftpersistuser",
        "email": "draftpersist@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "draftpersistuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Draft Metadata"}, headers=headers)
    session_id = create.json()["id"]
    draft_payload = {
        "proposed": True,
        "title": "Review Fed rate-cut timeline",
        "instruction": "Compare September cut odds against the forward curve before Friday's open.",
        "persona": "family_assistant",
        "profile": None,
        "task_type": "one_shot",
        "schedule": None,
        "deliver": True,
        "requires_approval": True,
        "task_recipe": None,
    }

    with (
        patch("app.api.chat._execute_chat_turn", new_callable=AsyncMock, return_value="Prepared task draft"),
        patch("app.api.chat.get_task_handoff_payload", return_value={"task_draft": draft_payload}),
        patch(
            "app.api.chat.get_tool_execution_records",
            return_value=[{"tool": "propose_task_draft", "arguments": {}, "result_summary": "ok"}],
        ),
    ):
        sent = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Make a task to review the rate-cut timeline before Friday and compare it with the forward curve."},
            headers=headers,
        )

    assert sent.status_code == 200
    assert isinstance(sent.json()["message_id"], int)
    metadata = sent.json()["metadata"]
    assert metadata["task_draft"]["title"] == "Review Fed rate-cut timeline"
    assert metadata["task_draft_status"] == "draft"
    assert metadata["tool_calls"] == ["propose_task_draft"]

    history = await client.get(f"/chat/sessions/{session_id}", headers=headers)
    assert history.status_code == 200
    assistant = [msg for msg in history.json()["messages"] if msg["role"] == "assistant"][-1]
    assert assistant["metadata"]["task_draft"]["title"] == "Review Fed rate-cut timeline"
    assert assistant["metadata"]["task_draft_status"] == "draft"
    assert assistant["metadata"]["tool_calls"] == ["propose_task_draft"]


@pytest.mark.asyncio
async def test_accept_task_draft_creates_task_once_and_updates_message_metadata(client):
    await client.post("/auth/register", json={
        "username": "draftacceptuser",
        "email": "draftaccept@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "draftacceptuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Accept Draft"}, headers=headers)
    session_id = create.json()["id"]
    draft_payload = {
        "proposed": True,
        "title": "Review Fed rate-cut timeline",
        "instruction": "Compare September cut odds against the forward curve before Friday's open.",
        "persona": "family_assistant",
        "profile": None,
        "llm_model_override": None,
        "task_type": "one_shot",
        "schedule": None,
        "deliver": True,
        "requires_approval": True,
        "active_hours_start": None,
        "active_hours_end": None,
        "active_hours_tz": None,
        "task_recipe": None,
    }

    with (
        patch("app.api.chat._execute_chat_turn", new_callable=AsyncMock, return_value="Prepared task draft"),
        patch("app.api.chat.get_task_handoff_payload", return_value={"task_draft": draft_payload}),
        patch(
            "app.api.chat.get_tool_execution_records",
            return_value=[{"tool": "propose_task_draft", "arguments": {}, "result_summary": "ok"}],
        ),
    ):
        sent = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Make a task to review the rate-cut timeline before Friday."},
            headers=headers,
        )

    assert sent.status_code == 200
    message_id = sent.json()["message_id"]
    assert isinstance(message_id, int)

    first_accept = await client.post(f"/chat/messages/{message_id}/task-draft/accept", headers=headers)
    assert first_accept.status_code == 200
    first_payload = first_accept.json()
    assert first_payload["created"] is True
    assert first_payload["reused_existing"] is False
    assert first_payload["title"] == "Review Fed rate-cut timeline"
    task_id = first_payload["task_id"]
    assert first_payload["metadata"]["task_draft_status"] == "accepted"
    assert first_payload["metadata"]["created_task_id"] == task_id

    second_accept = await client.post(f"/chat/messages/{message_id}/task-draft/accept", headers=headers)
    assert second_accept.status_code == 200
    second_payload = second_accept.json()
    assert second_payload["created"] is True
    assert second_payload["reused_existing"] is True
    assert second_payload["task_id"] == task_id

    tasks = await client.get("/tasks", headers=headers)
    assert tasks.status_code == 200
    assert len(tasks.json()) == 1
    assert tasks.json()[0]["id"] == task_id

    refreshed = await client.get(f"/chat/sessions/{session_id}", headers=headers)
    refreshed_assistant = [msg for msg in refreshed.json()["messages"] if msg["role"] == "assistant"][-1]
    assert refreshed_assistant["metadata"]["task_draft_status"] == "accepted"
    assert refreshed_assistant["metadata"]["created_task_id"] == task_id


@pytest.mark.asyncio
async def test_deny_task_draft_updates_message_metadata_without_creating_task(client):
    await client.post("/auth/register", json={
        "username": "draftdenyuser",
        "email": "draftdeny@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "draftdenyuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Deny Draft"}, headers=headers)
    session_id = create.json()["id"]
    draft_payload = {
        "proposed": True,
        "title": "Monitor gasoline prices",
        "instruction": "Track local pump prices for the next two weeks.",
        "persona": "family_assistant",
        "task_type": "one_shot",
        "deliver": True,
        "requires_approval": True,
    }

    with (
        patch("app.api.chat._execute_chat_turn", new_callable=AsyncMock, return_value="Prepared task draft"),
        patch("app.api.chat.get_task_handoff_payload", return_value={"task_draft": draft_payload}),
        patch(
            "app.api.chat.get_tool_execution_records",
            return_value=[{"tool": "propose_task_draft", "arguments": {}, "result_summary": "ok"}],
        ),
    ):
        sent = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Make a task to monitor gasoline prices."},
            headers=headers,
        )

    message_id = sent.json()["message_id"]
    denied = await client.post(f"/chat/messages/{message_id}/task-draft/deny", headers=headers)
    assert denied.status_code == 200
    denied_payload = denied.json()
    assert denied_payload["denied"] is True
    assert denied_payload["metadata"]["task_draft_status"] == "denied"
    assert "created_task_id" not in denied_payload["metadata"]

    denied_again = await client.post(f"/chat/messages/{message_id}/task-draft/deny", headers=headers)
    assert denied_again.status_code == 200
    assert denied_again.json()["metadata"]["task_draft_status"] == "denied"

    tasks = await client.get("/tasks", headers=headers)
    assert tasks.status_code == 200
    assert tasks.json() == []

    refreshed = await client.get(f"/chat/sessions/{session_id}", headers=headers)
    refreshed_assistant = [msg for msg in refreshed.json()["messages"] if msg["role"] == "assistant"][-1]
    assert refreshed_assistant["metadata"]["task_draft_status"] == "denied"
    assert "created_task_id" not in refreshed_assistant["metadata"]


@pytest.mark.asyncio
async def test_accept_task_draft_can_link_existing_task(client):
    await client.post("/auth/register", json={
        "username": "draftlinkuser",
        "email": "draftlink@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "draftlinkuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    task_resp = await client.post(
        "/tasks",
        json={
            "title": "Edited linked task",
            "instruction": "Use edited details from the draft sheet.",
            "task_type": "one_shot",
            "deliver": True,
            "requires_approval": True,
        },
        headers=headers,
    )
    assert task_resp.status_code == 201
    existing_task_id = task_resp.json()["id"]

    create = await client.post("/chat/sessions", json={"title": "Link Existing Task"}, headers=headers)
    session_id = create.json()["id"]
    draft_payload = {
        "proposed": True,
        "title": "Original draft title",
        "instruction": "Original draft instruction.",
        "persona": "family_assistant",
        "task_type": "one_shot",
        "deliver": True,
        "requires_approval": True,
    }

    with (
        patch("app.api.chat._execute_chat_turn", new_callable=AsyncMock, return_value="Prepared task draft"),
        patch("app.api.chat.get_task_handoff_payload", return_value={"task_draft": draft_payload}),
        patch(
            "app.api.chat.get_tool_execution_records",
            return_value=[{"tool": "propose_task_draft", "arguments": {}, "result_summary": "ok"}],
        ),
    ):
        sent = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Make a task draft for later editing."},
            headers=headers,
        )

    message_id = sent.json()["message_id"]
    linked = await client.post(
        f"/chat/messages/{message_id}/task-draft/accept",
        json={"existing_task_id": existing_task_id},
        headers=headers,
    )
    assert linked.status_code == 200
    linked_payload = linked.json()
    assert linked_payload["reused_existing"] is True
    assert linked_payload["task_id"] == existing_task_id
    assert linked_payload["metadata"]["task_draft_status"] == "accepted"
    assert linked_payload["metadata"]["created_task_id"] == existing_task_id

    tasks = await client.get("/tasks", headers=headers)
    assert tasks.status_code == 200
    assert len(tasks.json()) == 1
    assert tasks.json()[0]["id"] == existing_task_id


@pytest.mark.asyncio
async def test_chat_history_normalizes_legacy_created_task_draft_status(client):
    await client.post("/auth/register", json={
        "username": "draftlegacyuser",
        "email": "draftlegacy@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "draftlegacyuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Legacy Draft"}, headers=headers)
    session_id = create.json()["id"]

    async with TestSessionLocal() as db:
        session = (
            await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one()
        db.add(
            ChatMessage(
                session_id=session.id,
                role="assistant",
                content="Legacy accepted draft",
                tool_results=json.dumps(
                    {
                        "kind": "assistant_message_metadata",
                        "task_draft": {
                            "title": "Legacy task",
                            "instruction": "Legacy instruction.",
                            "task_type": "one_shot",
                            "deliver": True,
                            "requires_approval": True,
                        },
                        "task_draft_status": "created",
                        "created_task_id": 42,
                    }
                ),
            )
        )
        await db.commit()

    history = await client.get(f"/chat/sessions/{session_id}", headers=headers)
    assert history.status_code == 200
    assistant = [msg for msg in history.json()["messages"] if msg["role"] == "assistant"][-1]
    assert assistant["metadata"]["task_draft_status"] == "accepted"
    assert assistant["metadata"]["created_task_id"] == 42


@pytest.mark.asyncio
async def test_stop_chat_session_returns_false_when_idle(client):
    await client.post("/auth/register", json={
        "username": "chatstopidle",
        "email": "chatstopidle@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatstopidle", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Stop Idle"}, headers=headers)
    session_id = create.json()["id"]

    stop = await client.post(f"/chat/sessions/{session_id}/stop", headers=headers)
    assert stop.status_code == 200
    assert stop.json() == {"stopped": False, "session_id": session_id}


@pytest.mark.asyncio
async def test_stop_chat_session_cancels_active_rest_run(client):
    await client.post("/auth/register", json={
        "username": "chatstoprun",
        "email": "chatstoprun@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatstoprun", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Stop Active"}, headers=headers)
    session_id = create.json()["id"]
    started = asyncio.Event()

    async def _slow_run_agent(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(60)
        return "done"

    with patch("app.api.chat.run_agent", new=AsyncMock(side_effect=_slow_run_agent)):
        send_task = asyncio.create_task(
            client.post(
                f"/chat/sessions/{session_id}/messages",
                json={"content": "do something long"},
                headers=headers,
            )
        )
        await started.wait()
        stop = await client.post(f"/chat/sessions/{session_id}/stop", headers=headers)
        assert stop.status_code == 200
        assert stop.json() == {"stopped": True, "session_id": session_id}

        send = await send_task

    assert send.status_code == 409
    assert send.json()["detail"] == "Chat stopped by user"

    stop_again = await client.post(f"/chat/sessions/{session_id}/stop", headers=headers)
    assert stop_again.status_code == 200
    assert stop_again.json() == {"stopped": False, "session_id": session_id}


@pytest.mark.asyncio
async def test_rest_duplicate_prompt_is_rejected_before_execution(client):
    await client.post("/auth/register", json={
        "username": "chatrestdupe",
        "email": "chatrestdupe@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatrestdupe", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "REST Dupe Guard"}, headers=headers)
    session_id = create.json()["id"]
    started = asyncio.Event()

    async def _slow_execute(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(60)
        return "done"

    with patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=_slow_execute)):
        first_task = asyncio.create_task(
            client.post(
                f"/chat/sessions/{session_id}/messages",
                json={"content": "tell me about Iran headlines", "client_send_id": "rest-dupe-1"},
                headers=headers,
            )
        )
        await started.wait()
        second = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "tell me about Iran headlines", "client_send_id": "rest-dupe-1"},
            headers=headers,
        )
        first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task

    assert second.status_code == 409
    assert "A matching chat request is already running." in second.text


@pytest.mark.asyncio
async def test_websocket_duplicate_prompt_is_rejected_before_execution(client):
    await client.post("/auth/register", json={
        "username": "chatdupeuser",
        "email": "chatdupe@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatdupeuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Dupe Guard"}, headers=headers)
    session_id = create.json()["id"]
    prompt = "run the search against my saved feeds and give me the results you find"

    async with TestSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "chatdupeuser"))
        ).scalar_one()
        session = (
            await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one()

        manager = get_chat_run_manager()
        await manager.clear(session_id)
        manager._recent_prompts.pop(session_id, None)
        await manager.claim_prompt(session_id, prompt)

        websocket = AsyncMock()
        with patch("app.api.chat._execute_chat_turn", new=AsyncMock(return_value="done")) as execute_mock:
            await _run_websocket_message(
                session_id=session_id,
                websocket=websocket,
                db=db,
                current_user=user,
                session=session,
                user_message=prompt,
                client_send_id="test-send-id",
                allowed_tools=None,
                blocked_tools=None,
            )

        assert execute_mock.await_count == 0
        websocket.send_json.assert_awaited_once()
        payload = websocket.send_json.await_args.args[0]
        assert payload["type"] == "error"
        assert "matching chat request" in payload["content"].lower()

        message_count = await db.scalar(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id == session_id)
        )
        assert message_count == 0
        manager._recent_prompts.pop(session_id, None)


@pytest.mark.asyncio
async def test_websocket_duplicate_client_send_id_is_rejected_before_execution(client):
    await client.post("/auth/register", json={
        "username": "chatdupesendid",
        "email": "chatdupesendid@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatdupesendid", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Send ID Guard"}, headers=headers)
    session_id = create.json()["id"]
    prompt = "show me the latest iran headlines"

    async with TestSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "chatdupesendid"))
        ).scalar_one()
        session = (
            await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one()

        manager = get_chat_run_manager()
        await manager.clear(session_id)
        manager._recent_prompts.pop(session_id, None)
        manager._recent_send_ids.pop(session_id, None)
        await manager.claim_client_send_id(session_id, "same-send-id")

        websocket = AsyncMock()
        with patch("app.api.chat._execute_chat_turn", new=AsyncMock(return_value="done")) as execute_mock:
            await _run_websocket_message(
                session_id=session_id,
                websocket=websocket,
                db=db,
                current_user=user,
                session=session,
                user_message=prompt,
                client_send_id="same-send-id",
                allowed_tools=None,
                blocked_tools=None,
            )

        assert execute_mock.await_count == 0
        websocket.send_json.assert_awaited_once()
        payload = websocket.send_json.await_args.args[0]
        assert payload["type"] == "error"
        assert "matching chat request" in payload["content"].lower()

        message_count = await db.scalar(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id == session_id)
        )
        assert message_count == 0
        manager._recent_prompts.pop(session_id, None)
        manager._recent_send_ids.pop(session_id, None)


@pytest.mark.asyncio
async def test_websocket_disconnect_does_not_rollback_completed_response(client):
    await client.post("/auth/register", json={
        "username": "chatpersistuser",
        "email": "chatpersist@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatpersistuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Persist On Disconnect"}, headers=headers)
    session_id = create.json()["id"]

    async def fake_stream_agent(*args, **kwargs):
        yield "response survives disconnect"

    async with TestSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "chatpersistuser"))
        ).scalar_one()
        session = (
            await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one()

        manager = get_chat_run_manager()
        await manager.clear(session_id)
        manager._recent_prompts.pop(session_id, None)
        manager._recent_send_ids.pop(session_id, None)

        websocket = AsyncMock()
        websocket.send_json.side_effect = RuntimeError("socket already closed")

        with (
            patch("app.api.chat.stream_agent", new=fake_stream_agent),
            patch("app.api.chat.classify_chat_complexity", return_value=SimpleNamespace(is_complex=False)),
        ):
            await _run_websocket_message(
                session_id=session_id,
                websocket=websocket,
                db=db,
                current_user=user,
                session=session,
                user_message="tell me something simple",
                client_send_id="disconnect-persist-1",
                allowed_tools=None,
                blocked_tools=None,
            )

        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id)
            )
        ).scalars().all()

        assert [row.role for row in rows] == ["user", "assistant"]
        assert rows[-1].content == "response survives disconnect"


@pytest.mark.asyncio
async def test_websocket_promotes_native_draft_without_retransmitting_tokens(client):
    await client.post("/auth/register", json={
        "username": "chatdraftstreamuser",
        "email": "chatdraftstream@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatdraftstreamuser", "password": "pass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    create = await client.post("/chat/sessions", json={"title": "Draft Stream"}, headers=headers)
    session_id = create.json()["id"]

    async def fake_stream_agent(*args, **kwargs):
        callback = kwargs["provisional_text_callback"]
        await callback("delta", "Visible ")
        await callback("delta", "answer")
        await callback("commit", "")
        yield "Visible "
        yield "answer"

    async with TestSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "chatdraftstreamuser"))
        ).scalar_one()
        session = (
            await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one()

        manager = get_chat_run_manager()
        await manager.clear(session_id)
        manager._recent_prompts.pop(session_id, None)
        manager._recent_send_ids.pop(session_id, None)
        websocket = AsyncMock()

        with (
            patch("app.api.chat.stream_agent", new=fake_stream_agent),
            patch("app.api.chat.classify_chat_complexity", return_value=SimpleNamespace(is_complex=False)),
        ):
            await _run_websocket_message(
                session_id=session_id,
                websocket=websocket,
                db=db,
                current_user=user,
                session=session,
                user_message="tell me something simple",
                client_send_id="draft-stream-1",
                allowed_tools=None,
                blocked_tools=None,
            )

        payloads = [call.args[0] for call in websocket.send_json.await_args_list]
        assert [payload["type"] for payload in payloads].count("draft_token") == 2
        assert any(payload["type"] == "draft_commit" for payload in payloads)
        assert not any(payload["type"] == "token" for payload in payloads)
        done_payload = next(payload for payload in payloads if payload["type"] == "done")
        assert done_payload["content"] == "Visible answer"


@pytest.mark.asyncio
async def test_websocket_done_payload_includes_message_id_for_task_drafts(client):
    await client.post("/auth/register", json={
        "username": "chatdraftwsuser",
        "email": "chatdraftws@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatdraftwsuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "WS Draft Message ID"}, headers=headers)
    session_id = create.json()["id"]
    draft_payload = {
        "proposed": True,
        "title": "Prepare shipping summary",
        "instruction": "Create a follow-up task for the shipping summary.",
        "persona": "family_assistant",
        "task_type": "one_shot",
        "deliver": True,
        "requires_approval": True,
    }

    async def fake_stream_agent(*args, **kwargs):
        yield "Prepared task draft"

    async with TestSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "chatdraftwsuser"))
        ).scalar_one()
        session = (
            await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one()

        manager = get_chat_run_manager()
        await manager.clear(session_id)
        manager._recent_prompts.pop(session_id, None)
        manager._recent_send_ids.pop(session_id, None)

        websocket = AsyncMock()
        with (
            patch("app.api.chat.stream_agent", new=fake_stream_agent),
            patch("app.api.chat.classify_chat_complexity", return_value=SimpleNamespace(is_complex=False)),
            patch("app.api.chat.get_task_handoff_payload", return_value={"task_draft": draft_payload}),
            patch(
                "app.api.chat.get_tool_execution_records",
                return_value=[{"tool": "propose_task_draft", "arguments": {}, "result_summary": "ok"}],
            ),
        ):
            await _run_websocket_message(
                session_id=session_id,
                websocket=websocket,
                db=db,
                current_user=user,
                session=session,
                user_message="Make me a task draft for the shipping summary.",
                client_send_id="draft-message-id-1",
                allowed_tools=None,
                blocked_tools=None,
            )

        payloads = [call.args[0] for call in websocket.send_json.await_args_list]
        done_payload = next(payload for payload in payloads if payload["type"] == "done")
        assert isinstance(done_payload["message_id"], int)
        assert done_payload["metadata"]["task_draft_status"] == "draft"


@pytest.mark.asyncio
async def test_rest_assistant_metadata_includes_structured_evidence(client):
    await client.post("/auth/register", json={
        "username": "evidencerestuser",
        "email": "evidencerest@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "evidencerestuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "REST Evidence"}, headers=headers)
    session_id = create.json()["id"]

    with (
        patch("app.api.chat._execute_chat_turn", new=AsyncMock(return_value="Grounded answer from library tools.")),
        patch("app.api.chat._apply_required_library_grounding", new=AsyncMock(side_effect=lambda history, *args, **kwargs: history)),
        patch(
            "app.api.chat.get_tool_execution_records",
            return_value=[
                {"tool": "search_library", "arguments": {"query": "Agents of Chaos"}, "result_summary": "match"},
                {"tool": "summarize_document", "arguments": {"document_name": "Agents of Chaos.pdf"}, "result_summary": "summary"},
            ],
        ),
    ):
        sent = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "Summarize the library document for me."},
            headers=headers,
        )

    assert sent.status_code == 200
    metadata = sent.json()["metadata"]
    assert metadata["evidence"]["grounded"] is True
    assert metadata["evidence"]["tool_names"] == ["search_library", "summarize_document"]
    assert metadata["evidence"]["source_kinds"] == ["library"]
    assert metadata["evidence"]["source_counts"]["library"] == 2
    assert metadata["evidence"]["tool_details"] == [
        {
            "tool_name": "search_library",
            "detail_kind": "query",
            "label": "Query",
            "value": "Agents of Chaos",
        },
        {
            "tool_name": "summarize_document",
            "detail_kind": "document",
            "label": "Document",
            "value": "Agents of Chaos.pdf",
        },
    ]

    refreshed = await client.get(f"/chat/sessions/{session_id}", headers=headers)
    assistant = refreshed.json()["messages"][-1]
    assert assistant["metadata"]["evidence"]["tool_names"] == ["search_library", "summarize_document"]
    assert assistant["metadata"]["evidence"]["source_counts"]["library"] == 2
    assert assistant["metadata"]["evidence"]["tool_details"][0]["value"] == "Agents of Chaos"


@pytest.mark.asyncio
async def test_websocket_emits_live_state_events_for_tool_backed_turn(client):
    await client.post("/auth/register", json={
        "username": "chatstatewsuser",
        "email": "chatstatews@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatstatewsuser", "password": "pass123"})

    create = await client.post(
        "/chat/sessions",
        json={"title": "WS State Events"},
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    session_id = create.json()["id"]
    runtime_messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "private streamed reasoning must not persist",
            "tool_calls": [
                {
                    "id": "call_lib_1",
                    "type": "function",
                    "function": {"name": "summarize_document", "arguments": '{"document_name":"Agents of Chaos.pdf"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_lib_1",
            "content": "Saved summary",
        },
    ]

    async def _tool_backed_execute(*args, **kwargs):
        pre_tool_callback = kwargs["pre_tool_callback"]
        callback = kwargs["runtime_message_callback"]
        await pre_tool_callback(runtime_messages[0]["tool_calls"])
        await callback(runtime_messages)
        return "Grounded answer from tool output."

    async with TestSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "chatstatewsuser"))
        ).scalar_one()
        session = (
            await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one()

        manager = get_chat_run_manager()
        await manager.clear(session_id)
        manager._recent_prompts.pop(session_id, None)
        manager._recent_send_ids.pop(session_id, None)

        websocket = AsyncMock()
        with (
            patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=_tool_backed_execute)),
            patch("app.api.chat.classify_chat_complexity", return_value=SimpleNamespace(is_complex=False)),
            patch("app.api.chat._apply_required_library_grounding", new=AsyncMock(side_effect=lambda history, *args, **kwargs: history)),
            patch(
                "app.api.chat.get_tool_execution_records",
                return_value=[{"tool": "summarize_document", "arguments": {"document_name": "Agents of Chaos.pdf"}, "result_summary": "Saved summary"}],
            ),
        ):
            await _run_websocket_message(
                session_id=session_id,
                websocket=websocket,
                db=db,
                current_user=user,
                session=session,
                user_message="Summarize the document in my library.",
                client_send_id="ws-state-1",
                allowed_tools=None,
                blocked_tools=None,
            )

        payloads = [call.args[0] for call in websocket.send_json.await_args_list]
        state_payloads = [payload for payload in payloads if payload["type"] == "state"]
        assert [payload["state"] for payload in state_payloads] == [
            "thinking",
            "tool_active",
            "tool_completed",
            "completed",
        ]
        assert state_payloads[1]["tool_names"] == ["summarize_document"]
        done_payload = next(payload for payload in payloads if payload["type"] == "done")
        assert done_payload["metadata"]["evidence"]["source_kinds"] == ["library"]
        assert done_payload["metadata"]["evidence"]["tool_details"] == [
            {
                "tool_name": "summarize_document",
                "detail_kind": "document",
                "label": "Document",
                "value": "Agents of Chaos.pdf",
            }
        ]


def test_build_assistant_tool_details_includes_web_query_and_page_url():
    from app.api.chat import _build_assistant_tool_details

    details = _build_assistant_tool_details(
        [
            {
                "tool": "web_context",
                "arguments": {"query": "Qwen 3.8 changes", "depth": "deep"},
                "result_summary": "context",
            },
            {"tool": "web_search", "arguments": {"query": "NASA Swift reboot"}, "result_summary": "result"},
            {
                "tool": "fetch_page",
                "arguments": {"url": "https://www.nasa.gov/swift"},
                "result_summary": "Title: Swift Mission Overview\nPage content from https://www.nasa.gov/swift:\n\n...",
            },
        ]
    )

    assert details == [
        {
            "tool_name": "web_context",
            "detail_kind": "query",
            "label": "Query",
            "value": "Qwen 3.8 changes",
        },
        {
            "tool_name": "web_search",
            "detail_kind": "query",
            "label": "Query",
            "value": "NASA Swift reboot",
        },
        {
            "tool_name": "fetch_page",
            "detail_kind": "url",
            "label": "Page",
            "value": "https://www.nasa.gov/swift",
            "source_kind": "web",
            "source_title": "Swift Mission Overview",
        },
    ]


def test_build_assistant_content_blocks_extracts_table_and_chart_hint():
    from app.api.chat import _build_assistant_content_blocks

    content = """Benchmark comparison:

| Benchmark | Qwen 3.6 | Qwen 3.8 | Delta |
|---|---:|---:|---:|
| DeepSWE 1.1 | 13.3 | **42.2** | +28.9 |
| SWE-bench Pro | 53.5 | 61.7 | +8.2 |

The newer model improves most strongly on agentic coding.
"""

    blocks = _build_assistant_content_blocks(content)

    assert len(blocks) == 1
    block = blocks[0]
    assert block["schema_version"] == 1
    assert block["id"] == "table_1"
    assert block["type"] == "table"
    assert block["source_markdown"].startswith("| Benchmark")
    assert len(block["source_fingerprint"]) == 16
    assert block["columns"] == ["Benchmark", "Qwen 3.6", "Qwen 3.8", "Delta"]
    assert block["column_alignments"] == ["left", "right", "right", "right"]
    assert block["rows"] == [
        ["DeepSWE 1.1", "13.3", "**42.2**", "+28.9"],
        ["SWE-bench Pro", "53.5", "61.7", "+8.2"],
    ]
    assert block["chart"] == {
        "kind": "bar",
        "category_column": 0,
        "value_columns": [1, 2, 3],
    }


def test_build_assistant_content_blocks_captures_nearby_table_heading():
    from app.api.chat import _build_assistant_content_blocks

    blocks = _build_assistant_content_blocks(
        "### Model Results\n\n| Model | Score |\n|:---|---:|\n| Local | 92 |\n| Cloud | 95 |"
    )

    assert blocks[0]["title"] == "Model Results"
    assert blocks[0]["source_markdown"].startswith("### Model Results\n\n| Model")
    assert blocks[0]["column_alignments"] == ["left", "right"]


def test_build_assistant_content_blocks_derives_title_from_nearby_intro():
    from app.api.chat import _build_assistant_content_blocks

    content = """Here are ten highly rated restaurants in Savannah:

---

| # | Restaurant | Rating |
|---:|---|---:|
| 1 | Mrs. Wilkes' Dining Room | 4.6 |
| 2 | The Olde Pink House | 4.5 |"""
    blocks = _build_assistant_content_blocks(content)

    assert blocks[0]["title"] == "Ten highly rated restaurants in Savannah"
    assert blocks[0]["source_markdown"].startswith("| # | Restaurant")


def test_build_assistant_content_blocks_does_not_use_list_item_as_table_title():
    from app.api.chat import _build_assistant_content_blocks

    content = """- Compare these results carefully

| Model | Score |
|---|---:|
| Local | 92 |
| Cloud | 95 |"""

    assert "title" not in _build_assistant_content_blocks(content)[0]


def test_build_assistant_content_blocks_caps_native_tables_at_eight():
    from app.api.chat import _build_assistant_content_blocks

    content = "\n\n".join(
        f"| Table {index} | Value |\n|---|---:|\n| Row | {index} |\n| Other | {index + 1} |"
        for index in range(1, 10)
    )

    blocks = _build_assistant_content_blocks(content)

    assert len(blocks) == 8
    assert [block["id"] for block in blocks] == [f"table_{index}" for index in range(1, 9)]
    assert blocks[-1]["columns"] == ["Table 8", "Value"]


def test_build_assistant_content_blocks_extracts_rss_news_digest():
    from app.api.chat import _build_assistant_content_blocks

    content = """Here are the strongest stories from your feeds:

### Election Updates
* **Spain Calls Snap Election Amid Housing Crisis**
  Spain's prime minister called an early election after a legislative defeat.
  [NPR](https://example.com/spain) | [BBC](https://example.com/spain-bbc)
* **Brazil Presidential Race Heads to Run-Off**
  No candidate crossed 50 percent in the first round.
  [BBC](https://example.com/brazil)

### US & National Policy
* **Georgia Voting Forum Draws Local Officials**
  Election directors answered voter questions at a regional forum.
  [WRBL](https://example.com/georgia)
"""

    blocks = _build_assistant_content_blocks(
        content,
        [{"tool": "list_recent_feed_items", "arguments": {}}],
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "news_digest"
    assert blocks[0]["schema_version"] == 1
    assert len(blocks[0]["source_fingerprint"]) == 16
    assert blocks[0]["title"] == "News Briefing"
    assert [section["title"] for section in blocks[0]["sections"]] == [
        "Election Updates",
        "US & National Policy",
    ]
    assert blocks[0]["sections"][0]["items"][0]["sources"] == [
        {"label": "NPR", "url": "https://example.com/spain"},
        {"label": "BBC", "url": "https://example.com/spain-bbc"},
    ]


def test_build_assistant_content_blocks_accepts_rss_headline_suffix_and_bare_link():
    from app.api.chat import _build_assistant_content_blocks

    content = """Here are recent headlines:

### Foreign Policy
- **First headline** *(Al Jazeera, Oct 7)*
  First grounded summary.
  🔗 https://example.com/first

- **Second headline** *(BBC, Oct 6)*
  Second grounded summary.
  🔗 https://example.com/second

Let me know if you want more.
"""

    blocks = _build_assistant_content_blocks(
        content,
        [{"tool": "search_my_feeds", "arguments": {"query": "topic"}}],
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "news_digest"
    items = blocks[0]["sections"][0]["items"]
    assert items == [
        {
            "title": "First headline",
            "summary": "First grounded summary.",
            "sources": [{"label": "Al Jazeera", "url": "https://example.com/first"}],
        },
        {
            "title": "Second headline",
            "summary": "Second grounded summary.",
            "sources": [{"label": "BBC", "url": "https://example.com/second"}],
        },
    ]


def test_build_assistant_content_blocks_falls_back_to_selected_rss_evidence():
    from app.api.chat import _build_assistant_content_blocks

    content = """Here are the major headlines:

**International & Politics**
- **First headline**; a short inline explanation
  https://example.com/first

**Business**
- Second headline without bold formatting
  https://example.com/second

---

Ask if you want a deeper dive.
"""
    tool_result = """Recent feed items (2):

[1] First canonical headline
    Source: NPR
    Summary: First summary from the feed.
    URL: https://example.com/first?traffic_source=rss

[2] Second canonical headline
    Source: BBC
    Summary: Second summary from the feed.
    URL: https://example.com/second
"""

    blocks = _build_assistant_content_blocks(
        content,
        [{"tool": "list_recent_feed_items", "arguments": {}, "result_summary": tool_result}],
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "news_digest"
    assert blocks[0]["sections"] == [
        {
            "title": "Selected Headlines",
            "items": [
                {
                    "title": "First canonical headline",
                    "summary": "First summary from the feed.",
                    "sources": [
                        {
                            "label": "NPR",
                            "url": "https://example.com/first?traffic_source=rss",
                        }
                    ],
                },
                {
                    "title": "Second canonical headline",
                    "summary": "Second summary from the feed.",
                    "sources": [{"label": "BBC", "url": "https://example.com/second"}],
                },
            ],
        }
    ]
    assert blocks[0]["source_markdown"].startswith("**International & Politics**")
    assert blocks[0]["source_markdown"].endswith("---")


def test_build_assistant_content_blocks_does_not_promote_news_without_rss_tool():
    from app.api.chat import _build_assistant_content_blocks

    content = """### News
* **First story**
  First summary.
  [Source](https://example.com/one)
* **Second story**
  Second summary.
  [Source](https://example.com/two)
"""

    assert _build_assistant_content_blocks(content) == []


def test_build_assistant_content_blocks_extracts_bounded_stat_group():
    from app.api.chat import _build_assistant_content_blocks

    content = """Current conditions are stable.

### System Health
- **Status:** Healthy
- **Active tasks:** 12
- **Queue depth:** 3
- **Last check:** 2 minutes ago

No intervention is required.
"""

    blocks = _build_assistant_content_blocks(content)

    assert blocks == [
        {
            "schema_version": 1,
            "id": "stat_group_1",
            "type": "stat_group",
            "source_markdown": """### System Health
- **Status:** Healthy
- **Active tasks:** 12
- **Queue depth:** 3
- **Last check:** 2 minutes ago""",
            "source_fingerprint": blocks[0]["source_fingerprint"],
            "title": "System Health",
            "items": [
                {"label": "Status", "value": "Healthy"},
                {"label": "Active tasks", "value": "12"},
                {"label": "Queue depth", "value": "3"},
                {"label": "Last check", "value": "2 minutes ago"},
            ],
        }
    ]


def test_build_assistant_content_blocks_leaves_short_fact_list_as_prose():
    from app.api.chat import _build_assistant_content_blocks

    content = """### Result
- **Status:** Healthy
- **Queue depth:** 3
"""

    assert _build_assistant_content_blocks(content) == []


def test_build_assistant_content_blocks_leaves_place_shaped_stats_as_prose_without_place_evidence():
    from app.api.chat import _build_assistant_content_blocks

    content = (
        "### 1. **Three Tree Coffee Roasters** ⭐ 4.5\n"
        "- **Address:** 441 S Main St, Statesboro, GA 30458\n"
        "- **Distance from Downtown:** ~0.2 mi south\n"
        "- **Phone:** (912) 681-8733\n"
        "- **Website:** <https://threetreecoffee.com/>"
    )

    assert _build_assistant_content_blocks(content) == []


def test_build_assistant_content_blocks_extracts_bounded_code_artifact():
    from app.api.chat import _build_assistant_content_blocks

    content = """Use this helper to normalize the value.

### normalize.py
```python
def normalize(value: str) -> str:
    return " ".join(value.split()).strip()
```

It intentionally preserves no surrounding whitespace.
"""

    blocks = _build_assistant_content_blocks(content)

    assert len(blocks) == 1
    assert blocks[0]["type"] == "code_artifact"
    assert blocks[0]["title"] == "normalize.py"
    assert blocks[0]["code"] == {
        "language": "python",
        "filename": "normalize.py",
        "content": 'def normalize(value: str) -> str:\n    return " ".join(value.split()).strip()',
    }


def test_build_assistant_content_blocks_links_code_to_tool_backed_workspace_file():
    from app.api.chat import _build_assistant_content_blocks

    content = """### scripts/check.py
```python
def check() -> bool:
    return True
```
"""
    blocks = _build_assistant_content_blocks(
        content,
        [
            {
                "tool": "write_file",
                "arguments": {"path": "/workspace/1/scripts/check.py"},
                "result_summary": "Workspace file written",
                "is_error": False,
            }
        ],
    )

    assert blocks[0]["code"]["path"] == "scripts/check.py"
    assert blocks[0]["code"]["filename"] == "check.py"


def test_build_assistant_content_blocks_does_not_promote_short_inline_command():
    from app.api.chat import _build_assistant_content_blocks

    assert _build_assistant_content_blocks("Run:\n```bash\npytest -q\n```") == []


def test_code_artifact_supersedes_markdown_table_inside_fence():
    from app.api.chat import _build_assistant_content_blocks

    content = """### Markdown example
```markdown
| Name | Value |
|---|---:|
| Alpha | 1 |
| Beta | 2 |
```
"""
    blocks = _build_assistant_content_blocks(content)

    assert [block["type"] for block in blocks] == ["code_artifact"]


def test_normalize_assistant_metadata_preserves_bounded_code_artifact():
    from app.api.chat import _normalize_assistant_metadata_payload

    metadata = _normalize_assistant_metadata_payload(
        {
            "content_blocks": [
                {
                    "type": "code_artifact",
                    "source_markdown": "```swift\nlet value = 1\nprint(value)\n```",
                    "title": "Example.swift",
                    "code": {
                        "language": "swift",
                        "filename": "Example.swift",
                        "content": "let value = 1\nprint(value)",
                        "path": "/workspace/1/Sources/Example.swift",
                    },
                }
            ]
        }
    )

    block = metadata["content_blocks"][0]
    assert block["type"] == "code_artifact"
    assert block["code"]["path"] == "Sources/Example.swift"
    assert block["code"]["language"] == "swift"


def test_normalize_assistant_metadata_preserves_bounded_stat_group():
    from app.api.chat import _normalize_assistant_metadata_payload

    metadata = _normalize_assistant_metadata_payload(
        {
            "content_blocks": [
                {
                    "id": "stat_group_1",
                    "type": "stat_group",
                    "source_markdown": "### Quote\n- **Price:** $42\n- **Change:** +1.2%\n- **Status:** Open",
                    "title": "Quote",
                    "items": [
                        {"label": "Price", "value": "$42"},
                        {"label": "Change", "value": "+1.2%"},
                        {"label": "Status", "value": "Open"},
                    ],
                }
            ]
        }
    )

    normalized = metadata["content_blocks"][0]
    assert normalized["schema_version"] == 1
    assert normalized["type"] == "stat_group"
    assert normalized["items"][1] == {"label": "Change", "value": "+1.2%"}


def test_build_assistant_content_blocks_extracts_explicit_timeline():
    from app.api.chat import _build_assistant_content_blocks

    content = """The incident unfolded in three stages.

### Incident Timeline
- **09:15 AM:** Monitoring detected elevated error rates.
- **09:28 AM** — Operators disabled the affected integration.
- **10:05 AM** Service recovered and validation completed.

No data was lost.
"""

    blocks = _build_assistant_content_blocks(content)

    assert len(blocks) == 1
    assert blocks[0]["type"] == "timeline"
    assert blocks[0]["title"] == "Incident Timeline"
    assert blocks[0]["events"] == [
        {"label": "09:15 AM", "detail": "Monitoring detected elevated error rates."},
        {"label": "09:28 AM", "detail": "Operators disabled the affected integration."},
        {"label": "10:05 AM", "detail": "Service recovered and validation completed."},
    ]


def test_timeline_preserves_event_and_research_source_links():
    from app.api.chat import _build_assistant_content_blocks

    content = """### Merger Timeline
- **June 9:** WBD announced its split in a [company release](https://example.com/release).
- **September 12:** Reporting described the first bid.
"""
    tools = [
        {
            "tool": "web_search",
            "arguments": {"query": "WBD Paramount merger timeline"},
            "structured_content": {
                "sources": [
                    {"title": "Deal reporting", "url": "https://example.com/report"},
                    {"title": "Company release", "url": "https://example.com/release"},
                ]
            },
        }
    ]

    blocks = _build_assistant_content_blocks(content, tools)

    assert blocks[0]["events"][0]["detail"] == "WBD announced its split in a company release."
    assert blocks[0]["events"][0]["sources"] == [
        {"label": "company release", "url": "https://example.com/release"}
    ]
    assert blocks[0]["sources"] == [
        {"label": "Deal reporting", "url": "https://example.com/report"},
        {"label": "Company release", "url": "https://example.com/release"},
    ]


def test_build_assistant_content_blocks_does_not_promote_generic_bold_list_to_timeline():
    from app.api.chat import _build_assistant_content_blocks

    content = """### Recommendations
- **First:** Check the logs.
- **Second:** Restart the service.
- **Third:** Verify recovery.
"""

    blocks = _build_assistant_content_blocks(content)

    assert all(block["type"] != "timeline" for block in blocks)


def test_normalize_assistant_metadata_preserves_bounded_timeline():
    from app.api.chat import _normalize_assistant_metadata_payload

    metadata = _normalize_assistant_metadata_payload(
        {
            "content_blocks": [
                {
                    "id": "timeline_1",
                    "type": "timeline",
                    "source_markdown": "### Timeline\n- **Day 1:** Started\n- **Day 2:** Finished",
                    "title": "Timeline",
                    "events": [
                        {
                            "label": "Day 1",
                            "detail": "Started",
                            "sources": [{"label": "Launch", "url": "https://example.com/start"}],
                        },
                        {"label": "Day 2", "detail": "Finished"},
                    ],
                    "sources": [{"label": "Overview", "url": "https://example.com/overview"}],
                }
            ]
        }
    )

    block = metadata["content_blocks"][0]
    assert block["schema_version"] == 1
    assert block["events"] == [
        {
            "label": "Day 1",
            "detail": "Started",
            "sources": [{"label": "Launch", "url": "https://example.com/start"}],
        },
        {"label": "Day 2", "detail": "Finished"},
    ]
    assert block["sources"] == [
        {"label": "Overview", "url": "https://example.com/overview"}
    ]


def test_build_assistant_content_blocks_extracts_tool_backed_file_artifact():
    from app.api.chat import _build_assistant_content_blocks

    content = "The report is ready at `reports/weekly-summary.md`."
    blocks = _build_assistant_content_blocks(
        content,
        [
            {
                "tool": "write_file",
                "arguments": {"path": "reports/weekly-summary.md", "content": "private"},
                "result_summary": "Wrote 7 bytes to weekly-summary.md",
                "is_error": False,
            }
        ],
    )

    assert blocks == [
        {
            "schema_version": 1,
            "id": "file_artifact_1",
            "type": "file_artifact",
            "source_markdown": content,
            "source_fingerprint": blocks[0]["source_fingerprint"],
            "title": "weekly-summary.md",
            "file": {
                "path": "reports/weekly-summary.md",
                "filename": "weekly-summary.md",
                "media_type": "text/markdown",
                "operation": "written",
            },
        }
    ]


def test_build_assistant_content_blocks_requires_successful_referenced_file_write():
    from app.api.chat import _build_assistant_content_blocks

    failed = {
        "tool": "write_file",
        "arguments": {"path": "reports/private.md"},
        "is_error": True,
    }
    unreferenced = {
        "tool": "append_file",
        "arguments": {"path": "reports/hidden.md"},
        "is_error": False,
    }

    assert _build_assistant_content_blocks("The write failed.", [failed]) == []
    assert _build_assistant_content_blocks("The report was updated.", [unreferenced]) == []


def test_build_assistant_content_blocks_normalizes_absolute_workspace_artifact_path():
    from app.api.chat import _build_assistant_content_blocks

    blocks = _build_assistant_content_blocks(
        "Saved to `reports/result.csv`.",
        [
            {
                "tool": "write_file",
                "arguments": {"path": "/Users/example/fruitcake/workspace/7/reports/result.csv"},
                "is_error": False,
            }
        ],
    )

    assert blocks[0]["file"]["path"] == "reports/result.csv"


def test_normalize_assistant_metadata_preserves_bounded_file_artifact():
    from app.api.chat import _normalize_assistant_metadata_payload

    metadata = _normalize_assistant_metadata_payload(
        {
            "content_blocks": [
                {
                    "type": "file_artifact",
                    "source_markdown": "Saved to `reports/result.csv`.",
                    "file": {
                        "path": "reports/result.csv",
                        "filename": "result.csv",
                        "media_type": "text/csv",
                        "operation": "appended",
                    },
                }
            ]
        }
    )

    block = metadata["content_blocks"][0]
    assert block["schema_version"] == 1
    assert block["type"] == "file_artifact"
    assert block["file"] == {
        "path": "reports/result.csv",
        "filename": "result.csv",
        "media_type": "text/csv",
        "operation": "appended",
    }


def test_build_assistant_content_blocks_extracts_tool_backed_place_group():
    from app.api.chat import _build_assistant_content_blocks

    content = (
        "### Coffee nearby\n"
        "- **The Daily Grind** — Coffee shop at 17 Main St.\n"
        "- **Three Tree Coffee Roasters** — Cafe at 441 S Main St.\n\n"
        "Both are close to downtown."
    )
    blocks = _build_assistant_content_blocks(
        content,
        [
            {
                "tool": "search_places",
                "arguments": {"query": "coffee", "near": "Statesboro"},
                "is_error": False,
                "structured_content": {
                    "capability": "place_search",
                    "provider": "brave",
                    "places": [
                        {
                            "name": "The Daily Grind",
                            "address": "17 Main St, Statesboro, GA",
                            "latitude": 32.448,
                            "longitude": -81.783,
                            "category": "Coffee shop",
                            "rating": 4.6,
                            "rating_max": 5,
                            "review_count": 82,
                            "url": "https://example.com/daily-grind",
                            "provider": "brave",
                        },
                        {
                            "name": "Three Tree Coffee Roasters",
                            "address": "441 S Main St, Statesboro, GA",
                            "category": "Cafe",
                            "provider": "brave",
                        },
                        {
                            "name": "Unmentioned Cafe",
                            "address": "99 Hidden St",
                            "provider": "brave",
                        },
                    ],
                },
            }
        ],
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "place_group"
    assert blocks[0]["title"] == "Coffee nearby"
    assert blocks[0]["provider"] == "brave"
    assert [place["name"] for place in blocks[0]["places"]] == [
        "The Daily Grind",
        "Three Tree Coffee Roasters",
    ]
    assert blocks[0]["places"][0]["rating"] == 4.6
    assert "Both are close to downtown." not in blocks[0]["source_markdown"]


def test_place_group_supersedes_overlapping_markdown_table():
    from app.api.chat import _build_assistant_content_blocks

    content = (
        "### Nearby places\n"
        "| Name | Address |\n"
        "|---|---|\n"
        "| The Daily Grind | 17 Main St |\n"
        "| Three Tree Coffee | 441 S Main St |"
    )
    blocks = _build_assistant_content_blocks(
        content,
        [
            {
                "tool": "search_places",
                "is_error": False,
                "structured_content": {
                    "capability": "place_search",
                    "provider": "nominatim",
                    "places": [
                        {"name": "The Daily Grind", "address": "17 Main St"},
                        {"name": "Three Tree Coffee", "address": "441 S Main St"},
                    ],
                },
            }
        ],
    )

    assert [block["type"] for block in blocks] == ["place_group"]


def test_normalize_assistant_metadata_preserves_bounded_place_group():
    from app.api.chat import _normalize_assistant_metadata_payload

    metadata = _normalize_assistant_metadata_payload(
        {
            "content_blocks": [
                {
                    "type": "place_group",
                    "source_markdown": "- **Cafe** — 1 Main St",
                    "title": "Nearby",
                    "provider": "brave",
                    "places": [
                        {
                            "name": "Cafe",
                            "address": "1 Main St",
                            "latitude": 32.0,
                            "longitude": -81.0,
                            "rating": 4.5,
                            "review_count": 12,
                            "url": "javascript:alert(1)",
                        }
                    ],
                }
            ]
        }
    )

    block = metadata["content_blocks"][0]
    assert block["schema_version"] == 1
    assert block["type"] == "place_group"
    assert block["places"] == [
        {
            "name": "Cafe",
            "address": "1 Main St",
            "latitude": 32.0,
            "longitude": -81.0,
            "rating": 4.5,
            "review_count": 12,
        }
    ]


def test_normalize_assistant_metadata_preserves_bounded_news_digest():
    from app.api.chat import _normalize_assistant_metadata_payload

    metadata = _normalize_assistant_metadata_payload(
        {
            "content_blocks": [
                {
                    "id": "news_digest_1",
                    "type": "news_digest",
                    "source_markdown": "### News\n* **Story**",
                    "title": "Evening News",
                    "sections": [
                        {
                            "title": "Politics",
                            "items": [
                                {
                                    "title": "First story",
                                    "summary": "A grounded summary.",
                                    "sources": [
                                        {"label": "NPR", "url": "https://example.com/story"},
                                        {"label": "Bad", "url": "javascript:alert(1)"},
                                    ],
                                },
                                {
                                    "title": "Second story",
                                    "summary": "Another grounded summary.",
                                    "sources": [],
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    )

    block = metadata["content_blocks"][0]
    assert block["type"] == "news_digest"
    assert block["schema_version"] == 1
    assert block["title"] == "Evening News"
    assert block["sections"][0]["items"][0]["sources"] == [
        {"label": "NPR", "url": "https://example.com/story"}
    ]


def test_normalize_assistant_metadata_upgrades_legacy_content_block_and_rejects_future_schema():
    from app.api.chat import _normalize_assistant_metadata_payload

    normalized = _normalize_assistant_metadata_payload(
        {
            "content_blocks": [
                {
                    "id": "legacy_table",
                    "type": "table",
                    "source_markdown": "| Name | Value |\n|---|---:|\n| A | 1 |",
                    "columns": ["Name", "Value"],
                    "rows": [["A", "1"]],
                },
                {
                    "schema_version": 99,
                    "id": "future_table",
                    "type": "table",
                    "source_markdown": "| Name | Value |\n|---|---|\n| B | 2 |",
                    "columns": ["Name", "Value"],
                    "rows": [["B", "2"]],
                },
            ]
        }
    )

    assert len(normalized["content_blocks"]) == 1
    assert normalized["content_blocks"][0]["id"] == "legacy_table"
    assert normalized["content_blocks"][0]["schema_version"] == 1
    assert normalized["content_blocks"][0]["column_alignments"] == ["left", "left"]


def test_build_assistant_activity_is_human_readable_and_bounded():
    from app.api.chat import _build_assistant_activity

    activities = _build_assistant_activity(
        [
            {"tool": "web_context", "arguments": {"query": "Qwen 3.8 benchmarks"}},
            {
                "tool": "fetch_page",
                "arguments": {"url": "https://example.com/report"},
                "result_summary": "Title: Benchmark report\nBody",
            },
            {"tool": "search_library", "arguments": {"query": "model notes"}},
        ]
    )

    assert activities == [
        {"tool_name": "web_context", "label": "Searched the web", "value": "Qwen 3.8 benchmarks"},
        {"tool_name": "fetch_page", "label": "Read a webpage", "value": "Benchmark report"},
        {"tool_name": "search_library", "label": "Searched your library", "value": "model notes"},
    ]


def test_assistant_metadata_preserves_content_blocks_and_activity():
    from app.api.chat import _build_assistant_message_metadata

    metadata = _build_assistant_message_metadata(
        handoff_metadata={},
        executed_tools=[{"tool": "web_search", "arguments": {"query": "market data"}}],
        content="| Name | Value |\n|---|---:|\n| A | 1 |\n| B | 2 |",
    )

    assert metadata is not None
    assert metadata["content_blocks"][0]["type"] == "table"
    assert metadata["content_blocks"][0]["chart"]["value_columns"] == [1]
    assert metadata["activity"] == [
        {"tool_name": "web_search", "label": "Searched the web", "value": "market data"}
    ]


def test_build_assistant_tool_details_fetch_page_kind_heuristics():
    from app.api.chat import _build_assistant_tool_details

    details = _build_assistant_tool_details(
        [
            {"tool": "fetch_page", "arguments": {"url": "https://en.wikipedia.org/wiki/Swift"}, "result_summary": "no title line"},
            {"tool": "fetch_page", "arguments": {"url": "https://example.com/whitepaper.pdf"}, "result_summary": "no title line"},
        ]
    )

    assert [d["source_kind"] for d in details] == ["wiki", "pdf"]
    assert all("source_title" not in d for d in details)


def test_build_chat_state_event_includes_image_render_details():
    from app.api.chat import _build_chat_state_event, _build_live_tool_details

    tool_calls = [
        {
            "function": {
                "name": "generate_image",
                "arguments": json.dumps(
                    {
                        "prompt": "A tiny robot baking a cake",
                        "model": "sd3.5-large",
                        "workflow": "fruitcake_lab",
                        "steps": 40,
                        "seed": "123",
                        "width": 1024,
                        "height": 1024,
                        "negative_prompt": "not shown in live state",
                    }
                ),
            }
        }
    ]

    details = _build_live_tool_details(tool_calls)
    payload = _build_chat_state_event(
        "image_rendering",
        tool_names=["generate_image"],
        tool_details=details,
    )

    assert payload["state"] == "image_rendering"
    assert payload["tool_names"] == ["generate_image"]
    assert payload["tool_details"] == [
        {
            "tool_name": "generate_image",
            "arguments": {
                "prompt": "A tiny robot baking a cake",
                "model": "sd3.5-large",
                "workflow": "fruitcake_lab",
                "steps": 40,
                "seed": 123,
                "width": 1024,
                "height": 1024,
            },
        }
    ]


def test_build_live_tool_details_exposes_only_safe_operator_context():
    from app.api.chat import _build_live_tool_details

    tool_calls = [
        {
            "function": {
                "name": "web_context",
                "arguments": json.dumps(
                    {"query": "Qwen 3.8 benchmark changes", "depth": "deep", "api_key": "secret"}
                ),
            }
        },
        {
            "function": {
                "name": "web_search",
                "arguments": json.dumps({"query": "reflecting pool Washington DC", "api_key": "secret"}),
            }
        },
        {
            "function": {
                "name": "fetch_page",
                "arguments": json.dumps(
                    {"url": "https://example.com/article?token=secret#private", "headers": {"Authorization": "secret"}}
                ),
            }
        },
        {
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": "reports/repo_map.md", "token": "secret"}),
            }
        },
    ]

    assert _build_live_tool_details(tool_calls) == [
        {
            "tool_name": "web_context",
            "arguments": {"query": "Qwen 3.8 benchmark changes", "depth": "deep"},
        },
        {"tool_name": "web_search", "arguments": {"query": "reflecting pool Washington DC"}},
        {"tool_name": "fetch_page", "arguments": {"url": "https://example.com/article"}},
        {"tool_name": "read_file", "arguments": {"path": "reports/repo_map.md"}},
    ]


def test_build_assistant_evidence_metadata_counts_repeated_web_sources():
    from app.api.chat import _build_assistant_evidence_metadata

    evidence = _build_assistant_evidence_metadata(
        [
            {"tool": "web_search", "arguments": {"query": "swift observatory reboot"}, "result_summary": "search"},
            {
                "tool": "fetch_page",
                "arguments": {"url": "https://www.nasa.gov/swift"},
                "result_summary": "Title: Swift Mission Overview\nPage content from https://www.nasa.gov/swift:\n\n...",
            },
            {
                "tool": "fetch_page",
                "arguments": {"url": "https://en.wikipedia.org/wiki/Neil_Gehrels_Swift_Observatory"},
                "result_summary": "Title: Neil Gehrels Swift Observatory\nPage content from https://en.wikipedia.org/wiki/Neil_Gehrels_Swift_Observatory:\n\n...",
            },
        ]
    )

    assert evidence is not None
    assert evidence["tool_names"] == ["web_search", "fetch_page"]
    assert evidence["source_kinds"] == ["web"]
    assert evidence["source_counts"] == {"web": 3}


def test_build_assistant_evidence_metadata_extracts_bounded_rss_story_sources():
    from app.api.chat import _build_assistant_evidence_metadata

    evidence = _build_assistant_evidence_metadata(
        [
            {
                "tool": "search_my_feeds",
                "arguments": {"query": "space missions"},
                "result_summary": """Cached feed results for 'space missions' (cache-only):

[1] Swift Observatory gets a boost
    Feed: NASA News
    Published: 2026-10-05T12:00:00Z
    URL: https://www.nasa.gov/swift-update
    Mission update.

[2] A second mission
    Feed: Space News
    URL: https://example.org/mission
""",
            }
        ]
    )

    assert evidence is not None
    assert evidence["tool_details"] == [
        {
            "tool_name": "search_my_feeds",
            "detail_kind": "query",
            "label": "Query",
            "value": "space missions",
        }
    ]
    assert evidence["citations"] == [
        {
            "url": "https://www.nasa.gov/swift-update",
            "title": "Swift Observatory gets a boost",
            "source": "NASA News",
            "published_at": "2026-10-05T12:00:00Z",
        },
        {
            "url": "https://example.org/mission",
            "title": "A second mission",
            "source": "Space News",
        },
    ]


def test_build_assistant_evidence_metadata_preserves_web_context_provider_and_sources():
    from app.api.chat import _build_assistant_evidence_metadata

    evidence = _build_assistant_evidence_metadata(
        [
            {
                "tool": "web_context",
                "arguments": {"query": "Qwen 3.8 benchmark changes"},
                "result_summary": "Web context for: Qwen 3.8 benchmark changes",
                "structured_content": {
                    "provider": "brave",
                    "citations": [
                        {
                            "title": "Qwen 3.8 model card",
                            "url": "https://huggingface.co/Qwen/Qwen3.8-27B",
                            "source": "brave",
                        }
                    ],
                },
            }
        ]
    )

    assert evidence is not None
    assert evidence["tool_details"] == [
        {
            "tool_name": "web_context",
            "detail_kind": "query",
            "label": "Query",
            "value": "Qwen 3.8 benchmark changes",
        },
        {
            "tool_name": "web_context",
            "detail_kind": "provider",
            "label": "Provider",
            "value": "brave",
        },
    ]
    assert evidence["citations"] == [
        {
            "title": "Qwen 3.8 model card",
            "url": "https://huggingface.co/Qwen/Qwen3.8-27B",
            "source": "brave",
        }
    ]


def test_build_assistant_evidence_metadata_includes_generated_image_artifact():
    from app.api.chat import _build_assistant_evidence_metadata

    evidence = _build_assistant_evidence_metadata(
        [
            {
                "tool": "generate_image",
                "arguments": {"prompt": "A tiny robot baking a cake", "workflow": "sdxl_basic"},
                "result_summary": json.dumps(
                    {
                        "image_path": "generated_images/robot.png",
                        "prompt": "A tiny robot baking a cake",
                        "workflow": "sdxl_basic",
                        "seed": 123,
                        "width": 1024,
                        "height": 1024,
                    }
                ),
            }
        ]
    )

    assert evidence is not None
    assert evidence["source_kinds"] == ["image"]
    assert evidence["source_counts"] == {"image": 1}
    assert evidence["image_artifacts"] == [
        {
            "path": "generated_images/robot.png",
            "source_tool": "generate_image",
            "prompt": "A tiny robot baking a cake",
            "title": "A tiny robot baking a cake",
            "workflow": "sdxl_basic",
            "seed": 123,
            "width": 1024,
            "height": 1024,
        }
    ]


def test_build_assistant_evidence_metadata_prefers_normalized_artifacts_and_citations():
    from app.api.chat import _build_assistant_evidence_metadata

    evidence = _build_assistant_evidence_metadata(
        [
            {
                "tool": "generate_image",
                "arguments": {"prompt": "A navigation diagram"},
                "result_summary": "Image generated.",
                "structured_content": {"image_path": "generated_images/navigation.png"},
                "artifacts": [
                    {
                        "kind": "image",
                        "path": "generated_images/navigation.png",
                        "prompt": "A navigation diagram",
                        "source_tool": "generate_image",
                    }
                ],
                "citations": [
                    {"url": "https://example.com/reference", "title": "Reference"}
                ],
            }
        ]
    )

    assert evidence is not None
    assert evidence["image_artifacts"][0]["path"] == "generated_images/navigation.png"
    assert evidence["image_artifacts"][0]["title"] == "A navigation diagram"
    assert evidence["citations"] == [
        {"url": "https://example.com/reference", "title": "Reference"}
    ]


def test_generated_image_reference_normalization_preserves_inline_placement():
    from app.api.chat import _ensure_generated_image_markdown_references

    records = [
        {
            "tool": "generate_image",
            "arguments": {"prompt": "A layered diagram"},
            "result_summary": json.dumps({"image_path": "generated_images/layers.png"}),
        }
    ]
    content = "First concept.\n\n![Layered diagram](generated_images/layers.png)\n\nSecond concept."

    assert _ensure_generated_image_markdown_references(content, records) == content


def test_generated_image_reference_normalization_appends_only_missing_artifacts():
    from app.api.chat import _ensure_generated_image_markdown_references

    records = [
        {
            "tool": "generate_image",
            "arguments": {"prompt": "First diagram"},
            "result_summary": json.dumps({"image_path": "generated_images/first.png"}),
        },
        {
            "tool": "generate_image",
            "arguments": {"prompt": "Second diagram"},
            "result_summary": json.dumps({"image_path": "generated_images/second.png"}),
        },
    ]
    content = "First concept.\n\n![First diagram](/workspace/images?path=generated_images%2Ffirst.png)"

    normalized = _ensure_generated_image_markdown_references(content, records)

    assert normalized.count("first.png") == 1
    assert normalized.endswith("![Second diagram](generated_images/second.png)")


def test_build_assistant_evidence_metadata_includes_described_image_detail():
    from app.api.chat import _build_assistant_evidence_metadata

    evidence = _build_assistant_evidence_metadata(
        [
            {
                "tool": "describe_image",
                "arguments": {
                    "path": "generated_images/robot.png",
                    "question": "What is visible?",
                },
                "result_summary": "Image inspected: generated_images/robot.png\n\nA tiny robot is baking.",
            }
        ]
    )

    assert evidence is not None
    assert evidence["source_kinds"] == ["image"]
    assert evidence["source_counts"] == {"image": 1}
    assert evidence["tool_details"] == [
        {
            "tool_name": "describe_image",
            "detail_kind": "image",
            "label": "Image",
            "value": "generated_images/robot.png",
        },
        {
            "tool_name": "describe_image",
            "detail_kind": "question",
            "label": "Question",
            "value": "What is visible?",
        },
    ]


def test_normalize_assistant_metadata_payload_passes_through_source_title_and_kind():
    from app.api.chat import _normalize_assistant_metadata_payload

    normalized = _normalize_assistant_metadata_payload(
        {
            "tool_calls": ["fetch_page"],
            "evidence": {
                "grounded": True,
                "tool_details": [
                    {
                        "tool_name": "fetch_page",
                        "detail_kind": "url",
                        "label": "Page",
                        "value": "https://www.nasa.gov/swift",
                        "source_kind": "web",
                        "source_title": "Swift Mission Overview",
                    }
                ],
            },
        }
    )

    assert normalized["evidence"]["tool_details"] == [
        {
            "tool_name": "fetch_page",
            "detail_kind": "url",
            "label": "Page",
            "value": "https://www.nasa.gov/swift",
                "source_kind": "web",
                "source_title": "Swift Mission Overview",
            }
        ]


def test_normalize_assistant_metadata_payload_passes_through_image_artifacts():
    from app.api.chat import _normalize_assistant_metadata_payload

    normalized = _normalize_assistant_metadata_payload(
        {
            "tool_calls": ["generate_image"],
            "evidence": {
                "grounded": True,
                "image_artifacts": [
                    {
                        "path": "generated_images/robot.png",
                        "title": "Robot cake",
                        "prompt": "A tiny robot baking a cake",
                        "workflow": "sdxl_basic",
                        "seed": "123",
                        "width": 1024,
                        "height": 1024,
                        "source_tool": "generate_image",
                    }
                ],
            },
        }
    )

    assert normalized["evidence"]["image_artifacts"] == [
        {
            "path": "generated_images/robot.png",
            "title": "Robot cake",
            "prompt": "A tiny robot baking a cake",
            "workflow": "sdxl_basic",
            "seed": 123,
            "width": 1024,
            "height": 1024,
            "source_tool": "generate_image",
        }
    ]


def test_normalize_assistant_metadata_payload_sanitizes_citations_and_keeps_library_refs():
    from app.api.chat import _normalize_assistant_metadata_payload

    normalized = _normalize_assistant_metadata_payload(
        {
            "evidence": {
                "grounded": True,
                "citations": [
                    {"title": "Safe source", "url": "https://example.com/story"},
                    {"title": "Unsafe source", "url": "javascript:alert(1)"},
                    {"document": "Agents of Chaos.pdf", "path": "library/agents-of-chaos.pdf"},
                ],
            }
        }
    )

    assert normalized["evidence"]["citations"] == [
        {"url": "https://example.com/story", "title": "Safe source"},
        {"document": "Agents of Chaos.pdf", "path": "library/agents-of-chaos.pdf"},
    ]


@pytest.mark.asyncio
async def test_workspace_image_endpoint_serves_owned_workspace_image(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    await client.post("/auth/register", json={
        "username": "imageuser",
        "email": "imageuser@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "imageuser", "password": "pass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    image_path = Path(tmp_path) / "1" / "generated_images" / "robot.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    response = await client.get(
        "/workspace/images",
        params={"path": "generated_images/robot.png"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_workspace_image_endpoint_rejects_non_images_and_path_escape(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    await client.post("/auth/register", json={
        "username": "imagenoaccess",
        "email": "imagenoaccess@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "imagenoaccess", "password": "pass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    note_path = Path(tmp_path) / "1" / "generated_images" / "note.txt"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("not an image", encoding="utf-8")

    non_image = await client.get(
        "/workspace/images",
        params={"path": "generated_images/note.txt"},
        headers=headers,
    )
    escaped = await client.get(
        "/workspace/images",
        params={"path": "../2/generated_images/other.png"},
        headers=headers,
    )

    assert non_image.status_code == 400
    assert escaped.status_code == 400


@pytest.mark.asyncio
async def test_workspace_file_endpoint_serves_owned_file_and_rejects_escape(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    await client.post("/auth/register", json={
        "username": "fileuser",
        "email": "fileuser@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "fileuser", "password": "pass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    report_path = Path(tmp_path) / "1" / "reports" / "summary.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("# Summary\n", encoding="utf-8")

    response = await client.get(
        "/workspace/files",
        params={"path": "reports/summary.md"},
        headers=headers,
    )
    escaped = await client.get(
        "/workspace/files",
        params={"path": "../2/reports/summary.md"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.content == b"# Summary\n"
    assert "summary.md" in response.headers["content-disposition"]
    assert escaped.status_code == 400


@pytest.mark.asyncio
async def test_workspace_upload_endpoint_stores_user_file(client, tmp_path, monkeypatch):
    relative_workspace = tmp_path / "relative-workspace"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "workspace_dir", "relative-workspace")
    await client.post("/auth/register", json={
        "username": "uploaduser",
        "email": "uploaduser@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "uploaduser", "password": "pass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.post(
        "/workspace/uploads",
        headers=headers,
        data={"target_dir": "uploads/chat"},
        files={"file": ("robot cake?.png", b"fake image bytes", "image/png")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "robot cake?.png"
    assert payload["media_type"] == "image/png"
    assert payload["size_bytes"] == len(b"fake image bytes")
    assert payload["is_image"] is True
    assert payload["path"].startswith("uploads/chat/")
    assert "?" not in payload["stored_filename"]
    assert (relative_workspace / "1" / payload["path"]).read_bytes() == b"fake image bytes"


@pytest.mark.asyncio
async def test_workspace_upload_endpoint_rejects_escape_and_oversize(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_max_size_mb", 1)
    await client.post("/auth/register", json={
        "username": "uploadblocked",
        "email": "uploadblocked@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "uploadblocked", "password": "pass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    escaped = await client.post(
        "/workspace/uploads",
        headers=headers,
        data={"target_dir": "../other"},
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    oversized = await client.post(
        "/workspace/uploads",
        headers=headers,
        data={"target_dir": "uploads"},
        files={"file": ("big.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream")},
    )

    assert escaped.status_code == 400
    assert oversized.status_code == 413


@pytest.mark.asyncio
async def test_describe_image_requires_configured_vision_model(monkeypatch):
    from app.agent.tools import _describe_image

    monkeypatch.setattr(settings, "image_vision_model", "")
    result = await _describe_image(
        {"path": "generated_images/robot.png"},
        UserContext(user_id=1, username="imageuser", role="parent"),
    )

    assert "Image description is not configured" in result


@pytest.mark.asyncio
async def test_describe_image_calls_configured_vision_model(tmp_path, monkeypatch):
    from PIL import Image
    from app.agent.tools import _describe_image

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "image_vision_model", "ollama_chat/qwen-vl:test")
    image_path = Path(tmp_path) / "1" / "generated_images" / "robot.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (4, 4), color=(20, 30, 40)).save(image_path)

    fake_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="A small dark square is visible."))]
    )

    async def fake_acompletion(**kwargs):
        message = kwargs["messages"][0]
        content = message["content"]
        assert kwargs["model"] == "ollama_chat/qwen-vl:test"
        assert content[0]["type"] == "text"
        assert "What is visible?" in content[0]["text"]
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return fake_response

    with (
        patch("litellm.acompletion", new=AsyncMock(side_effect=fake_acompletion)),
        patch("app.llm_usage.record_llm_usage_event", new=AsyncMock()),
    ):
        result = await _describe_image(
            {"path": "generated_images/robot.png", "question": "What is visible?"},
            UserContext(user_id=1, username="imageuser", role="parent"),
        )

    assert "Image inspected: generated_images/robot.png" in result
    assert "A small dark square is visible." in result


@pytest.mark.asyncio
async def test_execute_chat_turn_emits_validating_and_retrying_states():
    user_context = UserContext(user_id=1, username="stateuser", role="parent")
    history = [{"role": "user", "content": "Give me a cleaned-up answer."}]
    captured_states = []

    async def _capture_state(state, **kwargs):
        captured_states.append({"state": state, **kwargs})

    validations = [
        SimpleNamespace(
            invalid_urls=[],
            should_retry=True,
            retry_reason="tool_call_leakage",
            cleaned_content="",
        ),
        SimpleNamespace(
            invalid_urls=[],
            should_retry=False,
            retry_reason=None,
            cleaned_content="",
        ),
    ]

    with (
        patch("app.api.chat.run_agent", new=AsyncMock(side_effect=["Draft answer", "Clean answer"])),
        patch("app.api.chat.validate_chat_response", side_effect=validations),
        patch("app.api.chat.build_chat_retry_instruction", return_value="Retry cleanly."),
    ):
        reply = await _execute_chat_turn(
            history,
            user_context,
            user_prompt="Give me a cleaned-up answer.",
            mode="chat",
            model_override="gpt-5-mini",
            stage="chat_simple",
            enable_validation=True,
            state_callback=_capture_state,
        )

    assert reply == "Clean answer"
    assert [item["state"] for item in captured_states] == ["validating", "retrying", "validating"]
    assert captured_states[1]["retry_reason"] == "tool_call_leakage"
    assert captured_states[1]["attempt"] == 1


@pytest.mark.asyncio
async def test_rest_local_post_tool_synthesis_recovery_persists_tool_turns(client):
    await client.post("/auth/register", json={
        "username": "restrecoveruser",
        "email": "restrecover@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "restrecoveruser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "REST Recovery"}, headers=headers)
    session_id = create.json()["id"]
    manager = get_chat_run_manager()
    await manager.clear(session_id)
    manager._recent_prompts.pop(session_id, None)
    manager._recent_send_ids.pop(session_id, None)
    runtime_messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "private streamed reasoning must not persist",
            "tool_calls": [
                {
                    "id": "call_sum_1",
                    "type": "function",
                    "function": {"name": "summarize_document", "arguments": '{"document_name":"Agents of Chaos.pdf"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_sum_1",
            "content": "Long saved summary content " * 200,
        },
    ]

    async def _failing_execute(*args, **kwargs):
        callback = kwargs["runtime_message_callback"]
        await callback(runtime_messages)
        raise RuntimeError("local synthesis failed after tool execution")

    with (
        patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=_failing_execute)),
        patch("app.api.chat.get_agent_runtime_history", return_value=runtime_messages),
        patch("app.api.chat._apply_required_library_grounding", new=AsyncMock(side_effect=lambda history, *args, **kwargs: history)),
        patch("app.api.chat._is_local_chat_model", return_value=True),
        patch("app.api.chat._runtime_history_has_completed_tool_turn", return_value=True),
    ):
        sent = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "summarize the agents of chaos document in my library"},
            headers=headers,
        )

    assert sent.status_code == 200
    assert "local model failed" in sent.json()["content"]
    async with TestSessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id)
            )
        ).scalars().all()
    assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert "summarize_document" in (rows[1].tool_calls or "")
    assert "call_sum_1" in (rows[2].tool_results or "")
    assert "local model failed" in rows[3].content


@pytest.mark.asyncio
async def test_websocket_local_post_tool_synthesis_recovery_persists_tool_turns(client):
    await client.post("/auth/register", json={
        "username": "wsrecoveruser",
        "email": "wsrecover@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "wsrecoveruser", "password": "pass123"})

    create = await client.post(
        "/chat/sessions",
        json={"title": "WS Recovery"},
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    session_id = create.json()["id"]
    runtime_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_sum_1",
                    "type": "function",
                    "function": {"name": "summarize_document", "arguments": '{"document_name":"Agents of Chaos.pdf"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_sum_1",
            "content": "Long saved summary content " * 200,
        },
    ]

    async def _failing_execute(*args, **kwargs):
        callback = kwargs["runtime_message_callback"]
        await callback(runtime_messages)
        raise RuntimeError("local synthesis failed after tool execution")

    async with TestSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "wsrecoveruser"))
        ).scalar_one()
        session = (
            await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one()

        manager = get_chat_run_manager()
        await manager.clear(session_id)
        manager._recent_prompts.pop(session_id, None)
        manager._recent_send_ids.pop(session_id, None)

        websocket = AsyncMock()
        with (
            patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=_failing_execute)),
            patch("app.api.chat.get_agent_runtime_history", return_value=runtime_messages),
            patch("app.api.chat.classify_chat_complexity", return_value=SimpleNamespace(is_complex=False)),
            patch("app.api.chat._apply_required_library_grounding", new=AsyncMock(side_effect=lambda history, *args, **kwargs: history)),
            patch("app.api.chat._is_local_chat_model", return_value=True),
            patch("app.api.chat._runtime_history_has_completed_tool_turn", return_value=True),
        ):
            await _run_websocket_message(
                session_id=session_id,
                websocket=websocket,
                db=db,
                current_user=user,
                session=session,
                user_message="summarize the agents of chaos document in my library",
                client_send_id="ws-recover-1",
                allowed_tools=None,
                blocked_tools=None,
            )

        payloads = [call.args[0] for call in websocket.send_json.await_args_list]
        done_payload = next(payload for payload in payloads if payload["type"] == "done")
        assert "local model failed" in done_payload["content"]

        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id)
            )
        ).scalars().all()
        assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_runtime_history_flush_skips_non_persistable_messages_without_duplicate_rows(client):
    await client.post("/auth/register", json={
        "username": "runtimeflushuser",
        "email": "runtimeflush@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "runtimeflushuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Runtime Flush"}, headers=headers)
    session_id = create.json()["id"]
    manager = get_chat_run_manager()
    await manager.clear(session_id)
    manager._recent_prompts.pop(session_id, None)
    manager._recent_send_ids.pop(session_id, None)

    first_batch = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "private streamed reasoning must not persist",
            "tool_calls": [
                {
                    "id": "call_sum_1",
                    "type": "function",
                    "function": {"name": "summarize_document", "arguments": '{"document_name":"Agents of Chaos.pdf"}'},
                }
            ],
        },
        {
            "role": "assistant",
            "content": "internal planning note",
        },
        {
            "role": "tool",
            "tool_call_id": "call_sum_1",
            "content": "saved tool output",
        },
    ]
    second_batch = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_sum_2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"workspace/report.md"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_sum_2",
            "content": "second tool output",
        },
    ]
    runtime_history = [*first_batch, *second_batch]
    history_state = {"messages": []}

    async def _execute_with_incremental_flush(*args, **kwargs):
        callback = kwargs["runtime_message_callback"]
        await callback(first_batch)
        history_state["messages"] = runtime_history
        raise RuntimeError("local synthesis failed after incremental flush")

    with (
        patch("app.api.chat._execute_chat_turn", new=AsyncMock(side_effect=_execute_with_incremental_flush)),
        patch("app.api.chat._apply_required_library_grounding", new=AsyncMock(side_effect=lambda history, *args, **kwargs: history)),
        patch("app.api.chat._is_local_chat_model", return_value=True),
        patch("app.api.chat._runtime_history_has_completed_tool_turn", return_value=True),
        patch("app.api.chat.get_agent_runtime_history", side_effect=lambda: history_state["messages"]),
    ):
        sent = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "summarize the agents of chaos document in my library"},
            headers=headers,
        )

    assert sent.status_code == 200
    async with TestSessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id)
            )
        ).scalars().all()

    roles = [row.role for row in rows]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert "call_sum_1" in (rows[2].tool_results or "")
    assert "call_sum_2" in (rows[4].tool_results or "")
    tool_call_payloads = [row.tool_calls for row in rows if row.role == "assistant" and row.tool_calls]
    assert len(tool_call_payloads) == 2
    assert sum("call_sum_1" in str(payload or "") for payload in tool_call_payloads) == 1
    assert sum("call_sum_2" in str(payload or "") for payload in tool_call_payloads) == 1
    assert all("private streamed reasoning" not in str(row.content or "") for row in rows)
    assert all("private streamed reasoning" not in str(row.tool_calls or "") for row in rows)


@pytest.mark.asyncio
async def test_chat_session_status_reports_active_run(client):
    await client.post("/auth/register", json={
        "username": "chatstatususer",
        "email": "chatstatus@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatstatususer", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Status"}, headers=headers)
    session_id = create.json()["id"]

    async with TestSessionLocal() as db:
        manager = get_chat_run_manager()
        task = asyncio.create_task(asyncio.sleep(5))
        await manager.register(session_id, task)
        try:
            resp = await client.get(f"/chat/sessions/{session_id}/status", headers=headers)
            assert resp.status_code == 200
            assert resp.json() == {"session_id": session_id, "active": True}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await manager.clear(session_id, task)


@pytest.mark.asyncio
async def test_websocket_handler_ignores_receive_task_disconnect_after_message_completion(client):
    await client.post("/auth/register", json={
        "username": "chatdisconnectuser",
        "email": "chatdisconnect@example.com",
        "password": "pass123",
    })
    login = await client.post("/auth/login", json={"username": "chatdisconnectuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Disconnect Cleanup"}, headers=headers)
    session_id = create.json()["id"]

    async def fake_run_message(**kwargs):
        return None

    class FakeWebSocket:
        def __init__(self):
            self.headers = {"authorization": f"Bearer {token}"}
            self.sent = []
            self._first = True

        async def accept(self):
            return None

        async def receive_text(self):
            if self._first:
                self._first = False
                return '{"content":"hello","client_send_id":"cleanup-1"}'
            raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self):
            return None

    websocket = FakeWebSocket()

    async with TestSessionLocal() as db:
        with patch("app.api.chat._run_websocket_message", new=AsyncMock(side_effect=fake_run_message)):
            await chat_websocket(session_id, websocket, db)
