"""
Auth endpoint integration tests.
Uses an in-memory SQLite database so no real postgres is needed.
"""

import asyncio
import contextlib
from types import SimpleNamespace
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select, func
from sqlalchemy.pool import StaticPool
from unittest.mock import AsyncMock, patch

from app.api.chat import _run_websocket_message, chat_websocket
from app.chat_runtime import get_chat_run_manager
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
            json={"content": "hello"},
            headers=headers,
        )
    assert sent.status_code == 200

    history = await client.get(f"/chat/sessions/{session_id}", headers=headers)
    assert history.status_code == 200
    messages = history.json()["messages"]
    assert len(messages) >= 2
    assert all("created_at" in msg for msg in messages)


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
