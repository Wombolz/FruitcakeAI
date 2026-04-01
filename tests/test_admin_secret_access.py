from __future__ import annotations

import pytest

from app.db.models import Secret
from app.secrets_service import resolve_secret_value
from tests.conftest import TestSessionLocal


async def _admin_headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": password,
            "role": "admin",
        },
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _user_headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": password,
        },
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_secret_access_events_lists_and_filters_entries(client):
    admin_headers = await _admin_headers(client, "secretauditadmin")
    user_headers = await _user_headers(client, "secretaudituser")

    created = await client.post(
        "/secrets",
        json={"name": "n2yo_api_key", "provider": "n2yo", "value": "admin-audit-secret"},
        headers=user_headers,
    )
    assert created.status_code == 201
    secret_id = created.json()["id"]

    async with TestSessionLocal() as db:
        secret = await db.get(Secret, secret_id)
        assert secret is not None
        await resolve_secret_value(
            db,
            user_id=secret.user_id,
            name="n2yo_api_key",
            mark_used=True,
            tool_name="api_request:n2yo:iss_visual_passes",
            task_id=77,
            audit=True,
        )
        await resolve_secret_value(
            db,
            user_id=secret.user_id,
            name="missing_secret",
            mark_used=True,
            tool_name="api_request:n2yo:iss_visual_passes",
            task_id=77,
            audit=True,
        )
        await db.commit()

    listing = await client.get("/admin/secret-access-events", headers=admin_headers)
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["count"] == 2
    assert payload["entries"][0]["username"] == "secretaudituser"
    assert payload["entries"][0].get("ciphertext") is None

    filtered = await client.get(
        "/admin/secret-access-events?secret_name=n2yo_api_key&success=true",
        headers=admin_headers,
    )
    assert filtered.status_code == 200
    filtered_payload = filtered.json()
    assert filtered_payload["count"] == 1
    assert filtered_payload["entries"][0]["secret_id"] == secret_id
    assert filtered_payload["entries"][0]["task_id"] == 77
    assert filtered_payload["entries"][0]["success"] is True
