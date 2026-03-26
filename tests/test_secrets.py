from __future__ import annotations

import pytest

from app.db.models import Secret
from app.secrets_service import decrypt_secret_value, resolve_secret_value
from tests.conftest import TestSessionLocal


async def _login_token(client, username: str) -> str:
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "testpass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": username, "password": "testpass123"},
    )
    return login.json()["access_token"]


@pytest.mark.asyncio
async def test_create_list_and_rotate_secret(client):
    token = await _login_token(client, "secretuser")
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/secrets",
        json={"name": "n2yo_api_key", "provider": "n2yo", "value": "super-secret-key"},
        headers=headers,
    )
    assert created.status_code == 201
    data = created.json()
    assert data["name"] == "n2yo_api_key"
    assert data["provider"] == "n2yo"
    assert data["masked_preview"].endswith("key")

    listing = await client.get("/secrets", headers=headers)
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    rotated = await client.post(
        f"/secrets/{data['id']}/rotate",
        json={"value": "even-more-secret"},
        headers=headers,
    )
    assert rotated.status_code == 200
    assert rotated.json()["masked_preview"].endswith("cret")


@pytest.mark.asyncio
async def test_secret_name_is_unique_per_user(client):
    token = await _login_token(client, "dupsecret")
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post(
        "/secrets",
        json={"name": "weather_api_key", "provider": "weather", "value": "one"},
        headers=headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/secrets",
        json={"name": "weather_api_key", "provider": "weather", "value": "two"},
        headers=headers,
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_secret_resolve_is_owner_scoped_and_marks_last_used(client):
    token = await _login_token(client, "resolveowner")
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/secrets",
        json={"name": "iss_api_key", "provider": "n2yo", "value": "iss-secret"},
        headers=headers,
    )
    secret_id = created.json()["id"]

    async with TestSessionLocal() as db:
        secret = await db.get(Secret, secret_id)
        assert secret is not None
        assert decrypt_secret_value(secret.ciphertext) == "iss-secret"
        resolved = await resolve_secret_value(db, user_id=secret.user_id, name="iss_api_key", mark_used=True)
        assert resolved == "iss-secret"
        assert secret.last_used_at is not None

        missing = await resolve_secret_value(db, user_id=9999, name="iss_api_key", mark_used=True)
        assert missing is None
