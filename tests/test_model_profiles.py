from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.runtime.provider import resolve_provider_capabilities
from app.config import settings
from app.model_profiles import get_model_profile_service


async def _register(client, username: str, *, role: str = "parent") -> dict[str, str]:
    response = await client.post("/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": "pass123",
        "role": role,
    })
    assert response.status_code == 201, response.text
    login = await client.post("/auth/login", json={"username": username, "password": "pass123"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.asyncio
async def test_model_profiles_seed_from_config_with_qwen_38_capabilities(client, monkeypatch):
    monkeypatch.setattr(settings, "local_models", "ollama_chat/qwen3.8:27b-q4_K_M")
    get_model_profile_service().clear()
    headers = await _register(client, "profileadmin", role="admin")

    response = await client.get("/admin/model-profiles", headers=headers)

    assert response.status_code == 200
    profile = next(
        item for item in response.json()["profiles"]
        if item["model_id"] == "ollama_chat/qwen3.8:27b-q4_K_M"
    )
    assert profile["id"] == profile["model_id"]
    assert profile["profile_id"]
    assert profile["provider"] == "local"
    assert profile["label"] == profile["display_name"]
    assert isinstance(profile["is_default_chat"], bool)
    assert isinstance(profile["is_default_task_small"], bool)
    assert isinstance(profile["is_default_task_large"], bool)
    assert profile["reasoning_efforts"] == ["low", "medium", "xhigh"]
    assert profile["default_reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_deployment_model_profile_defaults_are_visible_in_effective_settings(client, monkeypatch):
    model = "ollama_chat/qwen3.8:27b-q4_K_M"
    monkeypatch.setattr(settings, "llm_model", model)
    monkeypatch.setattr(settings, "local_models", model)
    get_model_profile_service().clear()
    headers = await _register(client, "profiledefaults")

    response = await client.get("/settings/me", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_chat_model"] == {"value": model, "source": "deployment"}
    assert payload["model_profile_id"]["value"]
    assert payload["reasoning_effort"] == {"value": "low", "source": "admin"}


@pytest.mark.asyncio
async def test_admin_profile_change_updates_provider_capabilities_without_restart(client, monkeypatch):
    model = "ollama_chat/hot-profile:test"
    monkeypatch.setattr(settings, "local_models", model)
    get_model_profile_service().clear()
    headers = await _register(client, "hotadmin", role="admin")
    listed = await client.get("/admin/model-profiles", headers=headers)
    profile = next(item for item in listed.json()["profiles"] if item["model_id"] == model)

    updated = await client.patch(
        f"/admin/model-profiles/{profile['profile_id']}",
        headers=headers,
        json={
            "supports_tools": False,
            "tool_mode": "text_only",
            "supports_vision": True,
            "keep_alive": "30m",
        },
    )

    assert updated.status_code == 200
    capabilities = resolve_provider_capabilities(model)
    assert capabilities.configured_text_only is True
    assert capabilities.vision is True
    assert capabilities.runtime_keep_alive == "30m"


@pytest.mark.asyncio
async def test_admin_can_update_model_context_budget_without_restart(client, monkeypatch):
    model = "ollama_chat/context-profile:test"
    monkeypatch.setattr(settings, "local_models", model)
    get_model_profile_service().clear()
    headers = await _register(client, "contextadmin", role="admin")
    listed = await client.get("/admin/model-profiles", headers=headers)
    profile = next(item for item in listed.json()["profiles"] if item["model_id"] == model)

    updated = await client.patch(
        f"/admin/model-profiles/{profile['profile_id']}",
        headers=headers,
        json={
            "context_window_tokens": 131_072,
            "output_reserve_tokens": 12_288,
            "reasoning_reserve_tokens": 8_192,
            "context_safety_margin_tokens": 4_096,
        },
    )

    assert updated.status_code == 200, updated.text
    payload = updated.json()
    assert payload["context_window_tokens"] == 131_072
    assert payload["output_reserve_tokens"] == 12_288
    assert payload["reasoning_reserve_tokens"] == 8_192
    assert payload["context_safety_margin_tokens"] == 4_096
    snapshot = get_model_profile_service().for_model(model)
    assert snapshot is not None
    assert snapshot.context_window_tokens == 131_072


@pytest.mark.asyncio
async def test_admin_profile_rejects_context_reserves_that_consume_input_window(client, monkeypatch):
    model = "ollama_chat/invalid-context-profile:test"
    monkeypatch.setattr(settings, "local_models", model)
    get_model_profile_service().clear()
    headers = await _register(client, "invalidcontextadmin", role="admin")
    listed = await client.get("/admin/model-profiles", headers=headers)
    profile = next(item for item in listed.json()["profiles"] if item["model_id"] == model)

    response = await client.patch(
        f"/admin/model-profiles/{profile['profile_id']}",
        headers=headers,
        json={
            "context_window_tokens": 8_192,
            "output_reserve_tokens": 5_000,
            "reasoning_reserve_tokens": 1_000,
            "context_safety_margin_tokens": 500,
        },
    )

    assert response.status_code == 422
    assert "leave at least 2048 tokens" in response.json()["error"]


@pytest.mark.asyncio
async def test_qwen_38_profile_rejects_unsupported_reasoning_value(client, monkeypatch):
    model = "ollama_chat/qwen3.8:27b-q4_K_M"
    monkeypatch.setattr(settings, "local_models", model)
    get_model_profile_service().clear()
    headers = await _register(client, "qwenadmin", role="admin")
    listed = await client.get("/admin/model-profiles", headers=headers)
    profile = next(item for item in listed.json()["profiles"] if item["model_id"] == model)

    response = await client.patch(
        f"/admin/model-profiles/{profile['profile_id']}",
        headers=headers,
        json={"reasoning_efforts": ["low", "high"]},
    )

    assert response.status_code == 422
    assert "low, medium, and xhigh" in response.json()["error"]


@pytest.mark.asyncio
async def test_user_model_preference_drives_new_session_and_next_turn(client, monkeypatch):
    model = "ollama_chat/qwen3.8:27b-q4_K_M"
    monkeypatch.setattr(settings, "local_models", model)
    get_model_profile_service().clear()
    admin_headers = await _register(client, "preferenceadmin", role="admin")
    listed = await client.get("/admin/model-profiles", headers=admin_headers)
    profile = next(item for item in listed.json()["profiles"] if item["model_id"] == model)
    user_headers = await _register(client, "modelpreferenceuser")

    preferences = await client.patch(
        "/settings/me",
        headers=user_headers,
        json={
            "expected_version": 0,
            "preferred_model_profile_id": profile["profile_id"],
            "preferred_reasoning_effort": "medium",
        },
    )
    assert preferences.status_code == 200, preferences.text
    assert preferences.json()["default_chat_model"] == {"value": model, "source": "user"}
    assert preferences.json()["reasoning_effort"] == {"value": "medium", "source": "user"}

    session = await client.post("/chat/sessions", headers=user_headers, json={"title": "Profile test"})
    assert session.status_code == 201
    assert session.json()["llm_model"] == model

    with patch("app.api.chat._execute_chat_turn", new=AsyncMock(return_value="ok")) as execute:
        response = await client.post(
            f"/chat/sessions/{session.json()['id']}/messages",
            headers=user_headers,
            json={"content": "Say hello"},
        )
    assert response.status_code == 200
    assert execute.await_args.kwargs["reasoning_effort_override"] == "medium"


@pytest.mark.asyncio
async def test_disabled_profile_is_hidden_and_rejected_for_session_selection(client, monkeypatch):
    model = "ollama_chat/disabled:test"
    monkeypatch.setattr(settings, "local_models", model)
    get_model_profile_service().clear()
    admin_headers = await _register(client, "disableadmin", role="admin")
    listed = await client.get("/admin/model-profiles", headers=admin_headers)
    profile = next(item for item in listed.json()["profiles"] if item["model_id"] == model)
    disabled = await client.patch(
        f"/admin/model-profiles/{profile['profile_id']}",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    user_headers = await _register(client, "disableduser")

    public_models = await client.get("/llm/models", headers=user_headers)
    assert model not in {item["id"] for item in public_models.json()["models"]}
    session = await client.post("/chat/sessions", headers=user_headers, json={"title": "Disabled"})
    changed = await client.patch(
        f"/chat/sessions/{session.json()['id']}/model",
        headers=user_headers,
        json={"llm_model": model},
    )
    assert changed.status_code == 400
