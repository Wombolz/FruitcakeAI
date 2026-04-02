from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Secret, SecretAccessEvent
from app.secrets_service import SecretDecryptionError, decrypt_secret_value, resolve_secret_value
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
        json={"name": "openweathermap_api_key", "provider": "weather", "value": "one"},
        headers=headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/secrets",
        json={"name": "openweathermap_api_key", "provider": "weather", "value": "two"},
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


@pytest.mark.asyncio
async def test_disable_secret_prevents_resolution(client):
    token = await _login_token(client, "disableowner")
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/secrets",
        json={"name": "openweathermap_api_key", "provider": "weather", "value": "wx-secret"},
        headers=headers,
    )
    secret_id = created.json()["id"]

    disabled = await client.post(f"/secrets/{secret_id}/disable", headers=headers)
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False

    async with TestSessionLocal() as db:
        secret = await db.get(Secret, secret_id)
        assert secret is not None
        assert secret.is_active is False
        resolved = await resolve_secret_value(db, user_id=secret.user_id, name="openweathermap_api_key", mark_used=True)
        assert resolved is None


@pytest.mark.asyncio
async def test_secret_service_requires_explicit_master_key(client):
    from app.config import settings
    from app.secrets_service import SecretConfigurationError, encrypt_secret_value

    original = settings.secrets_master_key
    settings.secrets_master_key = ""
    try:
        with pytest.raises(SecretConfigurationError, match="SECRETS_MASTER_KEY"):
            encrypt_secret_value("plain-secret")
    finally:
        settings.secrets_master_key = original


@pytest.mark.asyncio
async def test_secret_service_reports_clear_error_for_wrong_master_key(client):
    from app.config import settings
    from app.secrets_service import SecretDecryptionError, encrypt_secret_value

    original = settings.secrets_master_key
    try:
        settings.secrets_master_key = "test-master-key-a"
        ciphertext = encrypt_secret_value("plain-secret")
        settings.secrets_master_key = "test-master-key-b"
        with pytest.raises(
            SecretDecryptionError,
            match="Rotate this secret or verify SECRETS_MASTER_KEY",
        ):
            decrypt_secret_value(ciphertext)
    finally:
        settings.secrets_master_key = original


@pytest.mark.asyncio
async def test_secret_access_is_audited_on_success_and_failure(client):
    token = await _login_token(client, "auditowner")
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/secrets",
        json={"name": "n2yo_api_key", "provider": "n2yo", "value": "audit-secret"},
        headers=headers,
    )
    secret_id = created.json()["id"]

    async with TestSessionLocal() as db:
        secret = await db.get(Secret, secret_id)
        assert secret is not None
        success = await resolve_secret_value(
            db,
            user_id=secret.user_id,
            name="n2yo_api_key",
            mark_used=True,
            tool_name="api_request:n2yo:iss_visual_passes",
            task_id=69,
            audit=True,
        )
        assert success == "audit-secret"
        missing = await resolve_secret_value(
            db,
            user_id=secret.user_id,
            name="missing_secret",
            mark_used=True,
            tool_name="api_request:n2yo:iss_visual_passes",
            task_id=69,
            audit=True,
        )
        assert missing is None
        await db.commit()

        events = (await db.execute(select(SecretAccessEvent).order_by(SecretAccessEvent.id.asc()))).scalars().all()
        assert len(events) == 2
        assert events[0].secret_id == secret_id
        assert events[0].success is True
        assert events[0].tool_name == "api_request:n2yo:iss_visual_passes"
        assert events[0].task_id == 69
        assert events[1].success is False
        assert events[1].error_class == "SecretNotFound"


@pytest.mark.asyncio
async def test_secret_access_events_endpoint_returns_metadata_only(client):
    token = await _login_token(client, "eventowner")
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/secrets",
        json={"name": "n2yo_api_key", "provider": "n2yo", "value": "event-secret"},
        headers=headers,
    )
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
            task_id=42,
            audit=True,
        )
        await db.commit()

    response = await client.get(f"/secrets/{secret_id}/access-events", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["secret_id"] == secret_id
    assert data[0]["secret_name"] == "n2yo_api_key"
    assert data[0]["tool_name"] == "api_request:n2yo:iss_visual_passes"
    assert data[0]["task_id"] == 42
    assert "ciphertext" not in data[0]
