"""
Auth endpoint integration tests.
Uses an in-memory SQLite database so no real postgres is needed.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from unittest.mock import AsyncMock, patch

from app.db.session import Base, get_db
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

    sessions = await client.get("/chat/sessions", headers=headers)
    assert sessions.status_code == 200
    ids = [s["id"] for s in sessions.json()]
    assert session_id in ids


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
