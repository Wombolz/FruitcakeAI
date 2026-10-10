"""Memory v2 evaluation harness.

Turns "did memory get better?" into numbers. Runs entirely against an
in-memory SQLite database (no Postgres, no embeddings — this exercises the
lexical fallback path, the floor for every deployment) and measures, at two
store sizes (default 100 and 1000 memories):

  retrieval   recall of the known-relevant memory for scripted probes,
              mean injected count, mean non-directive tokens per turn,
              budget adherence violations
  legacy      the same probes under a simulation of the old 3-tier
              retrieval (Tier 1 injected every fact+directive, unbounded)
  dedup       re-asserting existing facts must not create rows
  conflict    contradicting subject-keyed facts must supersede cleanly
              and drop the stale head from retrieval

Usage:
  .venv/bin/python scripts/memory_eval.py
  .venv/bin/python scripts/memory_eval.py --sizes 100 1000 --budget 1200
Writes JSONL to scripts/memory_eval_results.jsonl and prints a summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlalchemy import and_, select  # noqa: E402

from app.db.models import Memory, User  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.memory.service import MemoryService, _estimate_tokens  # noqa: E402

FIRST_NAMES = [
    "Joey", "Emma", "Liam", "Sofia", "Noah", "Ava", "Mason", "Mia", "Ethan",
    "Isla", "Lucas", "Zoe", "Henry", "Ruby", "Owen", "Nora", "Levi", "Hazel",
]
ATTRIBUTES = [
    ("allergy", "is allergic to {v}", ["peanuts", "tree nuts", "shellfish", "dairy", "pollen", "penicillin"]),
    ("school", "attends {v}", ["Lincoln Elementary", "Westside Middle School", "Oakdale High", "Riverside Prep"]),
    ("birthday", "has a birthday on {v}", ["March 4", "July 19", "October 2", "December 27", "May 11"]),
    ("favorite_food", "loves eating {v}", ["lasagna", "sushi", "tacos", "pad thai", "grilled cheese"]),
    ("sport", "plays {v}", ["soccer", "baseball", "swimming", "tennis", "basketball"]),
    ("doctor", "sees {v} as their doctor", ["Dr. Alvarez", "Dr. Chen", "Dr. Patel", "Dr. Novak"]),
    ("bedtime", "has a bedtime of {v}", ["8pm", "8:30pm", "9pm", "9:30pm"]),
]
DIRECTIVES = [
    "Always use metric units in answers.",
    "Never schedule events before 9am.",
    "Prefer short bullet-point answers for planning questions.",
    "Always confirm before creating calendar events.",
    "Use casual tone with the kids' accounts.",
    "Weekly meal plans should avoid shellfish.",
    "Summaries of news should cite the source feed.",
    "Never suggest activities on Sunday mornings.",
]
JOURNAL_TEMPLATES = [
    "The family discussed {topic} during dinner.",
    "{name} mentioned wanting to try {topic} soon.",
    "Planning for {topic} started this week.",
    "{name} had an appointment about {topic}.",
]
TOPICS = [
    "a summer road trip", "the school science fair", "redoing the garage",
    "a new puppy", "piano lessons", "the neighborhood block party",
    "a camping weekend", "grandma's visit", "soccer tryouts", "a garden bed",
]


def _make_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_store(session_factory, *, size: int, rng: random.Random) -> dict[str, Any]:
    """Seed a synthetic household store. Returns probe targets:
    subject_key -> (memory_id, query, subject, attribute, value)."""
    now = datetime.now(timezone.utc)
    probes: dict[str, dict[str, Any]] = {}
    async with session_factory() as db:
        user = User(username="evaluser", email="eval@example.com", hashed_password="x", role="parent")
        db.add(user)
        await db.flush()
        user_id = user.id

        rows: list[Memory] = []
        for content in DIRECTIVES:
            rows.append(Memory(
                user_id=user_id, memory_type="procedural", kind="directive",
                content=content, importance=0.8, tags="[]", source="manual",
            ))

        # facts: subject x attribute grid until we hit ~70% of size
        fact_target = max(10, int(size * 0.7))
        made = 0
        for name in FIRST_NAMES:
            for attr, template, values in ATTRIBUTES:
                if made >= fact_target:
                    break
                value = rng.choice(values)
                detail = rng.choice([
                    "This was confirmed during a school-year checkup and should be kept current.",
                    "The family flagged this as important for planning meals and activities.",
                    "Noted from a conversation about weekly routines and reminders.",
                    "Relevant for scheduling, shopping lists, and weekend plans.",
                ])
                content = f"{name} {template.format(v=value)}. {detail}"
                subject_key = f"{name.lower()}:{attr}"
                memory = Memory(
                    user_id=user_id, memory_type="semantic", kind="fact",
                    subject_key=subject_key, content=content,
                    importance=rng.uniform(0.4, 0.9), tags="[]", source="manual",
                )
                rows.append(memory)
                probes[subject_key] = {
                    "query": f"What do you know about {name}'s {attr.replace('_', ' ')}?",
                    "subject": name, "attribute": attr, "value": value,
                    "content": content,
                }
                made += 1
            if made >= fact_target:
                break

        # journal: the remainder, with ages spread over 120 days
        journal_target = max(5, size - len(rows))
        for i in range(journal_target):
            template = rng.choice(JOURNAL_TEMPLATES)
            content = (
                template.format(name=rng.choice(FIRST_NAMES), topic=rng.choice(TOPICS))
                + f" Everyone shared thoughts on logistics, timing, and who would handle what. (note {i})"
            )
            age_days = rng.uniform(0, 120)
            memory = Memory(
                user_id=user_id, memory_type="episodic", kind="journal",
                content=content, importance=rng.uniform(0.3, 0.8), tags="[]", source="manual",
            )
            rows.append(memory)

        db.add_all(rows)
        await db.commit()

        # attach ids + backdate journal entries
        result = await db.execute(select(Memory).where(Memory.user_id == user_id))
        all_rows = result.scalars().all()
        journal_rows = [m for m in all_rows if m.kind == "journal"]
        for m in journal_rows:
            m.created_at = now - timedelta(days=rng.uniform(0, 120))
        for m in all_rows:
            if m.subject_key in probes:
                probes[m.subject_key]["memory_id"] = m.id
        await db.commit()

    return {"user_id": user_id, "probes": probes, "total": len(all_rows)}


async def _legacy_tier_retrieval(db, user_id: int) -> list[Memory]:
    """Simulate the old Tier 1: every active semantic+procedural memory,
    plus recent high-importance episodic — unbudgeted."""
    now = datetime.now(timezone.utc)
    tier1 = await db.execute(
        select(Memory).where(and_(
            Memory.user_id == user_id, Memory.is_active == True,
            Memory.memory_type.in_(["semantic", "procedural"]),
        ))
    )
    results = list(tier1.scalars().all())
    cutoff = now - timedelta(days=7)
    tier2 = await db.execute(
        select(Memory).where(and_(
            Memory.user_id == user_id, Memory.is_active == True,
            Memory.memory_type == "episodic",
            Memory.importance >= 0.6, Memory.created_at >= cutoff,
        ))
    )
    seen = {m.id for m in results}
    results.extend(m for m in tier2.scalars().all() if m.id not in seen)
    return results


async def _eval_store(size: int, budget: int, probe_count: int, rng: random.Random) -> list[dict[str, Any]]:
    engine, session_factory = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    seeded = await _seed_store(session_factory, size=size, rng=rng)
    user_id = seeded["user_id"]
    svc = MemoryService()
    records: list[dict[str, Any]] = []

    probe_items = list(seeded["probes"].values())
    rng.shuffle(probe_items)
    probe_items = [p for p in probe_items if "memory_id" in p][:probe_count]

    # --- retrieval probes: new vs legacy ---
    hits = 0
    injected_counts: list[int] = []
    injected_tokens: list[int] = []
    legacy_counts: list[int] = []
    legacy_tokens: list[int] = []
    budget_violations = 0
    async with session_factory() as db:
        for probe in probe_items:
            results = await svc.retrieve_for_context(db, user_id, query=probe["query"], token_budget=budget)
            ids = [m.id for m in results]
            non_directive = [m for m in results if m.kind != "directive"]
            tokens = sum(_estimate_tokens(m.content) for m in non_directive)
            if probe["memory_id"] in ids:
                hits += 1
            injected_counts.append(len(results))
            injected_tokens.append(tokens)
            if tokens > budget:
                budget_violations += 1

            legacy = await _legacy_tier_retrieval(db, user_id)
            legacy_counts.append(len(legacy))
            legacy_tokens.append(sum(_estimate_tokens(m.content) for m in legacy))

    records.append({
        "store_size": seeded["total"], "budget": budget, "metric": "retrieval",
        "probes": len(probe_items),
        "recall": round(hits / max(1, len(probe_items)), 3),
        "mean_injected": round(sum(injected_counts) / max(1, len(injected_counts)), 1),
        "mean_tokens": round(sum(injected_tokens) / max(1, len(injected_tokens)), 1),
        "budget_violations": budget_violations,
        "legacy_mean_injected": round(sum(legacy_counts) / max(1, len(legacy_counts)), 1),
        "legacy_mean_tokens": round(sum(legacy_tokens) / max(1, len(legacy_tokens)), 1),
    })

    # --- off-topic probes: nothing but directives/profile may ride along ---
    off_topic_queries = [
        "write a python function to sort a list",
        "what's the weather going to be like on Saturday",
        "open the dashboard",
        "tell me a joke about cats",
        "convert 12 miles to kilometers",
    ]
    off_topic_non_directive: list[int] = []
    async with session_factory() as db:
        for query in off_topic_queries:
            results = await svc.retrieve_for_context(db, user_id, query=query, token_budget=budget)
            off_topic_non_directive.append(sum(1 for m in results if m.kind != "directive"))
    records.append({
        "store_size": seeded["total"], "metric": "off_topic",
        "queries": len(off_topic_queries),
        "mean_non_directive_injected": round(sum(off_topic_non_directive) / len(off_topic_queries), 2),
        "max_non_directive_injected": max(off_topic_non_directive),
    })

    # --- dedup probes: re-assert existing facts ---
    dedup_targets = probe_items[: min(10, len(probe_items))]
    new_rows = 0
    async with session_factory() as db:
        before = (await db.execute(select(Memory).where(Memory.user_id == user_id))).scalars().all()
        before_count = len(before)
        for probe in dedup_targets:
            await svc.propose_write(
                db, user_id, content=probe["content"], memory_type="semantic",
                subject=probe["subject"], attribute=probe["attribute"],
            )
        await db.commit()
        after = (await db.execute(select(Memory).where(Memory.user_id == user_id))).scalars().all()
        new_rows = len(after) - before_count
    records.append({
        "store_size": seeded["total"], "metric": "dedup",
        "reasserted": len(dedup_targets), "new_rows_created": new_rows,
    })

    # --- conflict probes: contradict subject-keyed facts ---
    conflict_targets = probe_items[: min(10, len(probe_items))]
    correct_chains = 0
    stale_still_retrievable = 0
    async with session_factory() as db:
        for probe in conflict_targets:
            corrected = f"{probe['subject']} correction: the {probe['attribute'].replace('_', ' ')} is actually updated-{probe['value']}-revised."
            result = await svc.propose_write(
                db, user_id, content=corrected, memory_type="semantic",
                subject=probe["subject"], attribute=probe["attribute"],
            )
            await db.commit()
            old_row = await db.get(Memory, probe["memory_id"])
            chain_ok = (
                result.action == "superseded"
                and old_row is not None
                and old_row.is_active is False
                and old_row.superseded_by_id == (result.memory.id if result.memory else None)
            )
            if chain_ok:
                correct_chains += 1
            retrieved = await svc.retrieve_for_context(db, user_id, query=probe["query"], token_budget=budget)
            if probe["memory_id"] in [m.id for m in retrieved]:
                stale_still_retrievable += 1
    records.append({
        "store_size": seeded["total"], "metric": "conflict",
        "contradicted": len(conflict_targets),
        "correct_supersede_chains": correct_chains,
        "stale_heads_still_retrievable": stale_still_retrievable,
    })

    await engine.dispose()
    return records


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000])
    parser.add_argument("--budget", type=int, default=1200)
    parser.add_argument("--probes", type=int, default=25)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="scripts/memory_eval_results.jsonl")
    args = parser.parse_args()

    all_records: list[dict[str, Any]] = []
    for size in args.sizes:
        started = time.perf_counter()
        records = await _eval_store(size, args.budget, args.probes, random.Random(args.seed))
        for record in records:
            record["elapsed_s"] = round(time.perf_counter() - started, 2)
            all_records.append(record)
            print(json.dumps(record), flush=True)

    out_path = REPO_ROOT / args.out
    with out_path.open("w") as fh:
        for record in all_records:
            fh.write(json.dumps(record) + "\n")

    print("\n=== summary ===")
    for record in all_records:
        if record["metric"] == "retrieval":
            print(
                f"store={record['store_size']:5d} recall={record['recall']:.2f} "
                f"injected={record['mean_injected']:.0f} tokens={record['mean_tokens']:.0f} "
                f"(legacy: injected={record['legacy_mean_injected']:.0f} tokens={record['legacy_mean_tokens']:.0f}) "
                f"budget_violations={record['budget_violations']}"
            )
        elif record["metric"] == "off_topic":
            print(
                f"store={record['store_size']:5d} off_topic: mean_injected={record['mean_non_directive_injected']} "
                f"max={record['max_non_directive_injected']}"
            )
        elif record["metric"] == "dedup":
            print(f"store={record['store_size']:5d} dedup: reasserted={record['reasserted']} new_rows={record['new_rows_created']}")
        elif record["metric"] == "conflict":
            print(
                f"store={record['store_size']:5d} conflict: contradicted={record['contradicted']} "
                f"chains_ok={record['correct_supersede_chains']} stale_retrievable={record['stale_heads_still_retrievable']}"
            )


if __name__ == "__main__":
    asyncio.run(main())
