"""
Shared pytest fixtures for FruitcakeAI v5 tests.

All tests that touch the database use an in-memory SQLite engine so no
real PostgreSQL instance is required.  The fixture pattern mirrors the
existing test_auth.py approach.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.session import Base, get_db
from app.main import app

# ── In-memory SQLite engine ────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(
    test_engine, class_=AsyncSession, expire_on_commit=False
)


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
    """Create all tables before each test; drop them after."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    """Async HTTP client wired to the FastAPI app with the test database."""
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── Auth helpers ───────────────────────────────────────────────────────────────

async def register_and_login(
    client: AsyncClient,
    username: str,
    password: str = "testpass123",
    role: str | None = None,
) -> str:
    """Register a user and return their access token.

    If role is 'admin' the user is created via the register endpoint and then
    the role is patched directly through the DB override (register always creates
    a 'parent' role — admin must be seeded).
    """
    payload = {
        "username": username,
        "email": f"{username}@test.local",
        "password": password,
    }
    await client.post("/auth/register", json=payload)
    resp = await client.post("/auth/login", json={"username": username, "password": password})
    return resp.json()["access_token"]


@pytest.fixture
async def user_token(client: AsyncClient) -> str:
    """Access token for a regular (parent) user."""
    return await register_and_login(client, "testuser")


@pytest.fixture(autouse=True)
def secrets_master_key_for_tests():
    original = settings.secrets_master_key
    settings.secrets_master_key = "test-secrets-master-key"
    try:
        yield
    finally:
        settings.secrets_master_key = original
