"""
Memory v2 write-pipeline tests.

Covers the single enforced write path (MemoryService.propose_write):
- exclusion rules refuse task/session-state content with a reason
- near-duplicate writes dedupe against the existing head (text fallback path)
- subject_key conflicts supersede: new head, old row deactivated + chained
- directive cap refuses new directives but allows superseding ones
- kind mapping from legacy memory_type, and the create() compat wrapper
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models import Memory, User
from app.memory.service import (
    KIND_FROM_MEMORY_TYPE,
    MemoryService,
    _normalize_subject_key,
)
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


def test_kind_mapping_and_subject_key_normalization():
    assert KIND_FROM_MEMORY_TYPE == {
        "procedural": "directive",
        "semantic": "fact",
        "episodic": "journal",
    }
    assert _normalize_subject_key("Joey", "Allergy") == "joey:allergy"
    assert _normalize_subject_key("Emma's School", "name") == "emma_s_school:name"
    assert _normalize_subject_key("Joey", None) is None
    assert _normalize_subject_key(None, "allergy") is None


@pytest.mark.asyncio
async def test_write_creates_fact_with_kind_and_source():
    user_id = await _make_user("memv2create")
    svc = MemoryService()
    async with TestSessionLocal() as db:
        result = await svc.propose_write(
            db,
            user_id,
            content="Joey is allergic to peanuts.",
            memory_type="semantic",
            subject="Joey",
            attribute="allergy",
            source="chat_tool",
        )
        await db.commit()
        assert result.action == "created"
        assert result.memory.kind == "fact"
        assert result.memory.subject_key == "joey:allergy"
        assert result.memory.source == "chat_tool"
        assert result.memory.memory_type == "semantic"  # legacy compat


@pytest.mark.asyncio
async def test_near_duplicate_dedupes_and_bumps_importance():
    user_id = await _make_user("memv2dedup")
    svc = MemoryService()
    async with TestSessionLocal() as db:
        first = await svc.propose_write(
            db, user_id, content="The family dog is named Biscuit.", memory_type="semantic",
            importance=0.4,
        )
        await db.commit()
        second = await svc.propose_write(
            db, user_id, content="The family dog is named Biscuit!", memory_type="semantic",
            importance=0.8,
        )
        await db.commit()

        assert second.action == "deduplicated"
        assert second.memory.id == first.memory.id
        assert second.memory.importance == 0.8

        rows = await db.execute(select(Memory).where(Memory.user_id == user_id))
        assert len(rows.scalars().all()) == 1


@pytest.mark.asyncio
async def test_subject_conflict_supersedes_old_head():
    user_id = await _make_user("memv2conflict")
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

        assert new.action == "superseded"
        old_row = await db.get(Memory, old.memory.id)
        new_row = await db.get(Memory, new.memory.id)
        assert old_row.is_active is False
        assert old_row.superseded_by_id == new_row.id
        assert new_row.is_active is True
        assert new_row.superseded_by_id is None

        # Only one retrievable head for the subject_key
        heads = await db.execute(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.subject_key == "joey:allergy",
                Memory.is_active == True,
            )
        )
        assert [m.id for m in heads.scalars().all()] == [new_row.id]


@pytest.mark.asyncio
async def test_exclusion_rules_refuse_task_state_with_reason():
    user_id = await _make_user("memv2exclude")
    svc = MemoryService()
    async with TestSessionLocal() as db:
        result = await svc.propose_write(
            db, user_id, content="Currently working on step 2 of the RSS refactor.",
            memory_type="episodic",
        )
        assert result.action == "refused"
        assert "task" in result.reason.lower() or "session" in result.reason.lower()

        short = await svc.propose_write(db, user_id, content="ok", memory_type="semantic")
        assert short.action == "refused"


@pytest.mark.asyncio
async def test_directive_cap_refuses_but_allows_supersede(monkeypatch):
    monkeypatch.setattr(settings, "memory_directive_cap", 2)
    user_id = await _make_user("memv2cap")
    svc = MemoryService()
    async with TestSessionLocal() as db:
        first = await svc.propose_write(
            db, user_id, content="Always use metric units in answers.", memory_type="procedural",
            subject="household", attribute="units",
        )
        second = await svc.propose_write(
            db, user_id, content="Never schedule events before 9am.", memory_type="procedural",
        )
        await db.commit()
        assert first.action == "created"
        assert second.action == "created"

        third = await svc.propose_write(
            db, user_id, content="Always answer in formal tone for work questions.",
            memory_type="procedural",
        )
        assert third.action == "refused"
        assert "cap" in third.reason.lower()

        # Superseding an existing directive stays allowed at the cap.
        replacing = await svc.propose_write(
            db, user_id, content="Always use imperial units in answers.", memory_type="procedural",
            subject="household", attribute="units",
        )
        await db.commit()
        assert replacing.action == "superseded"


@pytest.mark.asyncio
async def test_create_compat_wrapper_routes_through_pipeline():
    user_id = await _make_user("memv2compat")
    svc = MemoryService()
    async with TestSessionLocal() as db:
        created = await svc.create(
            db, user_id, memory_type="episodic",
            content="In-laws visiting the weekend of July 12.",
        )
        await db.commit()
        assert isinstance(created, Memory)
        assert created.kind == "journal"

        refused = await svc.create(
            db, user_id, memory_type="episodic",
            content="Currently working on packing for the trip.",
        )
        assert isinstance(refused, str)
        assert "task" in refused.lower() or "session" in refused.lower()
