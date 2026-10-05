from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse
import base64
import hashlib

import pytest
from jose import jwt
from sqlalchemy import select

from app.config import settings
from app.db.models import Secret, User, UserIntegration
from app.integrations.service import resolve_user_integration
from app.mcp.servers.calendar import _resolve_provider
from app.agent.context import UserContext
from tests.conftest import TestSessionLocal


@pytest.fixture(autouse=True)
def _mock_apple_verification(monkeypatch):
    monkeypatch.setattr(
        "app.mcp.servers.calendar.verify_apple_caldav",
        AsyncMock(return_value=None),
    )


async def _register(client, username: str) -> tuple[dict[str, str], int]:
    response = await client.post("/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": "pass123",
    })
    assert response.status_code == 201
    login = await client.post("/auth/login", json={"username": username, "password": "pass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        return headers, int(user.id)


@pytest.mark.asyncio
async def test_apple_calendar_connection_is_user_owned_encrypted_and_reconnects(client, monkeypatch):
    monkeypatch.setattr(settings, "secrets_master_key", "integration-test-key")
    headers, user_id = await _register(client, "appleowner")

    first = await client.post("/integrations/apple/calendar/connect", headers=headers, json={
        "username": "owner@icloud.com",
        "app_password": "first-app-password",
        "url": "https://caldav.icloud.com",
        "default_calendar": "Home",
    })
    assert first.status_code == 201, first.text
    assert "password" not in first.text.lower()
    assert "secret_id" not in first.text.lower()

    second = await client.post("/integrations/apple/calendar/connect", headers=headers, json={
        "username": "owner@icloud.com",
        "app_password": "rotated-app-password",
        "url": "https://caldav.icloud.com",
        "default_calendar": "Family",
    })
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]

    async with TestSessionLocal() as db:
        rows = (await db.execute(select(UserIntegration).where(UserIntegration.user_id == user_id))).scalars().all()
        secrets = (await db.execute(select(Secret).where(Secret.user_id == user_id))).scalars().all()
        resolved = await resolve_user_integration(db, user_id=user_id, provider="apple")
    assert len(rows) == 1
    assert len(secrets) == 1
    assert secrets[0].ciphertext != "rotated-app-password"
    assert resolved is not None
    assert resolved.credential == "rotated-app-password"
    assert resolved.config["default_calendar"] == "Family"


@pytest.mark.asyncio
async def test_integrations_are_isolated_and_disconnect_revokes_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "secrets_master_key", "integration-test-key")
    owner_headers, owner_id = await _register(client, "integrationowner")
    other_headers, other_id = await _register(client, "integrationother")
    connected = await client.post("/integrations/apple/calendar/connect", headers=owner_headers, json={
        "username": "owner@icloud.com",
        "app_password": "private-app-password",
        "url": "https://caldav.icloud.com",
    })
    public_id = connected.json()["id"]

    other_list = await client.get("/integrations", headers=other_headers)
    other_disconnect = await client.post(f"/integrations/{public_id}/disconnect", headers=other_headers)
    assert other_list.json() == {"integrations": []}
    assert other_disconnect.status_code == 404

    disconnected = await client.post(f"/integrations/{public_id}/disconnect", headers=owner_headers)
    assert disconnected.status_code == 200
    assert disconnected.json()["status"] == "disconnected"
    async with TestSessionLocal() as db:
        assert await resolve_user_integration(db, user_id=owner_id, provider="apple") is None
        assert await resolve_user_integration(db, user_id=other_id, provider="apple") is None
        secret = (await db.execute(select(Secret).where(Secret.user_id == owner_id))).scalar_one()
        assert secret.is_active is False


@pytest.mark.asyncio
async def test_google_oauth_state_is_bound_to_requesting_user(client, monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", "client-id")
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "fruitcake://oauth/google")
    first_headers, _ = await _register(client, "googlefirst")
    second_headers, _ = await _register(client, "googlesecond")
    challenge = "a" * 43

    auth_url = await client.get(
        "/integrations/google/calendar/auth-url",
        headers=first_headers,
        params={"code_challenge": challenge},
    )
    assert auth_url.status_code == 200
    state = parse_qs(urlparse(auth_url.json()["authorization_url"]).query)["state"][0]
    payload = jwt.decode(state, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    assert payload["purpose"] == "google_calendar_oauth"

    callback = await client.post("/integrations/google/calendar/callback", headers=second_headers, json={
        "code": "code",
        "state": state,
        "code_verifier": "v" * 43,
    })
    assert callback.status_code == 400
    assert "does not match this user" in callback.json()["error"]


@pytest.mark.asyncio
async def test_google_callback_persists_tokens_without_returning_them(client, monkeypatch):
    monkeypatch.setattr(settings, "secrets_master_key", "integration-test-key")
    monkeypatch.setattr(settings, "google_oauth_client_id", "client-id")
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "fruitcake://oauth/google")
    headers, user_id = await _register(client, "googleowner")
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    auth_url = await client.get(
        "/integrations/google/calendar/auth-url",
        headers=headers,
        params={"code_challenge": challenge},
    )
    state = parse_qs(urlparse(auth_url.json()["authorization_url"]).query)["state"][0]

    with (
        patch("app.api.integrations.exchange_google_code", new=AsyncMock(return_value={
            "access_token": "access-private",
            "refresh_token": "refresh-private",
            "expires_in": 3600,
            "scope": "openid email https://www.googleapis.com/auth/calendar",
        })),
        patch("app.api.integrations._google_account_identifier", new=AsyncMock(return_value="owner@gmail.com")),
    ):
        response = await client.post("/integrations/google/calendar/callback", headers=headers, json={
            "code": "code",
            "state": state,
            "code_verifier": verifier,
        })

    assert response.status_code == 201, response.text
    assert response.json()["account_identifier"] == "owner@gmail.com"
    assert "access-private" not in response.text
    assert "refresh-private" not in response.text
    async with TestSessionLocal() as db:
        resolved = await resolve_user_integration(db, user_id=user_id, provider="google")
    assert resolved is not None
    assert resolved.access_token == "access-private"
    assert resolved.refresh_token == "refresh-private"

    replay = await client.post("/integrations/google/calendar/callback", headers=headers, json={
        "code": "code",
        "state": state,
        "code_verifier": verifier,
    })
    assert replay.status_code == 400
    assert "already used" in replay.json()["error"]


@pytest.mark.asyncio
async def test_calendar_resolver_prefers_user_integration_over_deployment_fallback(client, monkeypatch):
    monkeypatch.setattr(settings, "secrets_master_key", "integration-test-key")
    headers, user_id = await _register(client, "calendarresolver")
    await client.post("/integrations/apple/calendar/connect", headers=headers, json={
        "username": "resolver@icloud.com",
        "app_password": "resolver-password",
        "url": "https://caldav.icloud.com",
        "default_calendar": "Family",
    })
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", TestSessionLocal)
    provider = object()

    with patch("app.mcp.servers.calendar._AppleProvider", return_value=provider) as factory:
        resolved = await _resolve_provider(
            UserContext(user_id=user_id, username="calendarresolver", role="parent")
        )

    assert resolved is provider
    factory.assert_called_once_with(
        url="https://caldav.icloud.com",
        username="resolver@icloud.com",
        password="resolver-password",
        default_calendar="Family",
    )
