from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import ManagedAgentPreset, Task, TaskRun, User
from app.task_service import compute_next_run_at
from tests.conftest import TestSessionLocal


@pytest.mark.asyncio
async def test_create_task_defaults_to_requires_approval_true(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskapprovaluser",
            "email": "taskapproval@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskapprovaluser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Safe default task",
            "instruction": "Check something and report back.",
            "task_type": "one_shot",
            "deliver": True,
        },
        headers=headers,
    )

    assert created.status_code == 201
    assert created.json()["requires_approval"] is True


@pytest.mark.asyncio
async def test_create_task_computes_next_run_at_from_task_timezone(client):
    await client.post(
        "/auth/register",
        json={
            "username": "tasktzuser",
            "email": "tasktz@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "tasktzuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Morning local task",
            "instruction": "Run every morning.",
            "task_type": "recurring",
            "schedule": "0 08 * * *",
            "active_hours_tz": "America/New_York",
        },
        headers=headers,
    )

    assert created.status_code == 201
    next_run = datetime.fromisoformat(created.json()["next_run_at"])
    next_run_local = next_run.astimezone(ZoneInfo("America/New_York"))
    assert next_run_local.hour == 8
    assert next_run_local.minute == 0
    assert next_run.tzinfo == timezone.utc
    assert created.json()["effective_timezone"] == "America/New_York"
    assert created.json()["next_run_at_localized"].endswith(("EDT", "EST"))
    assert created.json()["created_at_localized"].endswith(("EDT", "EST"))


@pytest.mark.asyncio
async def test_create_task_returns_recipe_metadata_for_normalized_watcher(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskrecipeuser",
            "email": "taskrecipe@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskrecipeuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Iran Watch",
            "instruction": "Watch Iran and Middle East news for major updates.",
            "task_type": "recurring",
            "schedule": "every:2h",
            "deliver": True,
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["profile"] == "topic_watcher"
    assert payload["task_recipe"]["family"] == "topic_watcher"
    assert payload["task_recipe"]["instruction_style"] == "recipe_v1"


@pytest.mark.asyncio
async def test_create_task_accepts_explicit_recipe_family_from_editor(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskeditoruser",
            "email": "taskeditor@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskeditoruser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Daily Photography Briefing",
            "instruction": "Keep it concise and emphasize photo-industry business news.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
            "recipe_family": "briefing",
            "recipe_params": {
                "briefing_mode": "morning",
                "topic": "Photography",
                "path": "workspace/photography/daily.md",
                "window_hours": 24,
                "custom_guidance": "Keep it concise and emphasize photo-industry business news.",
            },
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "briefing"
    assert payload["task_recipe"]["params"]["briefing_mode"] == "morning"
    assert payload["task_recipe"]["params"]["market_symbol"] == "KO"
    assert payload["task_recipe"]["selected_executor_kind"] == "configured_executor"
    assert "append a morning briefing" in payload["instruction"].lower()
    assert "photo-industry business news" in payload["instruction"].lower()
    assert payload["task_recipe"]["params"]["custom_guidance"].lower().startswith("keep it concise")


@pytest.mark.asyncio
async def test_create_task_accepts_briefing_market_symbol_from_editor(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskbriefingsymboluser",
            "email": "taskbriefingsymbol@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskbriefingsymboluser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Evening Market Briefing",
            "instruction": "Summarize my watchlist.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
            "recipe_family": "briefing",
            "recipe_params": {
                "briefing_mode": "evening",
                "market_symbol": "NVDA",
            },
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "briefing"
    assert payload["task_recipe"]["params"]["briefing_mode"] == "evening"
    assert payload["task_recipe"]["params"]["market_symbol"] == "NVDA"


@pytest.mark.asyncio
async def test_create_task_accepts_explicit_watcher_recipe_params_from_editor(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskwatchereditoruser",
            "email": "taskwatchereditor@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskwatchereditoruser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "NASA Artemis Watcher",
            "instruction": "Keep this focused on major launches and mission updates.",
            "task_type": "recurring",
            "schedule": "every:2h",
            "deliver": True,
            "recipe_family": "topic_watcher",
            "recipe_params": {
                "topic": "NASA + Artemis",
                "threshold": "high",
                "sources": ["NASA Breaking News", "SpaceNews"],
            },
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "topic_watcher"
    assert payload["task_recipe"]["params"]["topic"] == "NASA + Artemis"
    assert payload["task_recipe"]["params"]["threshold"] == "high"


@pytest.mark.asyncio
async def test_create_task_accepts_explicit_agent_recipe_family_from_editor(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskagenteditoruser",
            "email": "taskagenteditor@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskagenteditoruser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Roadmap Verification Agent",
            "instruction": "Review pending roadmap phases against the code and summarize any drift.",
            "task_type": "one_shot",
            "deliver": True,
            "recipe_family": "agent",
            "recipe_params": {
                "agent_role": "roadmap_verifier",
                "source_context_hint": "roadmap_coordination",
                "context_paths": [
                    "Docs/_internal/FruitcakeAi Roadmap.md",
                    "Docs/_internal/roadmap_coordination.md",
                ],
            },
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "agent"
    assert payload["task_recipe"]["params"]["agent_role"] == "roadmap_verifier"
    assert payload["task_recipe"]["params"]["source_context_hint"] == "roadmap_coordination"
    assert payload["task_recipe"]["params"]["context_paths"] == [
        "Docs/_internal/FruitcakeAi Roadmap.md",
        "Docs/_internal/roadmap_coordination.md",
    ]
    assert payload["profile"] is None
    assert payload["persona"] == "roadmap_verifier"
    assert payload["resolved_agent"]["id"] == "roadmap_verifier"
    assert payload["resolved_agent"]["display_name"] == "Roadmap Verifier"
    assert payload["resolved_agent"]["category"] == "verify"
    assert payload["resolved_agent"]["execution_mode"] == "task"
    assert payload["instruction"].startswith("Review pending roadmap phases")


@pytest.mark.asyncio
async def test_create_agent_task_preserves_explicit_persona_override(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskagentpersonauser",
            "email": "taskagentpersona@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskagentpersonauser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Roadmap Verification Agent",
            "instruction": "Review pending roadmap phases against the code and summarize any drift.",
            "task_type": "one_shot",
            "deliver": True,
            "persona": "family_assistant",
            "recipe_family": "agent",
            "recipe_params": {
                "agent_role": "roadmap_verifier",
            },
        },
        headers=headers,
    )

    assert created.status_code == 201
    assert created.json()["persona"] == "family_assistant"


@pytest.mark.asyncio
async def test_create_task_rejects_overlong_title_with_clear_validation_error(client):
    await client.post(
        "/auth/register",
        json={
            "username": "tasktitlelimituser",
            "email": "tasktitlelimit@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "tasktitlelimituser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "x" * 256,
            "instruction": "Keep the detailed prompt here.",
            "task_type": "one_shot",
            "deliver": True,
        },
        headers=headers,
    )

    assert created.status_code == 400
    payload = created.json()
    assert payload["error"] == "title must be 255 characters or fewer."


@pytest.mark.asyncio
async def test_agent_instances_ensure_defaults_creates_seeded_instances(client):
    await client.post(
        "/auth/register",
        json={
            "username": "managedpresetuser",
            "email": "managedpreset@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "managedpresetuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/tasks/agent-instances/ensure-defaults", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    by_name = {item["display_name"]: item for item in data}
    assert set(by_name.keys()) == {"Main Library Sync", "Primary Repo Map", "Run Health Check"}
    assert by_name["Main Library Sync"]["linked_task"]["title"] == "Main Library Sync"
    assert by_name["Primary Repo Map"]["linked_task"]["schedule"] == "every:1d"
    assert by_name["Run Health Check"]["params"]["max_runs"] == 8
    assert by_name["Run Health Check"]["category"] == "verify"


@pytest.mark.asyncio
async def test_agent_instances_ensure_defaults_cleans_up_legacy_duplicate_seed_rows(client):
    await client.post(
        "/auth/register",
        json={
            "username": "managedpresetlegacyuser",
            "email": "managedpresetlegacy@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "managedpresetlegacyuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    seeded = await client.post("/tasks/agent-instances/ensure-defaults", headers=headers)
    assert seeded.status_code == 200
    primary_repo_map = next(item for item in seeded.json() if item["display_name"] == "Primary Repo Map")

    async with TestSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "managedpresetlegacyuser"))
        ).scalar_one()
        legacy_task = Task(
            user_id=user.id,
            title="repo_map_manager",
            instruction="Legacy repo map",
            task_type="recurring",
            schedule="every:1d",
            status="pending",
            deliver=False,
            requires_approval=False,
        )
        db.add(legacy_task)
        await db.flush()
        db.add(
            ManagedAgentPreset(
                user_id=user.id,
                preset_id="repo_map_manager",
                display_name="repo_map_manager",
                enabled=True,
                auto_maintain_task=True,
                schedule="every:1d",
                active_hours_tz="UTC",
                context_paths_json="[]",
                params_json="{}",
                linked_task_id=legacy_task.id,
            )
        )
        await db.commit()

    refreshed = await client.post("/tasks/agent-instances/ensure-defaults", headers=headers)
    assert refreshed.status_code == 200
    payload = refreshed.json()
    repo_map_rows = [item for item in payload if item["preset_id"] == "repo_map_manager"]
    assert len(repo_map_rows) == 1
    assert repo_map_rows[0]["display_name"] == "Primary Repo Map"
    assert repo_map_rows[0]["id"] == primary_repo_map["id"]

    async with TestSessionLocal() as db:
        duplicate_rows = (
            await db.execute(
                select(Task).where(Task.title == "repo_map_manager")
            )
        ).scalars().all()
        assert duplicate_rows == []


@pytest.mark.asyncio
async def test_agent_instance_update_disables_backing_task_and_updates_params(client):
    await client.post(
        "/auth/register",
        json={
            "username": "managedpresetedituser",
            "email": "managedpresetedit@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "managedpresetedituser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post("/tasks/agent-instances/ensure-defaults", headers=headers)
    assert created.status_code == 200
    recent = next(item for item in created.json() if item["display_name"] == "Run Health Check")
    instance_id = recent["id"]

    patched = await client.patch(
        f"/tasks/agent-instances/{instance_id}",
        json={
            "display_name": "Run Health Deep Check",
            "enabled": False,
            "schedule": "every:12h",
            "params": {
                "lookback_hours": 48,
                "max_runs": 12,
                "problematic_only": False,
                "emit_all_clear": True,
            },
        },
        headers=headers,
    )
    assert patched.status_code == 200
    payload = patched.json()
    assert payload["enabled"] is False
    assert payload["display_name"] == "Run Health Deep Check"
    assert payload["schedule"] == "every:12h"
    assert payload["params"]["lookback_hours"] == 48
    assert payload["linked_task"]["status"] == "cancelled"
    assert payload["linked_task"]["next_run_at"] is None

    task_id = payload["linked_task"]["id"]
    task_resp = await client.get(f"/tasks/{task_id}", headers=headers)
    assert task_resp.status_code == 200
    task_payload = task_resp.json()
    assert task_payload["task_recipe"]["params"]["max_runs"] == 12
    assert task_payload["status"] == "cancelled"
    assert task_payload["next_run_at"] is None


@pytest.mark.asyncio
async def test_create_agent_instance_supports_multiple_instances_per_preset(client):
    await client.post(
        "/auth/register",
        json={
            "username": "agentinstancecreateuser",
            "email": "agentinstancecreate@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "agentinstancecreateuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    seeded = await client.post("/tasks/agent-instances/ensure-defaults", headers=headers)
    assert seeded.status_code == 200

    created = await client.post(
        "/tasks/agent-instances",
        json={
            "preset_id": "repo_map_manager",
            "display_name": "Client Repo Map",
            "schedule": "every:12h",
            "params": {
                "output_path": "reports/client_repo_map.md",
                "included_roots": ["/tmp/client-repo"],
                "ignored_paths": [".git", ".venv"],
                "refresh_after_sync_only": False,
            },
        },
        headers=headers,
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["display_name"] == "Client Repo Map"
    assert payload["preset_id"] == "repo_map_manager"
    assert payload["linked_task"]["title"] == "Client Repo Map"
    assert payload["params"]["included_roots"] == ["/tmp/client-repo"]

    listed = await client.get("/tasks/agent-instances", headers=headers)
    assert listed.status_code == 200
    names = {item["display_name"] for item in listed.json()}
    assert "Primary Repo Map" in names
    assert "Client Repo Map" in names


@pytest.mark.asyncio
async def test_agent_instance_model_override_syncs_to_backing_task(client):
    await client.post(
        "/auth/register",
        json={
            "username": "agentinstancemodeluser",
            "email": "agentinstancemodel@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "agentinstancemodeluser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks/agent-instances",
        json={
            "preset_id": "recent_run_analyzer",
            "display_name": "Model-Aware Run Health",
            "llm_model_override": "gpt-5.4-mini",
        },
        headers=headers,
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["llm_model_override"] == "gpt-5.4-mini"

    task_id = payload["linked_task"]["id"]
    task_resp = await client.get(f"/tasks/{task_id}", headers=headers)
    assert task_resp.status_code == 200
    assert task_resp.json()["llm_model_override"] == "gpt-5.4-mini"

    patched = await client.patch(
        f"/tasks/agent-instances/{payload['id']}",
        json={"llm_model_override": "gpt-5.4"},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["llm_model_override"] == "gpt-5.4"

    task_resp = await client.get(f"/tasks/{task_id}", headers=headers)
    assert task_resp.status_code == 200
    assert task_resp.json()["llm_model_override"] == "gpt-5.4"


@pytest.mark.asyncio
async def test_agent_instances_ensure_defaults_falls_back_to_existing_instances_on_error(client):
    await client.post(
        "/auth/register",
        json={
            "username": "agentinstancefallbackuser",
            "email": "agentinstancefallback@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "agentinstancefallbackuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    seeded = await client.post("/tasks/agent-instances/ensure-defaults", headers=headers)
    assert seeded.status_code == 200

    with patch("app.api.tasks.ensure_seed_agent_instances", new=AsyncMock(side_effect=RuntimeError("boom"))):
        fallback = await client.post("/tasks/agent-instances/ensure-defaults", headers=headers)

    assert fallback.status_code == 200
    payload = fallback.json()
    names = {item["display_name"] for item in payload}
    assert "Main Library Sync" in names
    assert "Primary Repo Map" in names


@pytest.mark.asyncio
async def test_create_task_respects_explicit_generic_family_from_editor(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskgenericeditoruser",
            "email": "taskgenericeditor@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskgenericeditoruser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Daily Photography Briefing",
            "instruction": "Append a daily research briefing about photography from the past 24 hours to workspace/photography/daily.md.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
            "recipe_family": "",
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["profile"] is None
    assert payload["task_recipe"] is None
    assert payload["instruction"].startswith("Append a daily research briefing")


@pytest.mark.asyncio
async def test_create_task_accepts_explicit_briefing_family_without_path_as_profile_briefing(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskbriefinginvaliduser",
            "email": "taskbriefinginvalid@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskbriefinginvaliduser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "US Politics Briefing",
            "instruction": "Keep me up to date on US politics.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
            "recipe_family": "briefing",
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "briefing"
    assert payload["task_recipe"]["params"]["briefing_mode"] == "morning"
    assert payload["profile"] == "briefing"
    assert payload["task_recipe"]["selected_executor_kind"] is None


@pytest.mark.asyncio
async def test_create_task_preserves_custom_guidance_for_morning_briefing(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskmorningeditoruser",
            "email": "taskmorningeditor@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskmorningeditoruser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Morning Briefing",
            "instruction": (
                "Prepare a morning briefing for today using my calendar and current headlines.\n"
                "Include today's schedule, notable headlines, and any important conflicts or priorities.\n"
                "Also include a short bit of trivia about this day in history."
            ),
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
            "recipe_family": "briefing",
            "recipe_params": {
                "briefing_mode": "morning",
            },
        },
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["task_recipe"]["family"] == "briefing"
    assert payload["task_recipe"]["params"]["briefing_mode"] == "morning"
    assert "day in history" in payload["instruction"].lower()
    guidance = payload["task_recipe"]["params"]["custom_guidance"].lower()
    assert guidance.startswith("include today's schedule")
    assert "also include a short bit of trivia" in guidance


@pytest.mark.asyncio
async def test_patch_task_can_clear_active_hours_and_recipe_family(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskpatchclearuser",
            "email": "taskpatchclear@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskpatchclearuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Morning local task",
            "instruction": "Watch Iran and Middle East news for major updates.",
            "task_type": "recurring",
            "schedule": "every:2h",
            "active_hours_start": "07:00",
            "active_hours_end": "22:00",
            "active_hours_tz": "America/New_York",
            "recipe_family": "topic_watcher",
        },
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    patched = await client.patch(
        f"/tasks/{task_id}",
        json={
            "active_hours_start": None,
            "active_hours_end": None,
            "active_hours_tz": None,
            "recipe_family": "",
            "recipe_params": None,
        },
        headers=headers,
    )

    assert patched.status_code == 200
    payload = patched.json()
    assert payload["active_hours_start"] is None
    assert payload["active_hours_end"] is None
    assert payload["active_hours_tz"] is None
    assert payload["profile"] is None
    assert payload["task_recipe"] is None


@pytest.mark.asyncio
async def test_patch_task_can_change_one_shot_to_recurring(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskpatchtypeuser",
            "email": "taskpatchtype@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskpatchtypeuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "One-time task",
            "instruction": "Do this once.",
            "task_type": "one_shot",
            "deliver": True,
        },
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    patched = await client.patch(
        f"/tasks/{task_id}",
        json={
            "task_type": "recurring",
            "schedule": "every:1d",
        },
        headers=headers,
    )

    assert patched.status_code == 200
    payload = patched.json()
    assert payload["task_type"] == "recurring"
    assert payload["schedule"] == "every:1d"
    assert payload["next_run_at"] is not None


@pytest.mark.asyncio
async def test_patch_task_can_repair_generic_task_into_daily_briefing(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskpatchbriefinguser",
            "email": "taskpatchbriefing@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskpatchbriefinguser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "US Politics Daily Briefing",
            "instruction": "Keep me updated on US politics.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "deliver": True,
        },
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    patched = await client.patch(
        f"/tasks/{task_id}",
        json={
            "title": "US Politics Daily Briefing",
            "instruction": "Focus on policy, elections, and congressional movement.",
            "recipe_family": "briefing",
            "recipe_params": {
                "briefing_mode": "morning",
                "topic": "US Politics",
                "path": "workspace/politics/US Politics.md",
                "window_hours": 24,
                "custom_guidance": "Focus on policy, elections, and congressional movement.",
            },
        },
        headers=headers,
    )

    assert patched.status_code == 200
    payload = patched.json()
    assert payload["task_recipe"]["family"] == "briefing"
    assert payload["task_recipe"]["params"]["briefing_mode"] == "morning"
    assert payload["task_recipe"]["params"]["topic"] == "US Politics"
    assert payload["task_recipe"]["params"]["path"] == "workspace/politics/US Politics.md"
    assert payload["task_recipe"]["params"]["custom_guidance"].lower().startswith("focus on policy")
    assert "congressional movement" in payload["instruction"].lower()


@pytest.mark.asyncio
async def test_create_task_uses_user_timezone_when_task_timezone_missing(client):
    await client.post(
        "/auth/register",
        json={
            "username": "usertzuser",
            "email": "usertz@example.com",
            "password": "pass123",
        },
    )
    async with TestSessionLocal() as db:
        rows = await db.execute(select(User).where(User.username == "usertzuser"))
        user = rows.scalar_one_or_none()
        assert user is not None
        user.active_hours_tz = "America/New_York"
        await db.commit()

    login = await client.post(
        "/auth/login",
        json={"username": "usertzuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Morning user tz task",
            "instruction": "Run every morning.",
            "task_type": "recurring",
            "schedule": "0 08 * * *",
        },
        headers=headers,
    )

    assert created.status_code == 201
    next_run = datetime.fromisoformat(created.json()["next_run_at"])
    next_run_local = next_run.astimezone(ZoneInfo("America/New_York"))
    assert next_run_local.hour == 8
    assert next_run_local.minute == 0
    assert created.json()["effective_timezone"] == "America/New_York"
    assert created.json()["next_run_at_localized"].endswith(("EDT", "EST"))


@pytest.mark.asyncio
async def test_create_task_falls_back_to_utc_for_invalid_timezone(client):
    await client.post(
        "/auth/register",
        json={
            "username": "badtzuser",
            "email": "badtz@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "badtzuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Fallback UTC task",
            "instruction": "Run in fallback UTC.",
            "task_type": "recurring",
            "schedule": "0 08 * * *",
            "active_hours_tz": "Mars/Olympus_Mons",
        },
        headers=headers,
    )

    assert created.status_code == 201
    next_run = datetime.fromisoformat(created.json()["next_run_at"])
    assert next_run.tzinfo == timezone.utc
    assert next_run.hour == 8
    assert next_run.minute == 0
    assert created.json()["effective_timezone"] == "UTC"
    assert created.json()["next_run_at_localized"].endswith("UTC")


def test_compute_next_run_at_preserves_local_hour_across_dst_boundary():
    after = datetime(2026, 3, 8, 11, 30, tzinfo=timezone.utc)

    next_run = compute_next_run_at(
        "0 08 * * *",
        after=after,
        task_timezone="America/New_York",
        user_timezone=None,
    )

    assert next_run is not None
    next_run_local = next_run.astimezone(ZoneInfo("America/New_York"))
    assert next_run_local.hour == 8
    assert next_run_local.minute == 0
    assert next_run_local.tzname() == "EDT"


@pytest.mark.asyncio
async def test_manual_run_rejects_when_task_has_active_run(client):
    await client.post(
        "/auth/register",
        json={
            "username": "taskrunconflictuser",
            "email": "taskrunconflict@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "taskrunconflictuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Conflict task",
            "instruction": "Do the thing.",
            "task_type": "one_shot",
        },
        headers=headers,
    )
    task_id = created.json()["id"]

    async with TestSessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        task.status = "pending"
        db.add(TaskRun(task_id=task_id, status="running"))
        await db.commit()

    resp = await client.post(f"/tasks/{task_id}/run", headers=headers)
    assert resp.status_code == 409
    assert "Task is already running" in resp.text


@pytest.mark.asyncio
async def test_manual_run_commits_immediate_queue_state_before_runner_executes(client):
    await client.post(
        "/auth/register",
        json={
            "username": "manualrunqueueuser",
            "email": "manualrunqueue@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": "manualrunqueueuser", "password": "pass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/tasks",
        json={
            "title": "Recurring agent task",
            "instruction": "Run immediately when asked.",
            "task_type": "recurring",
            "schedule": "every:1d",
            "recipe_family": "agent",
            "recipe_params": {"agent_role": "runtime_inspector"},
        },
        headers=headers,
    )
    assert created.status_code == 201
    task_id = int(created.json()["id"])

    observed: dict[str, object] = {}

    class FakeRunner:
        async def execute(self, task, *, trigger_source: str = "direct"):
            observed["trigger_source"] = trigger_source
            observed["passed_next_run_at"] = getattr(task, "next_run_at", None)
            async with TestSessionLocal() as db:
                persisted = await db.get(Task, int(task.id))
                observed["persisted_next_run_at"] = getattr(persisted, "next_run_at", None)
                observed["persisted_status"] = getattr(persisted, "status", None)

    with patch("app.autonomy.runner.get_task_runner", return_value=FakeRunner()):
        resp = await client.post(f"/tasks/{task_id}/run", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    assert observed["trigger_source"] == "manual"
    assert observed["persisted_status"] == "pending"
    assert observed["passed_next_run_at"] is not None
    assert observed["persisted_next_run_at"] == observed["passed_next_run_at"]
