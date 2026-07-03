"""
Memory v2 budgeted-retrieval tests.

Covers the retrieval inversion (retrieve_for_context):
- injection respects the token budget as the store grows
- directives are always included; facts/journal fill the remainder by rank
- superseded heads are never retrieved
- journal entries decay: fresher beats older at equal importance/relevance
- the chat retrieval query includes recent user turns, not just the latest
- recalled memory ids ride assistant message metadata end-to-end (REST)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models import Memory, User
from app.memory.service import MemoryService, _estimate_tokens
from tests.conftest import TestSessionLocal


async def _make_user(username: str) -> int:
    async with TestSessionLocal() as db:
        user = User(
            username=username,
            email=f"{username}@example.com",
            hashed_password="x",
            role="parent",
            persona="family_assistant",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id


async def _seed(
    user_id: int,
    content: str,
    *,
    kind: str = "fact",
    importance: float = 0.5,
    created_at: datetime | None = None,
    superseded_by_id: int | None = None,
    is_active: bool = True,
) -> int:
    async with TestSessionLocal() as db:
        memory = Memory(
            user_id=user_id,
            memory_type={"directive": "procedural", "fact": "semantic", "journal": "episodic"}[kind],
            kind=kind,
            content=content,
            importance=importance,
            tags="[]",
            is_active=is_active,
            superseded_by_id=superseded_by_id,
            source="manual",
        )
        db.add(memory)
        await db.commit()
        await db.refresh(memory)
        if created_at is not None:
            memory.created_at = created_at
            await db.commit()
        return memory.id


@pytest.mark.asyncio
async def test_retrieval_respects_token_budget_as_store_grows():
    user_id = await _make_user("membudget")
    for i in range(60):
        content = (
            f"The household keeps a detailed record of appliance warranty details for item {i}, "
            "including purchase date, provider contact, and coverage terms for future reference."
        )
        await _seed(user_id, content, importance=0.6)

    svc = MemoryService()
    async with TestSessionLocal() as db:
        results = await svc.retrieve_for_context(db, user_id, query="warranty details", token_budget=500)

    assert 0 < len(results) < 60
    non_directive_tokens = sum(_estimate_tokens(m.content) for m in results if m.kind != "directive")
    assert non_directive_tokens <= 500


@pytest.mark.asyncio
async def test_directives_always_included_facts_fill_remainder():
    user_id = await _make_user("memdirectives")
    d1 = await _seed(user_id, "Always use metric units when answering.", kind="directive")
    d2 = await _seed(user_id, "Never schedule anything before 9am.", kind="directive")
    for i in range(30):
        await _seed(user_id, f"Fact about household inventory item number {i} with extra detail text.", importance=0.5)

    svc = MemoryService()
    async with TestSessionLocal() as db:
        results = await svc.retrieve_for_context(db, user_id, query="inventory", token_budget=300)

    ids = [m.id for m in results]
    assert d1 in ids and d2 in ids
    assert results[0].kind == "directive"
    assert any(m.kind == "fact" for m in results)


@pytest.mark.asyncio
async def test_superseded_heads_are_never_retrieved():
    user_id = await _make_user("memheads")
    svc = MemoryService()
    async with TestSessionLocal() as db:
        old = await svc.propose_write(
            db, user_id, content="Joey is allergic to peanuts.", memory_type="semantic",
            subject="Joey", attribute="allergy",
        )
        await db.commit()
        new = await svc.propose_write(
            db, user_id, content="Joey is allergic to tree nuts, not peanuts.", memory_type="semantic",
            subject="Joey", attribute="allergy",
        )
        await db.commit()

        results = await svc.retrieve_for_context(db, user_id, query="what is joey allergic to")
        ids = [m.id for m in results]
        assert new.memory.id in ids
        assert old.memory.id not in ids


@pytest.mark.asyncio
async def test_journal_decay_prefers_fresh_events():
    user_id = await _make_user("memdecay")
    now = datetime.now(timezone.utc)
    stale = await _seed(
        user_id, "Family trip planning discussion happened.", kind="journal",
        importance=0.6, created_at=now - timedelta(days=90),
    )
    fresh = await _seed(
        user_id, "Family trip planning meeting scheduled soon.", kind="journal",
        importance=0.6, created_at=now - timedelta(days=1),
    )

    svc = MemoryService()
    async with TestSessionLocal() as db:
        # Budget sized to fit exactly one journal entry
        results = await svc.retrieve_for_context(
            db, user_id, query="family trip planning",
            token_budget=_estimate_tokens("Family trip planning meeting scheduled soon.") + 200,
        )
    journal_ids = [m.id for m in results if m.kind == "journal"]
    assert fresh in journal_ids
    assert stale not in journal_ids or journal_ids.index(fresh) < journal_ids.index(stale)


def test_memory_retrieval_query_includes_recent_user_turns():
    from app.api.chat import _memory_retrieval_query

    history = [
        {"role": "user", "content": "Can you help me plan Joey's birthday party?"},
        {"role": "assistant", "content": "Of course — when is it?"},
        {"role": "user", "content": "What about Tuesday?"},
    ]
    query = _memory_retrieval_query(history, "What about Tuesday?")
    assert "birthday party" in query
    assert query.strip().endswith("What about Tuesday?")


@pytest.mark.asyncio
async def test_recalled_memory_ids_ride_assistant_metadata(client):
    reg = await client.post(
        "/auth/register",
        json={"username": "memmetauser", "email": "memmetauser@example.com", "password": "pass123"},
    )
    login = await client.post("/auth/login", json={"username": "memmetauser", "password": "pass123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = await client.get("/auth/me", headers=headers)
    user_id = me.json()["id"]

    memory_id = await _seed(user_id, "This user prefers bullet-point summaries in answers.")

    session = await client.post("/chat/sessions", headers=headers)
    session_id = session.json()["id"]

    async def _fake_run_agent(messages, user_context, mode="chat", model_override=None, stage=None, **kwargs):
        return "reply"

    with patch("app.api.chat.run_agent", new=AsyncMock(side_effect=_fake_run_agent)):
        resp = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={"content": "How should you format answers for me?"},
            headers=headers,
        )

    assert resp.status_code == 200
    metadata = resp.json().get("metadata") or {}
    assert memory_id in (metadata.get("recalled_memory_ids") or [])

    # And it survives the persisted round-trip
    async with TestSessionLocal() as db:
        from app.db.models import ChatMessage

        rows = await db.execute(
            select(ChatMessage).where(
                ChatMessage.session_id == session_id, ChatMessage.role == "assistant"
            )
        )
        stored = rows.scalars().all()
    assert stored and "recalled_memory_ids" in (stored[-1].tool_results or "")
