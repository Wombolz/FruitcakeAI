from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models import User
from app.settings_service import UserSettingsResolver
from tests.conftest import TestSessionLocal


async def _register_and_login(client, username: str) -> str:
    registered = await client.post("/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": "testpass123",
    })
    assert registered.status_code == 201, registered.text
    login = await client.post("/auth/login", json={"username": username, "password": "testpass123"})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


@pytest.mark.asyncio
async def test_public_user_id_is_stable_across_auth_reads(client):
    token = await _register_and_login(client, "stableidentity")
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.get("/auth/me", headers=headers)
    second = await client.get("/auth/me", headers=headers)

    assert first.status_code == 200
    public_id = first.json()["public_id"]
    assert UUID(public_id).version == 4
    assert second.json()["public_id"] == public_id


@pytest.mark.asyncio
async def test_public_user_ids_are_unique(client):
    first_token = await _register_and_login(client, "identityone")
    second_token = await _register_and_login(client, "identitytwo")

    first = await client.get("/auth/me", headers={"Authorization": f"Bearer {first_token}"})
    second = await client.get("/auth/me", headers={"Authorization": f"Bearer {second_token}"})

    assert first.json()["public_id"] != second.json()["public_id"]


@pytest.mark.asyncio
async def test_admin_user_payload_exposes_public_id(client):
    await client.post("/auth/register", json={
        "username": "settingsadmin",
        "email": "settingsadmin@example.com",
        "password": "pass123",
        "role": "admin",
    })
    login = await client.post("/auth/login", json={"username": "settingsadmin", "password": "pass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.get("/admin/users", headers=headers)

    assert response.status_code == 200
    assert UUID(response.json()[0]["public_id"]).version == 4


@pytest.mark.asyncio
async def test_settings_read_uses_deployment_fallbacks_with_provenance(client, monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "ollama_chat/test-default")
    monkeypatch.setattr(settings, "image_vision_model", "ollama/test-vision")
    token = await _register_and_login(client, "fallbacksettings")

    response = await client.get(
        "/settings/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 0
    assert body["default_chat_model"] == {
        "value": "ollama_chat/test-default",
        "source": "deployment",
    }
    assert body["default_vision_model"] == {
        "value": "ollama/test-vision",
        "source": "deployment",
    }
    assert body["timezone"] == {"value": "UTC", "source": "deployment"}
    assert body["appearance"] == {"value": "system", "source": "deployment"}


@pytest.mark.asyncio
async def test_settings_patch_persists_typed_preferences_and_legacy_runtime_fields(client):
    token = await _register_and_login(client, "typedsettings")
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.patch(
        "/settings/me",
        headers=headers,
        json={
            "expected_version": 0,
            "chat_routing_preference": "deep",
            "timezone": "America/New_York",
            "active_hours_start": "07:30",
            "active_hours_end": "22:00",
            "notifications_enabled": False,
            "delivery_enabled": True,
            "appearance": "dark",
            "reduce_motion": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["chat_routing_preference"] == {"value": "deep", "source": "user"}
    assert body["timezone"] == {"value": "America/New_York", "source": "user"}
    assert body["notifications_enabled"] == {"value": False, "source": "user"}
    assert body["appearance"] == {"value": "dark", "source": "user"}

    me = await client.get("/auth/me", headers=headers)
    assert me.json()["chat_routing_preference"] == "deep"
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "typedsettings"))).scalar_one()
        assert user.active_hours_tz == "America/New_York"
        assert user.active_hours_start == "07:30"
        assert user.active_hours_end == "22:00"


@pytest.mark.asyncio
async def test_settings_patch_rejects_stale_version(client):
    token = await _register_and_login(client, "stalesettings")
    headers = {"Authorization": f"Bearer {token}"}
    first = await client.patch(
        "/settings/me",
        headers=headers,
        json={"expected_version": 0, "appearance": "dark"},
    )
    assert first.status_code == 200

    stale = await client.patch(
        "/settings/me",
        headers=headers,
        json={"expected_version": 0, "appearance": "light"},
    )

    assert stale.status_code == 409
    assert "current version: 1" in stale.json()["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"timezone": "Not/A_Timezone"}, "valid IANA timezone"),
        ({"active_hours_start": "7:30"}, "24-hour HH:MM"),
        ({"appearance": "sepia"}, "Input should be"),
        ({"notifications_enabled": None}, "cannot be null"),
    ],
)
async def test_settings_patch_validates_typed_values(client, payload, detail):
    token = await _register_and_login(client, f"invalid{abs(hash(str(payload)))}")
    response = await client.patch(
        "/settings/me",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )

    assert response.status_code == 422
    assert detail in str(response.json())


@pytest.mark.asyncio
async def test_settings_resolver_applies_explicit_overrides_last():
    async with TestSessionLocal() as db:
        user = User(
            username="resolveruser",
            email="resolver@example.com",
            hashed_password="unused",
            chat_routing_preference="fast",
            active_hours_tz="America/New_York",
        )
        db.add(user)
        await db.flush()

        resolver = UserSettingsResolver()
        resolved = await resolver.resolve(
            db,
            user,
            overrides={"chat_routing_preference": "deep", "timezone": "Europe/London"},
        )

    assert resolved.value("chat_routing_preference") == "deep"
    assert resolved.provenance["chat_routing_preference"] == "session_task_override"
    assert resolved.value("timezone") == "Europe/London"
    assert resolved.provenance["timezone"] == "session_task_override"
