"""
Nightly memory-extraction tests (memory v2 Day 4).

- candidates flow into the MemoryProposal review queue
- narrow auto-approval: high-confidence facts write through the pipeline;
  directives and would-supersede candidates stay pending with a reason
- incognito sessions are never read by extraction
- re-running extraction does not duplicate proposals
- admin endpoint gates on admin role
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.models import ChatMessage, ChatSession, Memory, MemoryProposal, User
from app.memory.extraction import run_memory_extraction_for_user, run_nightly_memory_extraction
from tests.conftest import TestSessionLocal


def _fake_llm(candidates):
    payload = json.dumps(candidates)

    class _Msg:
        content = payload

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    return AsyncMock(return_value=_Resp())


async def _headers(client, username: str, *, admin: bool = False) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    if admin:
        async with TestSessionLocal() as db:
            user = (await db.execute(select(User).where(User.username == username))).scalar_one()
            user.role = "admin"
            await db.commit()
    login = await client.post("/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _seed_user_with_chat(username: str, *, incognito: bool = False) -> int:
    async with TestSessionLocal() as db:
        user = User(username=username, email=f"{username}@example.com", hashed_password="x", role="parent")
        db.add(user)
        await db.flush()
        session = ChatSession(user_id=user.id, title="t", is_incognito=incognito)
        db.add(session)
        await db.flush()
        db.add_all([
            ChatMessage(session_id=session.id, role="user", content="Joey is allergic to tree nuts, remember that."),
            ChatMessage(session_id=session.id, role="assistant", content="Noted — Joey's allergy is tree nuts."),
        ])
        await db.commit()
        return user.id


CANDIDATES = [
    {"kind": "fact", "content": "Joey is allergic to tree nuts.", "subject": "Joey",
     "attribute": "allergy", "importance": 0.9, "confidence": 0.9},
    {"kind": "directive", "content": "Always avoid tree nuts in meal plans.",
     "importance": 0.8, "confidence": 0.9},
    {"kind": "fact", "content": "Emma attends Lincoln Elementary now.", "subject": "Emma",
     "attribute": "school", "importance": 0.7, "confidence": 0.4},
]


@pytest.mark.asyncio
async def test_extraction_queues_and_auto_approves_narrowly():
    user_id = await _seed_user_with_chat("extractuser")
    with patch("app.memory.extraction.litellm.acompletion", new=_fake_llm(CANDIDATES)):
        async with TestSessionLocal() as db:
            stats = await run_memory_extraction_for_user(db, user_id)
            await db.commit()

    assert stats["candidates"] == 3
    assert stats["auto_approved"] == 1   # high-confidence fact, no existing head
    assert stats["queued"] == 2          # directive + low-confidence fact stay pending

    async with TestSessionLocal() as db:
        proposals = (await db.execute(
            select(MemoryProposal).where(MemoryProposal.user_id == user_id)
        )).scalars().all()
        by_content = {p.content: p for p in proposals}
        approved = by_content["Joey is allergic to tree nuts."]
        assert approved.status == "approved"
        assert approved.approved_memory_id is not None
        memory = await db.get(Memory, approved.approved_memory_id)
        assert memory.kind == "fact"
        assert memory.source == "extraction"
        assert memory.subject_key == "joey:allergy"

        directive = by_content["Always avoid tree nuts in meal plans."]
        assert directive.status == "pending"
        assert "review" in (directive.reason or "").lower()

        low_confidence = by_content["Emma attends Lincoln Elementary now."]
        assert low_confidence.status == "pending"


@pytest.mark.asyncio
async def test_extraction_marks_would_supersede_for_review():
    user_id = await _seed_user_with_chat("extractconflict")
    # existing head for joey:allergy
    async with TestSessionLocal() as db:
        db.add(Memory(
            user_id=user_id, memory_type="semantic", kind="fact",
            subject_key="joey:allergy", content="Joey is allergic to peanuts.",
            importance=0.8, tags="[]", source="manual",
        ))
        await db.commit()

    with patch("app.memory.extraction.litellm.acompletion", new=_fake_llm([CANDIDATES[0]])):
        async with TestSessionLocal() as db:
            stats = await run_memory_extraction_for_user(db, user_id)
            await db.commit()

    assert stats["auto_approved"] == 0
    assert stats["queued"] == 1
    async with TestSessionLocal() as db:
        proposal = (await db.execute(
            select(MemoryProposal).where(MemoryProposal.user_id == user_id)
        )).scalars().one()
        assert proposal.status == "pending"
        assert "replace" in (proposal.reason or "").lower()
        # the stale head was NOT silently superseded
        head = (await db.execute(
            select(Memory).where(Memory.user_id == user_id, Memory.subject_key == "joey:allergy", Memory.is_active == True)
        )).scalars().one()
        assert "peanuts" in head.content


@pytest.mark.asyncio
async def test_extraction_never_reads_incognito_sessions():
    user_id = await _seed_user_with_chat("extractincog", incognito=True)
    fake = _fake_llm(CANDIDATES)
    with patch("app.memory.extraction.litellm.acompletion", new=fake):
        async with TestSessionLocal() as db:
            stats = await run_memory_extraction_for_user(db, user_id)

    assert stats == {"candidates": 0, "queued": 0, "auto_approved": 0, "skipped_duplicates": 0}
    fake.assert_not_awaited()  # no transcript -> the model is never called


@pytest.mark.asyncio
async def test_rerun_does_not_duplicate_proposals():
    user_id = await _seed_user_with_chat("extractrerun")
    with patch("app.memory.extraction.litellm.acompletion", new=_fake_llm([CANDIDATES[1]])):
        async with TestSessionLocal() as db:
            await run_memory_extraction_for_user(db, user_id)
            await db.commit()
        async with TestSessionLocal() as db:
            stats = await run_memory_extraction_for_user(db, user_id)
            await db.commit()

    assert stats["skipped_duplicates"] == 1
    assert stats["queued"] == 0
    async with TestSessionLocal() as db:
        proposals = (await db.execute(
            select(MemoryProposal).where(MemoryProposal.user_id == user_id)
        )).scalars().all()
    assert len(proposals) == 1


@pytest.mark.asyncio
async def test_admin_endpoint_requires_admin_and_runs(client):
    headers = await _headers(client, "extractadmin", admin=True)

    with patch("app.memory.extraction.litellm.acompletion", new=_fake_llm([])):
        resp = await client.post("/admin/memory-extraction/run", headers=headers)
    assert resp.status_code == 202
    assert resp.json()["status"] == "completed"

    pheaders = await _headers(client, "extractparent")
    denied = await client.post("/admin/memory-extraction/run", headers=pheaders)
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_pending_extraction_fact_can_be_approved_and_preserves_subject_key(client):
    headers = await _headers(client, "extractapproveowner")

    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "extractapproveowner"))).scalar_one()
        proposal = MemoryProposal(
            proposal_key="extract-approve-fact",
            user_id=user.id,
            proposal_type="flat_memory_create",
            source_type="nightly_extraction",
            status="pending",
            content="Joey is allergic to tree nuts.",
            confidence=0.84,
            reason="Queued for operator review.",
        )
        proposal.proposal_payload = {
            "memory_type": "semantic",
            "content": "Joey is allergic to tree nuts.",
            "kind": "fact",
            "subject": "Joey",
            "attribute": "allergy",
            "importance": 0.9,
            "tags": ["nightly_extraction"],
            "confidence": 0.84,
        }
        db.add(proposal)
        await db.commit()
        proposal_id = proposal.id

    approve = await client.post(f"/memories/review/{proposal_id}/approve", headers=headers)
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["proposal"]["status"] == "approved"
    assert payload["memory"]["content"] == "Joey is allergic to tree nuts."

    async with TestSessionLocal() as db:
        proposal = await db.get(MemoryProposal, proposal_id)
        memory = await db.get(Memory, proposal.approved_memory_id)
        assert proposal is not None
        assert memory is not None
        assert memory.subject_key == "joey:allergy"
        assert memory.kind == "fact"
        assert memory.source == "extraction"
        assert "nightly_extraction" in memory.tags_list


@pytest.mark.asyncio
async def test_pending_extraction_supersede_approval_replaces_existing_head(client):
    headers = await _headers(client, "extractsupersedeowner")

    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "extractsupersedeowner"))).scalar_one()
        stale = Memory(
            user_id=user.id,
            memory_type="semantic",
            kind="fact",
            subject_key="joey:allergy",
            content="Joey is allergic to peanuts.",
            importance=0.8,
            tags="[]",
            source="manual",
            is_active=True,
        )
        db.add(stale)
        await db.flush()
        proposal = MemoryProposal(
            proposal_key="extract-approve-supersede",
            user_id=user.id,
            proposal_type="flat_memory_create",
            source_type="nightly_extraction",
            status="pending",
            content="Joey is allergic to tree nuts.",
            confidence=0.91,
            reason="Would replace an existing memory",
        )
        proposal.proposal_payload = {
            "memory_type": "semantic",
            "content": "Joey is allergic to tree nuts.",
            "kind": "fact",
            "subject": "Joey",
            "attribute": "allergy",
            "importance": 0.9,
            "tags": ["nightly_extraction"],
            "confidence": 0.91,
        }
        db.add(proposal)
        await db.commit()
        proposal_id = proposal.id
        stale_id = stale.id

    approve = await client.post(f"/memories/review/{proposal_id}/approve", headers=headers)
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["proposal"]["status"] == "approved"
    assert payload["memory"]["content"] == "Joey is allergic to tree nuts."

    async with TestSessionLocal() as db:
        proposal = await db.get(MemoryProposal, proposal_id)
        old_head = await db.get(Memory, stale_id)
        new_head = await db.get(Memory, proposal.approved_memory_id)
        assert old_head is not None and new_head is not None
        assert old_head.is_active is False
        assert old_head.superseded_by_id == new_head.id
        assert new_head.subject_key == "joey:allergy"
        assert new_head.source == "extraction"
