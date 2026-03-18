from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.agent.context import UserContext
from app.db.models import User
from app.skills.service import get_skill_service, hydrate_user_context
from tests.conftest import TestSessionLocal


def _skill_markdown(*, name: str = "RSS Curator", slug: str = "rss-curator", scope: str = "shared", pinned: bool = False, tools: list[str] | None = None) -> str:
    tools = tools or []
    pinned_line = "pinned: true\n" if pinned else ""
    return (
        "---\n"
        f"name: {name}\n"
        f"slug: {slug}\n"
        "description: Helps curate RSS feeds and summarize news coverage accurately.\n"
        f"scope: {scope}\n"
        f"required_tools: {tools}\n"
        f"{pinned_line}"
        "---\n"
        "When working on RSS or news tasks, prioritize recency, source credibility, and direct links.\n"
    )


async def _register_admin_token(client, username: str) -> str:
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "pass123",
            "role": "admin",
        },
    )
    login = await client.post("/auth/login", json={"username": username, "password": "pass123"})
    return login.json()["access_token"]


@pytest.mark.asyncio
async def test_skill_preview_parses_frontmatter(client):
    token = await _register_admin_token(client, "skilladmin")
    resp = await client.post(
        "/admin/skills/preview",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": _skill_markdown()},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "rss-curator"
    assert data["allowed_tool_additions"] == []
    assert data["preview_hash"]


@pytest.mark.asyncio
async def test_skill_preview_fetches_url_once(client):
    token = await _register_admin_token(client, "urladmin")
    with patch.object(get_skill_service(), "fetch_preview_content", new=AsyncMock(return_value=_skill_markdown())) as mock_fetch:
        resp = await client.post(
            "/admin/skills/preview",
            headers={"Authorization": f"Bearer {token}"},
            json={"source_url": "https://github.com/example/skill/SKILL.md"},
        )
    assert resp.status_code == 200
    mock_fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_skill_install_and_list(client):
    token = await _register_admin_token(client, "installadmin")
    headers = {"Authorization": f"Bearer {token}"}
    preview = await client.post("/admin/skills/preview", headers=headers, json={"content": _skill_markdown()})
    assert preview.status_code == 200

    install = await client.post("/admin/skills/install", headers=headers, json=preview.json())
    assert install.status_code == 201
    data = install.json()
    assert data["slug"] == "rss-curator"
    assert data["content_hash"]

    listing = await client.get("/admin/skills", headers=headers)
    assert listing.status_code == 200
    assert listing.json()[0]["slug"] == "rss-curator"


@pytest.mark.asyncio
async def test_skill_reinstall_supersedes_previous_active_record(client):
    token = await _register_admin_token(client, "dupeadmin")
    headers = {"Authorization": f"Bearer {token}"}
    preview = await client.post("/admin/skills/preview", headers=headers, json={"content": _skill_markdown()})
    body = preview.json()
    first = await client.post("/admin/skills/install", headers=headers, json=body)
    assert first.status_code == 201
    dupe = await client.post("/admin/skills/install", headers=headers, json=body)
    assert dupe.status_code == 201

    listing = await client.get("/admin/skills", headers=headers)
    rows = [row for row in listing.json() if row["slug"] == "rss-curator"]
    active_rows = [row for row in rows if row["is_active"]]
    inactive_rows = [row for row in rows if not row["is_active"]]
    assert len(active_rows) == 1
    assert len(inactive_rows) >= 1
    assert active_rows[0]["supersedes_skill_id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_skill_hard_delete_removes_targeted_record(client):
    token = await _register_admin_token(client, "deleteadmin")
    headers = {"Authorization": f"Bearer {token}"}
    preview = await client.post("/admin/skills/preview", headers=headers, json={"content": _skill_markdown()})
    installed = await client.post("/admin/skills/install", headers=headers, json=preview.json())
    skill_id = installed.json()["id"]

    deleted = await client.delete(f"/admin/skills/{skill_id}", headers=headers)
    assert deleted.status_code == 204

    listing = await client.get("/admin/skills", headers=headers)
    assert all(row["id"] != skill_id for row in listing.json())


@pytest.mark.asyncio
async def test_personal_skill_only_visible_to_owner():
    service = get_skill_service()
    async with TestSessionLocal() as db:
        owner = User(username="owner", email="owner@test.local", hashed_password="x", role="parent", persona="family_assistant")
        other = User(username="other", email="other@test.local", hashed_password="x", role="parent", persona="family_assistant")
        db.add_all([owner, other])
        await db.flush()
        preview = service.parse_markdown(_skill_markdown(scope="personal", pinned=True), personal_user_id=owner.id)
        await service.install_preview(db, preview=preview, installed_by=owner.id)
        await db.commit()

        owner_decisions = await service.explain_injection(db, user_id=owner.id, query="show me RSS news")
        other_decisions = await service.explain_injection(db, user_id=other.id, query="show me RSS news")

    assert owner_decisions and owner_decisions[0].included is True
    assert other_decisions == []


@pytest.mark.asyncio
async def test_empty_query_only_injects_pinned_skill():
    service = get_skill_service()
    async with TestSessionLocal() as db:
        user = User(username="emptyq", email="emptyq@test.local", hashed_password="x", role="parent", persona="family_assistant")
        db.add(user)
        await db.flush()
        pinned = service.parse_markdown(_skill_markdown(name="Pinned RSS", slug="pinned-rss", pinned=True))
        regular = service.parse_markdown(_skill_markdown(name="Regular RSS", slug="regular-rss"))
        await service.install_preview(db, preview=pinned, installed_by=user.id)
        await service.install_preview(db, preview=regular, installed_by=user.id)
        await db.commit()
        decisions = await service.explain_injection(db, user_id=user.id, query="")

    included = {d.slug for d in decisions if d.included}
    assert included == {"pinned-rss"}


@pytest.mark.asyncio
async def test_embedding_unavailable_uses_pinned_only_fallback():
    service = get_skill_service()
    async with TestSessionLocal() as db:
        user = User(username="fallback", email="fallback@test.local", hashed_password="x", role="parent", persona="family_assistant")
        db.add(user)
        await db.flush()
        pinned = service.parse_markdown(_skill_markdown(name="Pinned RSS", slug="pinned-rss", pinned=True))
        regular = service.parse_markdown(_skill_markdown(name="Regular RSS", slug="regular-rss"))
        await service.install_preview(db, preview=pinned, installed_by=user.id)
        await service.install_preview(db, preview=regular, installed_by=user.id)
        await db.commit()
        with patch("app.skills.service._embed", new=AsyncMock(return_value=None)):
            decisions = await service.explain_injection(db, user_id=user.id, query="rss updates")

    included = {d.slug for d in decisions if d.included}
    assert included == {"pinned-rss"}
    assert all(d.selection_mode == "pinned_only" for d in decisions)


@pytest.mark.asyncio
async def test_chat_injects_relevant_skill_into_user_context(client):
    token = await _register_admin_token(client, "chatskill")
    headers = {"Authorization": f"Bearer {token}"}
    preview = await client.post("/admin/skills/preview", headers=headers, json={"content": _skill_markdown(pinned=True)})
    assert preview.status_code == 200
    assert (await client.post("/admin/skills/install", headers=headers, json=preview.json())).status_code == 201

    create = await client.post("/chat/sessions", headers=headers, json={"title": "Skills"})
    session_id = create.json()["id"]

    with patch("app.api.chat.run_agent", new_callable=AsyncMock, return_value="ok") as mock_run:
        sent = await client.post(
            f"/chat/sessions/{session_id}/messages",
            headers=headers,
            json={"content": "Summarize RSS news feeds and coverage"},
        )
    assert sent.status_code == 200
    metadata = sent.json()["metadata"]
    user_context = mock_run.await_args.args[1]
    assert user_context.active_skill_slugs == ["rss-curator"]
    assert user_context.skill_prompt_additions
    assert metadata["active_skills"] == ["rss-curator"]
    assert metadata["skill_selection_mode"] in {"embedding", "pinned_only"}


@pytest.mark.asyncio
async def test_restricted_persona_skill_grant_cannot_restore_blocked_tool():
    service = get_skill_service()
    async with TestSessionLocal() as db:
        user = User(username="restricted", email="restricted@test.local", hashed_password="x", role="restricted", persona="restricted_assistant")
        db.add(user)
        await db.flush()
        preview = service.parse_markdown(_skill_markdown(tools=["web_search"]))
        await service.install_preview(db, preview=preview, installed_by=user.id)
        await db.commit()

        ctx = UserContext.from_user(user)
        hydrated = await hydrate_user_context(db, ctx, query="latest RSS news")

    assert "web_search" not in hydrated.skill_granted_tools


@pytest.mark.asyncio
async def test_preview_injection_endpoint_explains_reason(client):
    token = await _register_admin_token(client, "diagadmin")
    headers = {"Authorization": f"Bearer {token}"}
    preview = await client.post("/admin/skills/preview", headers=headers, json={"content": _skill_markdown()})
    installed = await client.post("/admin/skills/install", headers=headers, json=preview.json())
    skill_id = installed.json()["id"]

    resp = await client.get(
        f"/admin/skills/{skill_id}/preview-injection",
        headers=headers,
        params={"query": "calendar appointment details"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reason"] in {"below_similarity_threshold", "relevant", "embedding_unavailable_not_pinned", "pinned_only_fallback"}
    assert data["selection_mode"] in {"embedding", "pinned_only"}


@pytest.mark.asyncio
async def test_webhook_trigger_returns_skill_metadata(client):
    token = await _register_admin_token(client, "webhookskill")
    headers = {"Authorization": f"Bearer {token}"}
    preview = await client.post("/admin/skills/preview", headers=headers, json={"content": _skill_markdown(pinned=True)})
    assert (await client.post("/admin/skills/install", headers=headers, json=preview.json())).status_code == 201

    created = await client.post(
        "/webhooks",
        headers=headers,
        json={"name": "RSS hook", "instruction": "Summarize RSS news", "active": True},
    )
    webhook_key = created.json()["webhook_key"]
    with patch("app.api.webhooks._execute_webhook", new=AsyncMock(return_value=None)):
        triggered = await client.post(f"/webhooks/trigger/{webhook_key}", json={"event": "test"})
    assert triggered.status_code == 202
    data = triggered.json()
    assert "metadata" in data
    assert data["metadata"]["active_skills"] == ["rss-curator"]
