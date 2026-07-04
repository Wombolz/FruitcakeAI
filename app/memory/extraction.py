"""Nightly memory extraction (Roadmap Phase 8, pulled forward by memory v2).

Reviews the last day of non-incognito chat activity per user, asks a model
to extract durable memory candidates, and routes every candidate into the
existing MemoryProposal review queue (source_type="nightly_extraction").

Auto-approval is deliberately narrow — the trust surface is the point:
- auto-approved: confidence >= threshold, kind in {fact, journal}, and no
  existing head for the same subject_key (nothing is silently superseded)
- everything else stays pending for operator review: directives, low
  confidence, and any candidate that would replace an existing memory

Approved candidates are written through the one enforced pipeline
(MemoryService.propose_write), so extraction gets the same dedup, conflict,
exclusion, and cap discipline as every other writer.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import litellm
import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import ChatMessage, ChatSession, Memory, MemoryProposal
from app.memory.review_service import encode_proposal_payload
from app.memory.service import VALID_KINDS, _normalize_subject_key, get_memory_service

log = structlog.get_logger(__name__)

EXTRACTION_SOURCE_TYPE = "nightly_extraction"
EXTRACTION_PROPOSAL_TYPE = "flat_memory_create"
AUTO_APPROVE_CONFIDENCE = 0.75
MAX_CANDIDATES_PER_USER = 12
MAX_TRANSCRIPT_CHARS = 24_000

_EXTRACTION_PROMPTS: dict[str, str] = {
    "household": """You review one day of a household assistant's chat \
transcripts and extract durable memories worth keeping about the user and \
their household.

Return ONLY a JSON array (no prose). Each item:
{"kind": "fact|journal|directive", "content": "<self-contained statement>", \
"subject": "<who/what, for facts>", "attribute": "<which property, for facts>", \
"importance": 0.0-1.0, "confidence": 0.0-1.0}

Extract only:
- durable household facts (people, preferences, allergies, schools, routines)
- standing behavioral instructions the user clearly stated (kind=directive)
- meaningful near-term events (kind=journal)

Never extract: project/work state, one-off chatter, anything the assistant said
without user confirmation, implementation details, or secrets/credentials.
Return [] when nothing qualifies.

Transcript:
""",
    "project": """You review one day of a technical assistant's chat \
transcripts and extract durable project or workflow memories worth keeping.

Return ONLY a JSON array (no prose). Each item:
{"kind": "fact|journal|directive", "content": "<self-contained statement>", \
"subject": "<who/what, for facts>", "attribute": "<which property, for facts>", \
"importance": 0.0-1.0, "confidence": 0.0-1.0}

Extract only:
- durable project facts or environment constraints the user clearly confirmed
- stable workflow preferences, operating rules, or standing instructions \
  (kind=directive)
- meaningful project events that are likely to matter in the near term \
  (kind=journal)

Never extract: ordinary question/answer chatter, speculative assistant advice, \
one-off execution details, secrets/credentials, or web/news content unless the \
user clearly confirmed it as part of their enduring workflow or project state.
Use self-contained wording like "The Fruitcake repo root is ..." or \
"Use Docs/_internal for local planning notes."
Return [] when nothing qualifies.

Transcript:
""",
}


def _extraction_model() -> str:
    return settings.document_summary_model or settings.task_small_model or settings.llm_model


def _parse_candidates(raw: str) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    match = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        if not content or kind not in VALID_KINDS:
            continue
        out.append(
            {
                "kind": kind,
                "content": content,
                "subject": str(item.get("subject") or "").strip() or None,
                "attribute": str(item.get("attribute") or "").strip() or None,
                "importance": max(0.0, min(1.0, float(item.get("importance") or 0.5))),
                "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.5))),
            }
        )
    return out[:MAX_CANDIDATES_PER_USER]


async def _extract_candidates_for_lane(transcript: str, lane: str) -> list[dict[str, Any]]:
    prompt = _EXTRACTION_PROMPTS.get(lane)
    if not prompt:
        return []
    response = await litellm.acompletion(
        model=_extraction_model(),
        messages=[{"role": "user", "content": prompt + transcript}],
        max_tokens=1200,
    )
    return _parse_candidates(response.choices[0].message.content or "")


def _merge_lane_candidates(lane_results: list[tuple[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for lane, candidates in lane_results:
        for candidate in candidates:
            content = str(candidate.get("content") or "").strip()
            if not content:
                continue
            key = content.lower()
            if key not in merged:
                merged[key] = dict(candidate)
                merged[key]["lanes"] = {lane}
                order.append(key)
                continue
            existing = merged[key]
            existing["lanes"].add(lane)
            if candidate.get("subject") and not existing.get("subject"):
                existing["subject"] = candidate["subject"]
            if candidate.get("attribute") and not existing.get("attribute"):
                existing["attribute"] = candidate["attribute"]
            existing_confidence = float(existing.get("confidence") or 0.0)
            candidate_confidence = float(candidate.get("confidence") or 0.0)
            existing["importance"] = max(
                float(existing.get("importance") or 0.0),
                float(candidate.get("importance") or 0.0),
            )
            existing["confidence"] = max(existing_confidence, candidate_confidence)
            if candidate_confidence > existing_confidence:
                existing["kind"] = candidate.get("kind") or existing.get("kind")

    ordered = [merged[key] for key in order]
    ordered.sort(
        key=lambda item: (
            float(item.get("importance") or 0.0),
            float(item.get("confidence") or 0.0),
        ),
        reverse=True,
    )
    return ordered[:MAX_CANDIDATES_PER_USER]


def _candidate_tags(candidate: dict[str, Any]) -> list[str]:
    tags = ["nightly_extraction"]
    for lane in sorted(str(lane).strip() for lane in (candidate.get("lanes") or set()) if str(lane).strip()):
        tags.append(f"lane:{lane}")
    return tags


async def _recent_transcript(db: AsyncSession, user_id: int, since: datetime) -> str:
    rows = await db.execute(
        select(ChatMessage.role, ChatMessage.content)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(
            and_(
                ChatSession.user_id == user_id,
                ChatSession.is_incognito == False,  # never read incognito sessions
                ChatSession.is_task_session == False,
                ChatMessage.created_at >= since,
                ChatMessage.role.in_(["user", "assistant"]),
            )
        )
        .order_by(ChatMessage.created_at.asc())
    )
    lines = [f"{role}: {content}" for role, content in rows.all() if str(content or "").strip()]
    transcript = "\n".join(lines)
    return transcript[-MAX_TRANSCRIPT_CHARS:]


async def _proposal_exists(db: AsyncSession, user_id: int, proposal_key: str) -> bool:
    row = await db.execute(
        select(MemoryProposal.id).where(MemoryProposal.proposal_key == proposal_key).limit(1)
    )
    return row.scalar_one_or_none() is not None


def _proposal_key(user_id: int, content: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"{user_id}:{content.lower().strip()}".encode()).hexdigest()
    return f"extract_{digest[:48]}"


async def _has_conflicting_head(db: AsyncSession, user_id: int, subject_key: str | None) -> bool:
    if not subject_key:
        return False
    row = await db.execute(
        select(Memory.id).where(
            and_(
                Memory.user_id == user_id,
                Memory.subject_key == subject_key,
                Memory.is_active == True,
                Memory.superseded_by_id.is_(None),
            )
        ).limit(1)
    )
    return row.scalar_one_or_none() is not None


async def run_memory_extraction_for_user(
    db: AsyncSession,
    user_id: int,
    *,
    since_hours: int = 24,
) -> dict[str, int]:
    """Extract, queue, and (narrowly) auto-approve memory candidates for one
    user. Returns counters for observability. Caller commits."""
    stats = {"candidates": 0, "queued": 0, "auto_approved": 0, "skipped_duplicates": 0}
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    transcript = await _recent_transcript(db, user_id, since)
    if not transcript.strip():
        return stats

    lane_results: list[tuple[str, list[dict[str, Any]]]] = []
    for lane in ("household", "project"):
        lane_results.append((lane, await _extract_candidates_for_lane(transcript, lane)))
    candidates = _merge_lane_candidates(lane_results)
    stats["candidates"] = len(candidates)
    if not candidates:
        return stats

    svc = get_memory_service()
    now = datetime.now(timezone.utc)
    for candidate in candidates:
        key = _proposal_key(user_id, candidate["content"])
        if await _proposal_exists(db, user_id, key):
            stats["skipped_duplicates"] += 1
            continue

        subject_key = _normalize_subject_key(candidate["subject"], candidate["attribute"])
        would_supersede = await _has_conflicting_head(db, user_id, subject_key)
        auto_approvable = (
            candidate["confidence"] >= AUTO_APPROVE_CONFIDENCE
            and candidate["kind"] in {"fact", "journal"}
            and not would_supersede
        )
        tags = _candidate_tags(candidate)

        proposal = MemoryProposal(
            proposal_key=key,
            user_id=user_id,
            proposal_type=EXTRACTION_PROPOSAL_TYPE,
            source_type=EXTRACTION_SOURCE_TYPE,
            status="pending",
            content=candidate["content"],
            confidence=candidate["confidence"],
            reason=(
                "Would replace an existing memory" if would_supersede
                else ("Standing directive — operator review required" if candidate["kind"] == "directive" else "")
            ) or None,
            proposal_json=encode_proposal_payload(
                {
                    "memory_type": {"directive": "procedural", "fact": "semantic", "journal": "episodic"}[candidate["kind"]],
                    "content": candidate["content"],
                    "kind": candidate["kind"],
                    "subject": candidate["subject"],
                    "attribute": candidate["attribute"],
                    "importance": candidate["importance"],
                    "tags": tags,
                    "lanes": sorted(candidate.get("lanes") or []),
                }
            ),
        )
        db.add(proposal)
        await db.flush()

        if auto_approvable:
            result = await svc.propose_write(
                db,
                user_id,
                content=candidate["content"],
                kind=candidate["kind"],
                subject=candidate["subject"],
                attribute=candidate["attribute"],
                importance=candidate["importance"],
                tags=tags,
                source="extraction",
                confidence=candidate["confidence"],
            )
            if result.memory is not None:
                proposal.status = "approved"
                proposal.approved_memory_id = result.memory.id
                proposal.resolved_at = now
                stats["auto_approved"] += 1
                continue
            # pipeline refused — keep it pending with the refusal reason
            proposal.reason = result.reason

        stats["queued"] += 1

    log.info("memory.extraction_completed", user_id=user_id, **stats)
    return stats


async def run_nightly_memory_extraction(db: AsyncSession, *, since_hours: int = 24) -> dict[str, int]:
    """Run extraction for every user with recent, non-incognito chat activity."""
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    users = await db.execute(
        select(ChatSession.user_id)
        .join(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(
            and_(
                ChatSession.is_incognito == False,
                ChatSession.is_task_session == False,
                ChatMessage.created_at >= since,
            )
        )
        .distinct()
    )
    totals = {"users": 0, "candidates": 0, "queued": 0, "auto_approved": 0, "skipped_duplicates": 0}
    for (user_id,) in users.all():
        try:
            stats = await run_memory_extraction_for_user(db, int(user_id), since_hours=since_hours)
        except Exception:
            log.warning("memory.extraction_failed", user_id=user_id, exc_info=True)
            continue
        totals["users"] += 1
        for key in ("candidates", "queued", "auto_approved", "skipped_duplicates"):
            totals[key] += stats[key]
    return totals
