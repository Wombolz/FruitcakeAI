from __future__ import annotations

import pytest

from app.config import settings
from app.model_profiles import get_model_profile_service


async def _register(client, username: str, *, role: str = "parent") -> dict[str, str]:
    response = await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "testpass123",
            "role": role,
        },
    )
    assert response.status_code == 201, response.text
    login = await client.post(
        "/auth/login",
        json={"username": username, "password": "testpass123"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.asyncio
async def test_admin_model_allowlist_filters_user_models_and_preferences(client, monkeypatch):
    monkeypatch.setattr(settings, "local_models", "ollama_chat/access-a,ollama_chat/access-b")
    get_model_profile_service().clear()
    admin_headers = await _register(client, "accessadmin", role="admin")
    user_headers = await _register(client, "accessuser")

    users = (await client.get("/admin/users", headers=admin_headers)).json()
    user_id = next(item["id"] for item in users if item["username"] == "accessuser")
    access = await client.get(f"/admin/users/{user_id}/model-access", headers=admin_headers)
    assert access.status_code == 200
    assert access.json()["policy_mode"] == "deployment_default"
    profiles = [item for item in access.json()["models"] if item["model_id"].startswith("ollama_chat/access-")]
    assert len(profiles) == 2

    allowed = profiles[0]
    denied = profiles[1]
    updated = await client.put(
        f"/admin/users/{user_id}/model-access",
        headers=admin_headers,
        json={"allowed_profile_ids": [allowed["profile_id"]]},
    )
    assert updated.status_code == 200
    assert updated.json()["policy_mode"] == "explicit"

    visible = await client.get("/llm/models", headers=user_headers)
    assert {item["profile_id"] for item in visible.json()["models"]} == {allowed["profile_id"]}
    rejected = await client.patch(
        "/settings/me",
        headers=user_headers,
        json={"preferred_model_profile_id": denied["profile_id"]},
    )
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_revoked_model_is_rejected_for_existing_session(client, monkeypatch):
    monkeypatch.setattr(settings, "local_models", "ollama_chat/revoke-a,ollama_chat/revoke-b")
    get_model_profile_service().clear()
    admin_headers = await _register(client, "revokeadmin", role="admin")
    user_headers = await _register(client, "revokeuser")
    models = (await client.get("/llm/models", headers=user_headers)).json()["models"]
    selected = next(item for item in models if item["model_id"] == "ollama_chat/revoke-b")
    session = await client.post("/chat/sessions", headers=user_headers, json={"title": "Revoked"})
    changed = await client.patch(
        f"/chat/sessions/{session.json()['id']}/model",
        headers=user_headers,
        json={"llm_model": selected["model_id"]},
    )
    assert changed.status_code == 200

    users = (await client.get("/admin/users", headers=admin_headers)).json()
    user_id = next(item["id"] for item in users if item["username"] == "revokeuser")
    other = next(item for item in models if item["profile_id"] != selected["profile_id"])
    await client.put(
        f"/admin/users/{user_id}/model-access",
        headers=admin_headers,
        json={"allowed_profile_ids": [other["profile_id"]]},
    )

    response = await client.post(
        f"/chat/sessions/{session.json()['id']}/messages",
        headers=user_headers,
        json={"content": "Hello"},
    )
    assert response.status_code == 403
    assert "no longer available" in response.json()["error"]


@pytest.mark.asyncio
async def test_admin_policy_audit_records_metadata_not_private_content(client):
    admin_headers = await _register(client, "auditadmin", role="admin")
    user_headers = await _register(client, "audituser")
    me = await client.get("/auth/me", headers=user_headers)
    user_id = next(
        item["id"]
        for item in (await client.get("/admin/users", headers=admin_headers)).json()
        if item["public_id"] == me.json()["public_id"]
    )
    response = await client.patch(
        f"/admin/users/{user_id}",
        headers=admin_headers,
        json={"chat_routing_preference": "fast", "library_scopes": ["family_docs"]},
    )
    assert response.status_code == 200

    audit = await client.get(f"/admin/policy-audit?target_user_id={user_id}", headers=admin_headers)
    assert audit.status_code == 200
    event = audit.json()[0]
    assert event["action"] == "user.updated"
    assert event["summary"] == {"changed_fields": ["chat_routing_preference", "library_scopes"]}
    assert "email" not in str(event["summary"]).lower()


@pytest.mark.asyncio
async def test_empty_explicit_model_policy_blocks_new_chat_sessions(client):
    admin_headers = await _register(client, "nomodeladmin", role="admin")
    user_headers = await _register(client, "nomodeluser")
    users = (await client.get("/admin/users", headers=admin_headers)).json()
    user_id = next(item["id"] for item in users if item["username"] == "nomodeluser")
    updated = await client.put(
        f"/admin/users/{user_id}/model-access",
        headers=admin_headers,
        json={"allowed_profile_ids": []},
    )
    assert updated.status_code == 200

    response = await client.post("/chat/sessions", headers=user_headers, json={"title": "Unavailable"})
    assert response.status_code == 403
    assert "No chat model" in response.json()["error"]
