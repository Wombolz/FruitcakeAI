import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.agent.context import UserContext
from app.agent.tools import get_tools_for_user
from app.config import settings
from app.db.models import Skill
from app.skills.bundles import validate_files
from app.skills.bundle_service import read_skill_file
from app.skills.service import get_skill_service, hydrate_user_context
from tests.conftest import TestSessionLocal
from tests.test_skills import _register_admin_token


def bundle(body="Follow the model defaults. Final instruction must survive."):
    return {"SKILL.md": "---\nname: Image Helper\ndescription: Generate images using tuned workflows and image references.\nrequired_tools: [generate_image]\n---\n" + body + "\nRead [settings](./references/settings.md).",
            "references/settings.md": "Reference rule. " * 280}


@pytest.fixture(autouse=True)
def offline_skill_validation(monkeypatch):
    monkeypatch.setattr(get_skill_service(), "_known_tool_names", AsyncMock(return_value={"generate_image"}))
    monkeypatch.setattr("app.skills.service._embed", AsyncMock(return_value=None))


async def admin_headers(client, username="bundleadmin"):
    token = await _register_admin_token(client, username)
    return {"Authorization": f"Bearer {token}"}


async def install(client, headers, files=None, scope="shared"):
    body = {"files": files or bundle(), "scope": scope}
    preview = await client.post("/admin/skill-bundles/preview", json=body, headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_install"], preview.text
    result = await client.post("/admin/skill-bundles/install", json={**body, "preview_hash": preview.json()["preview_hash"]}, headers=headers)
    assert result.status_code == 201, result.text
    return result.json()["id"]


@pytest.mark.parametrize("bad_path", ["../secret.md", "/secret.md", "references/../secret.md", "x//secret.md", "x\\secret.md", "file:secret.md", "script.py"])
def test_bundle_rejects_paths_and_executable_files(bad_path):
    with pytest.raises(ValueError):
        validate_files({"SKILL.md": "main", bad_path: "bad"})


@pytest.mark.parametrize("files", [
    {"reference.md": "no main"}, {"SKILL.md": "x" * 12001},
    {"SKILL.md": "main", "reference.md": "x" * 64001},
    {"SKILL.md": "[missing](references/absent.md)"},
    {"SKILL.md": "[escape](../outside.md)"},
    {"SKILL.md": "[escape](/outside.md)"}, {"SKILL.md": "bad\0text"},
])
def test_invalid_bundles_are_rejected(files):
    with pytest.raises(ValueError):
        validate_files(files)


@pytest.mark.asyncio
async def test_preview_freezes_entire_bundle_and_hash_binds_every_file(client):
    headers = await admin_headers(client)
    files = bundle()
    response = await client.post("/admin/skill-bundles/preview", json={"files": files}, headers=headers)
    preview = response.json()
    assert preview["can_install"] and len(preview["file_paths"]) == 2
    files["references/settings.md"] = "changed after preview"
    stale = await client.post("/admin/skill-bundles/install", json={"files": files, "preview_hash": preview["preview_hash"]}, headers=headers)
    assert stale.status_code == 409
    assert (await client.get("/admin/skills", headers=headers)).json() == []


@pytest.mark.asyncio
async def test_install_list_inspect_disable_remove_and_source_is_not_live(client):
    headers = await admin_headers(client)
    files = bundle()
    skill_id = await install(client, headers, files)
    original = dict(files)
    files["references/settings.md"] = "external edit"
    detail = await client.get(f"/admin/skill-bundles/{skill_id}", headers=headers)
    assert detail.json()["files"] == original
    listing = (await client.get("/admin/skills", headers=headers)).json()
    assert listing[0]["bundle_file_count"] == 2
    assert (await client.patch(f"/admin/skills/{skill_id}", headers=headers, json={"is_active": False})).json()["is_active"] is False
    assert (await client.delete(f"/admin/skills/{skill_id}", headers=headers)).status_code == 204
    assert (await client.get(f"/admin/skill-bundles/{skill_id}", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_missing_tools_are_visible_and_install_is_blocked(client, monkeypatch):
    monkeypatch.setattr(get_skill_service(), "_known_tool_names", AsyncMock(return_value=set()))
    headers = await admin_headers(client)
    response = await client.post("/admin/skill-bundles/preview", json={"files": bundle()}, headers=headers)
    preview = response.json()
    assert preview["missing_tools"] == ["generate_image"] and not preview["can_install"]
    result = await client.post("/admin/skill-bundles/install", json={"files": bundle(), "preview_hash": preview["preview_hash"]}, headers=headers)
    assert result.status_code == 400


@pytest.mark.asyncio
async def test_admin_boundary_applies_to_preview_import_and_document_inspection(client):
    headers = await admin_headers(client)
    skill_id = await install(client, headers)
    await client.post("/auth/register", json={"username": "ordinary", "email": "ordinary@example.com", "password": "testpass123"})
    token = (await client.post("/auth/login", json={"username": "ordinary", "password": "testpass123"})).json()["access_token"]
    ordinary = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/admin/skill-bundles/preview", json={"files": bundle()}, headers=ordinary)).status_code == 403
    assert (await client.post("/admin/skill-bundles/install", json={"files": bundle(), "preview_hash": "x"}, headers=ordinary)).status_code == 403
    assert (await client.get(f"/admin/skill-bundles/{skill_id}", headers=ordinary)).status_code == 403


@pytest.mark.asyncio
async def test_main_instructions_survive_old_250_token_budget_and_references_are_paged(client):
    headers = await admin_headers(client)
    body = "Detailed instruction. " * 400 + "END_OF_COMPLETE_MAIN"
    skill_id = await install(client, headers, bundle(body))
    user = UserContext(user_id=1, username="bundleadmin", role="admin")
    async with TestSessionLocal() as db:
        await hydrate_user_context(db, user, query="Generate an image")
        assert user.active_skill_ids == [skill_id]
        assert "END_OF_COMPLETE_MAIN" in user.skill_prompt_additions[0]
        assert "truncated" not in user.skill_prompt_additions[0]
        assert "read_skill_file" in user.skill_prompt_additions[0]
        content = ""
        offset = 0
        while True:
            page = await read_skill_file(db, user, skill_id=skill_id, path="references/settings.md", offset=offset)
            assert len(json.dumps(page, ensure_ascii=False)) < 4000
            content += page["content"]
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        assert content == bundle()["references/settings.md"]
    with patch("app.mcp.registry.get_mcp_registry", return_value=SimpleNamespace(_is_ready=False)):
        assert "read_skill_file" in {t["function"]["name"] for t in get_tools_for_user(user)}
        user.blocked_tools = ["read_skill_file"]
        assert "read_skill_file" not in {t["function"]["name"] for t in get_tools_for_user(user)}


@pytest.mark.asyncio
async def test_private_unselected_disabled_and_external_documents_cannot_be_read(client):
    headers = await admin_headers(client)
    skill_id = await install(client, headers, scope="personal")
    user = UserContext(user_id=1, username="bundleadmin", role="admin")
    async with TestSessionLocal() as db:
        with pytest.raises(PermissionError):
            await read_skill_file(db, user, skill_id=skill_id, path="SKILL.md")
        user.active_skill_ids = [skill_id]
        assert (await read_skill_file(db, user, skill_id=skill_id, path="./references/settings.md"))["path"] == "references/settings.md"
        with pytest.raises(ValueError):
            await read_skill_file(db, user, skill_id=skill_id, path="/etc/passwd")
        with pytest.raises(ValueError):
            await read_skill_file(db, user, skill_id=skill_id, path="../SKILL.md")
        with pytest.raises(ValueError):
            await read_skill_file(db, user, skill_id=skill_id, path="SKILL.md", offset=-1)
        other = UserContext(user_id=2, username="other", role="parent", active_skill_ids=[skill_id])
        with pytest.raises(LookupError):
            await read_skill_file(db, other, skill_id=skill_id, path="SKILL.md")
        row = await db.get(Skill, skill_id)
        row.is_active = False
        await db.flush()
        with pytest.raises(LookupError):
            await read_skill_file(db, user, skill_id=skill_id, path="SKILL.md")


@pytest.mark.asyncio
async def test_small_configured_budget_blocks_install_instead_of_truncating(client, monkeypatch):
    monkeypatch.setattr(settings, "skills_bundle_max_tokens", 10)
    headers = await admin_headers(client)
    response = await client.post("/admin/skill-bundles/preview", json={"files": bundle()}, headers=headers)
    assert not response.json()["can_install"]
    assert any("budget" in warning for warning in response.json()["warnings"])


@pytest.mark.asyncio
async def test_repeated_identical_import_is_idempotent_and_old_versions_report_conflict(client):
    headers = await admin_headers(client)
    first = await install(client, headers)
    assert await install(client, headers) == first
    second = await install(client, headers, bundle("Updated main instructions"))
    assert second != first
    result = await client.patch(f"/admin/skills/{first}", headers=headers, json={"is_active": True})
    assert result.status_code == 409
    rows = (await client.get("/admin/skills", headers=headers)).json()
    assert len(rows) == 2
    assert [row["id"] for row in rows if row["is_active"]] == [second]


@pytest.mark.asyncio
async def test_empty_embedding_on_install_still_allows_later_matching(client, monkeypatch):
    headers = await admin_headers(client)
    skill_id = await install(client, headers)
    async def embedding(text):
        return [0.0, 1.0] if "weather" in text else [1.0, 0.0]
    monkeypatch.setattr("app.skills.service._embed", embedding)
    async with TestSessionLocal() as db:
        decisions = await get_skill_service().explain_injection(db, user_id=1, query="generate an image")
        assert decisions[0].skill_id == skill_id and decisions[0].included
        assert (await db.get(Skill, skill_id)).description_embedding_vector == [1.0, 0.0]
        weather = await get_skill_service().explain_injection(db, user_id=1, query="what is the weather")
        assert not weather[0].included


@pytest.mark.asyncio
async def test_json_request_limit_precedes_json_decoding(client):
    response = await client.post("/admin/skill-bundles/preview", content=b"x" * 2_000_001, headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_reference_collection_and_total_bytes_are_bounded():
    with pytest.raises(ValueError):
        validate_files({"SKILL.md": "main", **{f"r{i}.md": "x" for i in range(32)}})
    with pytest.raises(ValueError):
        validate_files({"SKILL.md": "main", **{f"r{i}.md": "x" * 64_000 for i in range(9)}})


def test_reader_is_known_to_overrides_and_task_profiles_but_hidden_until_selected():
    from app.api.chat import _apply_tool_overrides
    from app.autonomy.execution_profile import resolve_execution_profile
    user = UserContext(user_id=1, username="owner", role="admin")
    with patch("app.mcp.registry.get_mcp_registry", return_value=SimpleNamespace(_is_ready=False)):
        assert "read_skill_file" not in {t["function"]["name"] for t in get_tools_for_user(user)}
        _apply_tool_overrides(user, allowed_tools=None, blocked_tools=["read_skill_file"])
        assert "read_skill_file" in user.blocked_tools
        with patch("app.autonomy.execution_profile.UserContext.from_user", return_value=UserContext(user_id=1, username="owner", role="admin")):
            resolved = resolve_execution_profile(SimpleNamespace(persona="family_assistant"), SimpleNamespace(persona="family_assistant"))
        assert "read_skill_file" in resolved.allowed_tools


@pytest.mark.asyncio
async def test_reader_pages_do_not_expand_quotes_past_history_limit(client):
    from app.agent.tools import _call_tool
    headers = await admin_headers(client)
    files = bundle()
    files["references/settings.md"] = '"\n' * 5000
    skill_id = await install(client, headers, files)
    user = UserContext(user_id=1, username="bundleadmin", role="admin", active_skill_ids=[skill_id])
    with patch("app.db.session.AsyncSessionLocal", TestSessionLocal):
        result = await _call_tool("read_skill_file", {"skill_id": skill_id, "path": "references/settings.md"}, user)
    assert len(result) < settings.agent_tool_result_max_chars
    assert result.structured_content["next_offset"] == 3000
    assert result.endswith(files["references/settings.md"][:3000])
