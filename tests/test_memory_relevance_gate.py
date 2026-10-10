"""
Relevance-gated memory injection.

The token budget is a cap, not a filter: unrelated memories must not ride
along on every prompt.
- off-topic queries inject no facts/journal
- relevant facts qualify; directives stay always-on
- "profile"-tagged facts are always visible (capped)
- sensitive memories are only admitted by a strong match to the query
- no query -> directives + profile only (never a store dump)
"""

from __future__ import annotations

import json

import pytest

from app.db.models import Memory, User
from app.memory.service import MemoryService, is_sensitive_memory
from tests.conftest import TestSessionLocal


async def _user(username: str) -> int:
    async with TestSessionLocal() as db:
        user = User(username=username, email=f"{username}@example.com", hashed_password="x", role="parent")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id


async def _seed(user_id: int, rows: list[dict]) -> dict[str, int]:
    ids: dict[str, int] = {}
    async with TestSessionLocal() as db:
        for row in rows:
            m = Memory(
                user_id=user_id,
                memory_type={"directive": "procedural", "fact": "semantic", "journal": "episodic"}[row["kind"]],
                kind=row["kind"],
                content=row["content"],
                importance=row.get("importance", 0.6),
                tags=json.dumps(row.get("tags", [])),
                source="manual",
                is_active=True,
            )
            db.add(m)
            await db.flush()
            ids[row["key"]] = m.id
        await db.commit()
    return ids


ROWS = [
    {"key": "rule", "kind": "directive", "content": "Respond in English."},
    {"key": "name", "kind": "fact", "content": "The user's full name is John Womble.", "tags": ["profile"]},
    {"key": "ko", "kind": "fact", "content": "The user owns 264 shares of Coca-Cola KO stock."},
    {"key": "piano", "kind": "fact", "content": "Emma takes piano lessons on Tuesdays."},
    {"key": "news", "kind": "fact", "content": "The morning briefing includes top headlines from RSS feeds."},
    {"key": "incident", "kind": "journal", "content": "Sam refused to share the school pickup schedule with Pat.",
     "tags": ["incident"]},
    {"key": "harass", "kind": "fact", "content": "Pat harassed the family at the park last week."},
]


async def _retrieve(username: str, query: str | None) -> set[str]:
    user_id = await _user(username)
    ids = await _seed(user_id, ROWS)
    inverse = {v: k for k, v in ids.items()}
    async with TestSessionLocal() as db:
        mems = await MemoryService().retrieve_for_context(db, user_id, query=query)
    return {inverse[m.id] for m in mems}


@pytest.mark.asyncio
async def test_off_topic_query_injects_only_directives_and_profile():
    got = await _retrieve("gate_offtopic", "write a python function to sort a list")
    assert got == {"rule", "name"}


@pytest.mark.asyncio
async def test_relevant_fact_qualifies_and_irrelevant_ones_do_not():
    got = await _retrieve("gate_relevant", "when are Emma's piano lessons")
    assert "piano" in got
    assert "ko" not in got and "news" not in got
    assert {"rule", "name"} <= got  # always-on tiers


@pytest.mark.asyncio
async def test_no_query_is_not_a_store_dump():
    got = await _retrieve("gate_noquery", None)
    assert got == {"rule", "name"}


@pytest.mark.asyncio
async def test_sensitive_memories_need_a_strong_query_match():
    # weakly related query (shares one term) must not admit sensitive items
    weak = await _retrieve("gate_sens_weak", "what time does the park open today")
    assert "incident" not in weak and "harass" not in weak
    # explicit request about the topic does
    strong = await _retrieve("gate_sens_strong", "what happened when Pat harassed the family at the park")
    assert "harass" in strong


@pytest.mark.asyncio
async def test_sensitivity_detection_uses_tags_and_content():
    assert is_sensitive_memory(Memory(content="Plain fact.", tags=json.dumps(["legal"]), kind="fact"))
    assert is_sensitive_memory(Memory(content="They filed for custody.", tags="[]", kind="fact"))
    assert not is_sensitive_memory(Memory(content="Emma likes piano.", tags="[]", kind="fact"))
