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
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.models import ChatMessage, ChatSession, Memory, MemoryProposal, User
from app.memory.extraction import _recent_transcript, run_memory_extraction_for_user, run_nightly_memory_extraction
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


def _fake_llm_sequence(*candidate_groups):
    responses = [_fake_llm(candidates).return_value for candidates in candidate_groups]
    return AsyncMock(side_effect=responses)


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
     "attribute": "allergy", "evidence": "Joey is allergic to tree nuts",
     "importance": 0.9, "confidence": 0.9},
    {"kind": "directive", "content": "Always avoid tree nuts in meal plans.",
     "evidence": "Joey is allergic to tree nuts, remember that", "importance": 0.8, "confidence": 0.9},
    {"kind": "fact", "content": "Emma attends Lincoln Elementary now.", "subject": "Emma",
     "attribute": "school", "evidence": "Joey is allergic to tree nuts",
     "importance": 0.7, "confidence": 0.4},
]

PROJECT_CANDIDATES = [
    {
        "kind": "fact",
        "content": "The Fruitcake repo root is /Users/jwomble/Development/fruitcake_v5.",
        "subject": "Fruitcake repo",
        "attribute": "root",
        "evidence": "Our Fruitcake repo lives at /Users/jwomble/Development/fruitcake_v5",
        "importance": 0.9,
        "confidence": 0.92,
    },
    {
        "kind": "directive",
        "content": "Use Docs/_internal for local planning notes and keep them out of git.",
        "evidence": "Docs/_internal is for local planning only",
        "importance": 0.82,
        "confidence": 0.88,
    },
]


@pytest.mark.asyncio
async def test_extraction_queues_and_auto_approves_narrowly():
    user_id = await _seed_user_with_chat("extractuser")
    fake = _fake_llm_sequence(CANDIDATES, [])
    with patch("app.memory.extraction.litellm.acompletion", new=fake):
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
        assert "lane:household" in memory.tags_list
        assert "lane:household" in approved.proposal_payload["tags"]

        directive = by_content["Always avoid tree nuts in meal plans."]
        assert directive.status == "pending"
        assert "review" in (directive.reason or "").lower()
        assert "lane:household" in directive.proposal_payload["tags"]

        low_confidence = by_content["Emma attends Lincoln Elementary now."]
        assert low_confidence.status == "pending"
    assert fake.await_count == 2


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

    with patch("app.memory.extraction.litellm.acompletion", new=_fake_llm_sequence([CANDIDATES[0]], [])):
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

    assert stats["candidates"] == 0 and stats["llm_calls"] == 0
    fake.assert_not_awaited()  # no transcript -> the model is never called


@pytest.mark.asyncio
async def test_rerun_does_not_duplicate_proposals():
    user_id = await _seed_user_with_chat("extractrerun")
    with patch(
        "app.memory.extraction.litellm.acompletion",
        new=_fake_llm_sequence([CANDIDATES[1]], [], [CANDIDATES[1]], []),
    ):
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
async def test_recent_transcript_ignores_empty_assistant_rows():
    async with TestSessionLocal() as db:
        user = User(username="extractempty", email="extractempty@example.com", hashed_password="x", role="parent")
        db.add(user)
        await db.flush()
        session = ChatSession(user_id=user.id, title="t", is_incognito=False)
        db.add(session)
        await db.flush()
        db.add_all([
            ChatMessage(session_id=session.id, role="user", content="Remember that the repo root is fruitcake_v5."),
            ChatMessage(session_id=session.id, role="assistant", content=""),
            ChatMessage(session_id=session.id, role="assistant", content="Confirmed."),
        ])
        await db.commit()
        transcript = await _recent_transcript(
            db,
            user.id,
            datetime.now(timezone.utc) - timedelta(hours=24),
        )

    assert "assistant: \n" not in transcript
    assert "assistant: Confirmed." in transcript


@pytest.mark.asyncio
async def test_extraction_project_lane_persists_project_memory_tags():
    async with TestSessionLocal() as db:
        user = User(username="extractproject", email="extractproject@example.com", hashed_password="x", role="parent")
        db.add(user)
        await db.flush()
        session = ChatSession(user_id=user.id, title="t", is_incognito=False)
        db.add(session)
        await db.flush()
        db.add_all([
            ChatMessage(
                session_id=session.id,
                role="user",
                content="Our Fruitcake repo lives at /Users/jwomble/Development/fruitcake_v5 and Docs/_internal is for local planning only.",
            ),
            ChatMessage(
                session_id=session.id,
                role="assistant",
                content="Understood. I'll treat that repo root and Docs/_internal convention as part of the project setup.",
            ),
        ])
        await db.commit()
        user_id = user.id

    fake = _fake_llm_sequence([], PROJECT_CANDIDATES)
    with patch("app.memory.extraction.litellm.acompletion", new=fake):
        async with TestSessionLocal() as db:
            stats = await run_memory_extraction_for_user(db, user_id)
            await db.commit()

    assert stats["candidates"] == 2
    assert stats["auto_approved"] == 1
    assert stats["queued"] == 1
    async with TestSessionLocal() as db:
        proposals = (await db.execute(
            select(MemoryProposal).where(MemoryProposal.user_id == user_id)
        )).scalars().all()
        by_content = {p.content: p for p in proposals}
        repo_root = by_content["The Fruitcake repo root is /Users/jwomble/Development/fruitcake_v5."]
        directive = by_content["Use Docs/_internal for local planning notes and keep them out of git."]
        assert "lane:project" in repo_root.proposal_payload["tags"]
        assert "lane:project" in directive.proposal_payload["tags"]
        memory = await db.get(Memory, repo_root.approved_memory_id)
        assert memory is not None
        assert "lane:project" in memory.tags_list


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


def _fake_raw(*raws):
    def _resp(raw):
        class _Msg:
            content = raw

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    return AsyncMock(side_effect=[_resp(r) for r in raws])


@pytest.mark.asyncio
async def test_empty_model_response_is_counted_and_retried():
    user_id = await _seed_user_with_chat("extractempty2")
    # household: empty, retry succeeds; project: empty, empty
    ok = json.dumps({"memories": [CANDIDATES[0]]})
    fake = _fake_raw("", ok, "", "")
    with patch("app.memory.extraction.litellm.acompletion", new=fake):
        async with TestSessionLocal() as db:
            stats = await run_memory_extraction_for_user(db, user_id)
            await db.commit()
    assert stats["llm_calls"] == 4
    assert stats["empty_responses"] == 3
    assert stats["candidates"] == 1
    # the retry doubled the token budget
    budgets = [c.kwargs["max_tokens"] for c in fake.await_args_list]
    assert budgets[1] == budgets[0] * 2


@pytest.mark.asyncio
async def test_all_empty_run_is_flagged_unusable():
    from app.memory.extraction import extraction_totals_unusable

    await _seed_user_with_chat("extractallempty")
    with patch("app.memory.extraction.litellm.acompletion", new=_fake_raw("", "", "", "")):
        async with TestSessionLocal() as db:
            totals = await run_nightly_memory_extraction(db)
    assert totals["candidates"] == 0
    assert totals["empty_responses"] == totals["llm_calls"] > 0
    assert "no usable output" in (extraction_totals_unusable(totals) or "")
    assert extraction_totals_unusable({"users": 1, "llm_calls": 2, "empty_responses": 0, "parse_failures": 0}) is None


@pytest.mark.asyncio
async def test_ungrounded_and_ephemeral_candidates_are_dropped():
    user_id = await _seed_user_with_chat("extractgrounding")
    junk = [
        # quote does not appear in anything the user said
        {"kind": "fact", "content": "User holds 264 shares of KO.", "subject": "user",
         "attribute": "holdings", "evidence": "I own 264 shares of Coca-Cola",
         "importance": 0.5, "confidence": 0.95},
        # grounded but ephemeral
        {"kind": "journal", "content": "A hurricane forecast threatens the area this weekend.",
         "evidence": "Joey is allergic to tree nuts", "importance": 0.9, "confidence": 0.9},
        CANDIDATES[0],
    ]
    with patch("app.memory.extraction.litellm.acompletion", new=_fake_raw(json.dumps({"memories": junk}), "{\"memories\": []}")):
        async with TestSessionLocal() as db:
            stats = await run_memory_extraction_for_user(db, user_id)
            await db.commit()
    assert stats["candidates"] == 1
    assert stats["ungrounded"] == 1
    assert stats["filtered"] == 1


@pytest.mark.asyncio
async def test_prompt_is_user_led_and_includes_known_memories_and_think_off():
    async with TestSessionLocal() as db:
        user = User(username="extractprompt", email="extractprompt@example.com", hashed_password="x", role="parent")
        db.add(user)
        await db.flush()
        session = ChatSession(user_id=user.id, title="t", is_incognito=False)
        db.add(session)
        await db.flush()
        db.add_all([
            ChatMessage(session_id=session.id, role="user", content="Emma starts piano lessons on Tuesdays."),
            ChatMessage(session_id=session.id, role="assistant", content="BULKYTOOLPAYLOAD " * 100),
        ])
        db.add(Memory(
            user_id=user.id, memory_type="semantic", kind="fact", subject_key="joey:allergy",
            content="Joey is allergic to tree nuts.", importance=0.9, tags="[]", source="manual",
        ))
        await db.commit()
        user_id = user.id

    fake = _fake_raw('{"memories": []}', '{"memories": []}')
    with patch("app.memory.extraction.settings") as cfg, patch("app.memory.extraction.litellm.acompletion", new=fake):
        cfg.document_summary_model = "ollama_chat/qwen3.6:35b"
        async with TestSessionLocal() as db:
            await run_memory_extraction_for_user(db, user_id)

    call = fake.await_args_list[0]
    prompt = call.kwargs["messages"][0]["content"]
    assert "ALREADY KNOWN" in prompt and "Joey is allergic to tree nuts." in prompt
    assert "Emma starts piano lessons on Tuesdays." in prompt
    assert prompt.count("BULKYTOOLPAYLOAD") < 40  # assistant turn was clipped
    assert call.kwargs["think"] is False
    assert call.kwargs["response_format"]["type"] == "json_schema"


def test_merge_folds_paraphrased_candidates_but_not_distinct_facts():
    from app.memory.extraction import _merge_lane_candidates

    base = {"kind": "fact", "subject": None, "attribute": None, "evidence": "x y", "importance": 0.5}
    a = dict(base, content="The children Arui and Evie attend Garden Class at the Georgia Southern Botanic Gardens.", confidence=0.8)
    b = dict(base, content="Children Arui, Evie, Dean and Sam attend the Garden Class at the Georgia Southern Botanic Gardens.", confidence=0.9)
    c = dict(base, content="Joey is allergic to tree nuts.", confidence=0.9)
    d = dict(base, content="Joey is allergic to peanuts.", confidence=0.9)
    merged = _merge_lane_candidates([("household", [a, c]), ("project", [b, d])])
    contents = [m["content"] for m in merged]
    assert len(merged) == 3  # a/b folded; short distinct facts untouched
    assert any("Dean" in c for c in contents)  # stronger paraphrase kept


@pytest.mark.asyncio
async def test_review_api_payload_has_keys_the_native_client_requires(client):
    headers = await _headers(client, "extractclientcompat")
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "extractclientcompat"))).scalar_one()
        proposal = MemoryProposal(
            proposal_key="extract-client-compat",
            user_id=user.id,
            proposal_type="flat_memory_create",
            source_type="nightly_extraction",
            status="pending",
            content="Joey is allergic to tree nuts.",
            confidence=0.9,
        )
        # extraction-style payload: no topic / supporting_urls / source_names
        proposal.proposal_payload = {"kind": "fact", "content": "Joey is allergic to tree nuts.", "subject": None}
        db.add(proposal)
        await db.commit()

    resp = await client.get("/memories/review", headers=headers)
    assert resp.status_code == 200
    payload = resp.json()[0]["proposal"]
    assert payload["supporting_urls"] == []
    assert payload["source_names"] == []
    assert payload["memory_type"] == "semantic"
    assert payload["content"] == "Joey is allergic to tree nuts."

async def test_sensitive_candidates_never_auto_approve():
    user_id = await _seed_user_with_chat("extractsensitive")
    flagged = dict(CANDIDATES[0], content="Joey is allergic to tree nuts and Sam upset him about it.",
                   attribute="incident", sensitive=True)
    regex_only = {"kind": "journal", "content": "Neighbor Pat accused the family of harassment on Sept 30.",
                  "evidence": "Joey is allergic to tree nuts", "importance": 0.8, "confidence": 0.95}
    clean = dict(CANDIDATES[0], sensitive=False)
    payload = json.dumps({"memories": [flagged, regex_only, clean]})
    with patch("app.memory.extraction.litellm.acompletion", new=_fake_raw(payload, '{"memories": []}')):
        async with TestSessionLocal() as db:
            stats = await run_memory_extraction_for_user(db, user_id)
            await db.commit()
    assert stats["auto_approved"] == 1
    assert stats["queued"] == 2
    async with TestSessionLocal() as db:
        rows = (await db.execute(select(MemoryProposal).where(MemoryProposal.user_id == user_id))).scalars().all()
    pending = [r for r in rows if r.status == "pending"]
    assert len(pending) == 2
    assert all("sensitive" in (r.reason or "").lower() for r in pending)


@pytest.mark.asyncio
async def test_journal_candidates_always_queue_for_review():
    user_id = await _seed_user_with_chat("extractjournal")
    journal = {"kind": "journal", "content": "In-laws are visiting the family on July 12th.",
               "evidence": "Joey is allergic to tree nuts", "importance": 0.7, "confidence": 0.99}
    with patch("app.memory.extraction.litellm.acompletion", new=_fake_raw(json.dumps({"memories": [journal]}), '{"memories": []}')):
        async with TestSessionLocal() as db:
            stats = await run_memory_extraction_for_user(db, user_id)
            await db.commit()
    assert stats["auto_approved"] == 0
    assert stats["queued"] == 1
    async with TestSessionLocal() as db:
        proposal = (await db.execute(select(MemoryProposal).where(MemoryProposal.user_id == user_id))).scalars().one()
    assert proposal.status == "pending"
    assert "journal" in (proposal.reason or "").lower()
