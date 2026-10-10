"""Nightly memory extraction (Roadmap Phase 8, pulled forward by memory v2).

Reviews the last day of non-incognito chat activity per user, asks a model
to extract durable memory candidates, and routes every candidate into the
existing MemoryProposal review queue (source_type="nightly_extraction").

Extraction is deliberately conservative about *what* it reads and *what* it
accepts:
- the transcript is user-led: user turns in full, assistant turns clipped to
  a short context snippet (assistant output is mostly tool/news payload)
- sessions are extracted separately, packed into bounded chunks
- the model sees the user's already-known memories so it only returns new or
  changed facts
- every candidate must quote the user ("evidence") and that quote must be
  found in the user's own words; ungrounded and ephemeral (weather/news/
  prices) candidates are dropped and counted

Auto-approval is deliberately narrow — the trust surface is the point:
- auto-approved: confidence >= threshold, kind == fact, not sensitive, and no
  existing head for the same subject_key (nothing is silently superseded)
- everything else stays pending for operator review: directives, journal
  entries (the noisiest kind), sensitive content, low confidence, and any
  candidate that would replace an existing memory

Approved candidates are written through the one enforced pipeline
(MemoryService.propose_write), so extraction gets the same dedup, conflict,
exclusion, and cap discipline as every other writer.

Failures must be loud: an empty or unparseable model response is counted
separately from "nothing worth saving" and the system job fails when every
model call was unusable (a thinking model once exhausted its token budget on
reasoning and silently returned nothing for months).
"""

from __future__ import annotations

import json
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import litellm
import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import ChatMessage, ChatSession, Memory, MemoryProposal
from app.memory.review_service import encode_proposal_payload
from app.memory.service import (
    VALID_KINDS,
    _normalize_subject_key,
    get_memory_service,
    is_sensitive_text as _is_sensitive_text,
)

log = structlog.get_logger(__name__)

EXTRACTION_SOURCE_TYPE = "nightly_extraction"
EXTRACTION_PROPOSAL_TYPE = "flat_memory_create"
_STAT_KEYS = (
    "candidates", "queued", "auto_approved", "skipped_duplicates",
    "llm_calls", "empty_responses", "parse_failures", "ungrounded", "filtered",
    "sessions",
)
AUTO_APPROVE_CONFIDENCE = 0.75
MAX_CANDIDATES_PER_USER = 12
MAX_TRANSCRIPT_CHARS = 24_000
MAX_CHUNK_CHARS = 8_000
MAX_CHUNKS_PER_USER = 6
MIN_USER_CHARS = 30  # skip slices like "open the dashboard"
_run_lock: asyncio.Lock | None = None
USER_TURN_CHAR_CAP = 1_500
ASSISTANT_TURN_CHAR_CAP = 240
KNOWN_MEMORY_TOKEN_BUDGET = 500
# Room for reasoning plus the answer; a thinking model that ignores think=False
# must still be able to finish. One retry doubles it on an empty response.
EXTRACTION_MAX_TOKENS = 2_500
EVIDENCE_MIN_TOKEN_OVERLAP = 0.7

_SHARED_RULES = """Return ONLY a JSON object: {"memories": [ ... ]}. Each item:
{"kind": "fact|journal|directive", "content": "<self-contained statement>", \
"subject": "<who/what, for facts>", "attribute": "<which property, for facts>", \
"evidence": "<short verbatim quote from a USER line that supports this>", \
"sensitive": true|false, \
"importance": 0.0-1.0, "confidence": 0.0-1.0}

Rules:
- Only the user's own statements count. Lines starting "assistant:" are context \
only; never extract something just because the assistant said it.
- "evidence" must be words the user actually wrote. If you cannot quote the \
user, do not return the item.
- Skip anything already listed under ALREADY KNOWN. If a known fact has \
clearly changed, return the new value with the same subject and attribute.
- Never extract: weather, news, headlines, prices, sports scores, search or \
research results, one-off requests ("open the dashboard"), tool/dashboard \
state, implementation details, or secrets/credentials.
- Set "sensitive": true when the item describes a conflict, dispute, \
harassment, accusation, legal, medical or financial trouble, or anything \
about a named person outside the household that they would not expect to be \
stored silently. Otherwise false.
- Phrase situational directives as "When <situation>, <rule>" so they only \
apply when relevant; write unconditional rules plainly ("Respond in English.").
- Prefer returning nothing over returning something doubtful. Return \
{"memories": []} when nothing qualifies.
"""

_EXTRACTION_PROMPTS: dict[str, str] = {
    "household": """You review chat transcripts of a household assistant and \
extract durable memories worth keeping about the user and their household.

Extract only:
- durable household facts (people, preferences, allergies, schools, routines)
- standing behavioral instructions the user clearly stated (kind=directive)
- meaningful near-term personal events (kind=journal)

""" + _SHARED_RULES + """
Example transcript:
user: Joey is allergic to tree nuts, so none of that in the meal plan.
assistant: Got it, here is a nut-free plan...
user: what's the weather Saturday?
assistant: Heavy rain and gusts up to 30 mph expected...
Example output:
{"memories": [{"kind": "fact", "content": "Joey is allergic to tree nuts.", \
"subject": "Joey", "attribute": "allergy", "evidence": "Joey is allergic to \
tree nuts", "importance": 0.9, "confidence": 0.95}]}
(The weather answer is ignored: it is news about the world, not the user.)
""",
    "project": """You review chat transcripts of a technical assistant and \
extract durable project or workflow memories worth keeping.

Extract only:
- durable project facts or environment constraints the user clearly confirmed
- stable workflow preferences, operating rules, or standing instructions \
(kind=directive)
- meaningful project events likely to matter in the near term (kind=journal)

Use self-contained wording like "The Fruitcake repo root is ..." or \
"Use Docs/_internal for local planning notes."

""" + _SHARED_RULES + """
Example transcript:
user: From now on put planning notes in Docs/_internal and keep them out of git.
assistant: Understood.
user: run the tests
Example output:
{"memories": [{"kind": "directive", "content": "Put planning notes in \
Docs/_internal and keep them out of git.", "evidence": "put planning notes in \
Docs/_internal and keep them out of git", "importance": 0.8, "confidence": \
0.9}]}
(\"run the tests\" is a one-off request, not a memory.)
""",
}

_EXTRACTION_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "memory_candidates",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["fact", "journal", "directive"]},
                            "content": {"type": "string"},
                            "subject": {"type": "string"},
                            "attribute": {"type": "string"},
                            "evidence": {"type": "string"},
                            "sensitive": {"type": "boolean"},
                            "importance": {"type": "number"},
                            "confidence": {"type": "number"},
                        },
                        "required": ["kind", "content", "evidence"],
                    },
                }
            },
            "required": ["memories"],
        },
    },
}

# Content that is world-news/ephemeral state even when the user mentioned it.
_EPHEMERAL_CONTENT_RE = re.compile(
    r"\b(forecast|weather|hurricane|tornado|headline|breaking news|"
    r"share price|stock price|trading at|market (?:is|closed|opened)|"
    r"score(?:d|s)?\b|dashboard (?:shows|is open)|devices found)\b",
    re.IGNORECASE,
)


def _extraction_model() -> str:
    return settings.document_summary_model or settings.task_small_model or settings.llm_model


def _completion_kwargs(model: str) -> dict[str, Any]:
    """Extra litellm kwargs. Local thinking models must not spend the budget
    on reasoning; response_format is dropped by litellm where unsupported."""
    kwargs: dict[str, Any] = {"response_format": _EXTRACTION_RESPONSE_FORMAT, "drop_params": True}
    if str(model).startswith("ollama"):
        kwargs["think"] = False
    return kwargs


def _coerce_score(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _try_parse(raw: str) -> list[dict[str, Any]] | None:
    """Parse a model response. None means unparseable (distinct from [])."""
    text = str(raw or "").strip()
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    parsed: Any = None
    # Prefer whichever bracket opens first, so a bare array of objects is not
    # mistaken for a single object.
    first_obj, first_arr = text.find("{"), text.find("[")
    patterns = [r"\{.*\}", r"\[.*\]"]
    if first_arr != -1 and (first_obj == -1 or first_arr < first_obj):
        patterns.reverse()
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.DOTALL)
        if not match:
            continue
        try:
            candidate = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            candidate = candidate.get("memories")
        if isinstance(candidate, list):
            parsed = candidate
            break
    if not isinstance(parsed, list):
        return None
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
                "evidence": str(item.get("evidence") or "").strip(),
                "sensitive": bool(item.get("sensitive")) or _is_sensitive_text(content),
                "importance": _coerce_score(item.get("importance")),
                "confidence": _coerce_score(item.get("confidence")),
            }
        )
    return out[:MAX_CANDIDATES_PER_USER]


def _parse_candidates(raw: str) -> list[dict[str, Any]]:
    return _try_parse(raw) or []


def _norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def _user_text(chunk: str) -> str:
    return "\n".join(line[len("user:"):].strip() for line in chunk.splitlines() if line.startswith("user:"))


def _is_grounded(evidence: str, user_text: str) -> bool:
    words = _norm_words(evidence)
    if len(words) < 2:
        return False
    haystack = " ".join(_norm_words(user_text))
    if " ".join(words) in haystack:
        return True
    user_words = set(_norm_words(user_text))
    overlap = sum(1 for w in words if w in user_words) / len(words)
    return overlap >= EVIDENCE_MIN_TOKEN_OVERLAP


class _CallStats:
    def __init__(self) -> None:
        self.llm_calls = 0
        self.empty_responses = 0
        self.parse_failures = 0
        self.ungrounded = 0
        self.filtered = 0


async def _known_memories_block(db: AsyncSession | None, user_id: int | None, chunk: str) -> str:
    if db is None or user_id is None:
        return "(none)"
    try:
        memories = await get_memory_service().retrieve_for_context(
            db, user_id, _user_text(chunk)[:600] or None, token_budget=KNOWN_MEMORY_TOKEN_BUDGET
        )
    except Exception:
        log.warning("memory.extraction_known_lookup_failed", user_id=user_id, exc_info=True)
        return "(none)"
    lines = [f"- {m.content}" for m in memories]
    return "\n".join(lines) if lines else "(none)"


async def _call_model(prompt: str, stats: _CallStats) -> list[dict[str, Any]]:
    model = _extraction_model()
    from app.agent.local_model_lifecycle import track_local_model_use

    track_local_model_use(model)
    max_tokens = EXTRACTION_MAX_TOKENS
    for attempt in (1, 2):
        stats.llm_calls += 1
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            **_completion_kwargs(model),
        )
        raw = response.choices[0].message.content or ""
        if raw.strip():
            parsed = _try_parse(raw)
            if parsed is None:
                stats.parse_failures += 1
                log.warning("memory.extraction_unparseable", model=model, preview=raw[:200])
                return []
            return parsed
        stats.empty_responses += 1
        log.warning("memory.extraction_empty_response", model=model, attempt=attempt, max_tokens=max_tokens)
        max_tokens *= 2
    return []


async def _extract_candidates_for_lane(
    transcript: str,
    lane: str,
    *,
    stats: _CallStats | None = None,
    db: AsyncSession | None = None,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    prompt = _EXTRACTION_PROMPTS.get(lane)
    if not prompt:
        return []
    stats = stats or _CallStats()
    known = await _known_memories_block(db, user_id, transcript)
    full_prompt = f"{prompt}\nALREADY KNOWN:\n{known}\n\nTranscript:\n{transcript}"
    candidates = await _call_model(full_prompt, stats)

    user_text = _user_text(transcript)
    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        if not _is_grounded(candidate.get("evidence", ""), user_text):
            stats.ungrounded += 1
            continue
        if _EPHEMERAL_CONTENT_RE.search(candidate["content"]):
            stats.filtered += 1
            continue
        kept.append(candidate)
    return kept


def _near_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Cheap paraphrase check for candidates from the same run (the write
    pipeline's embedding dedup only sees what is already stored)."""
    wa = {w for w in _norm_words(a.get("content", "")) if len(w) > 2}
    wb = {w for w in _norm_words(b.get("content", "")) if len(w) > 2}
    if min(len(wa), len(wb)) < 6:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.75


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
            existing["sensitive"] = bool(existing.get("sensitive") or candidate.get("sensitive"))
            existing["importance"] = max(
                float(existing.get("importance") or 0.0),
                float(candidate.get("importance") or 0.0),
            )
            existing["confidence"] = max(existing_confidence, candidate_confidence)
            if candidate_confidence > existing_confidence:
                existing["kind"] = candidate.get("kind") or existing.get("kind")

    ordered: list[dict[str, Any]] = []
    for key in order:
        candidate = merged[key]
        twin = next((kept for kept in ordered if _near_duplicate(kept, candidate)), None)
        if twin is None:
            ordered.append(candidate)
            continue
        # paraphrase of something already kept: fold into the stronger one
        twin["lanes"] |= candidate["lanes"]
        twin["sensitive"] = bool(twin.get("sensitive") or candidate.get("sensitive"))
        twin["importance"] = max(twin["importance"], candidate["importance"])
        if candidate["confidence"] > twin["confidence"]:
            twin.update({k: candidate[k] for k in ("kind", "content", "subject", "attribute", "evidence")})
            twin["confidence"] = candidate["confidence"]
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
    if candidate.get("sensitive"):
        tags.append("sensitive")  # keeps it out of unprompted context injection
    for lane in sorted(str(lane).strip() for lane in (candidate.get("lanes") or set()) if str(lane).strip()):
        tags.append(f"lane:{lane}")
    return tags


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class _SessionSlice:
    session_id: int
    transcript: str
    user_chars: int
    last_at: datetime | None


async def _collect_session_slices(
    db: AsyncSession,
    user_id: int,
    since: datetime,
    *,
    session_ids: list[int] | None = None,
    respect_markers: bool = True,
) -> list[_SessionSlice]:
    """One user-led slice per session, oldest session first.

    User turns are kept (capped); assistant turns are clipped to a short
    context snippet. Sessions without any user turn are skipped. With
    respect_markers, only messages newer than the session's extraction
    high-water mark are included, so nothing is extracted twice.
    """
    conditions = [
        ChatSession.user_id == user_id,
        ChatSession.is_incognito == False,  # never read incognito sessions
        ChatSession.is_task_session == False,
        ChatMessage.created_at >= since,
        ChatMessage.role.in_(["user", "assistant"]),
    ]
    if session_ids is not None:
        conditions.append(ChatSession.id.in_(session_ids))
    if respect_markers:
        conditions.append(
            or_(
                ChatSession.memory_extracted_at.is_(None),
                ChatMessage.created_at > ChatSession.memory_extracted_at,
            )
        )
    rows = await db.execute(
        select(ChatMessage.session_id, ChatMessage.role, ChatMessage.content, ChatMessage.created_at)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(and_(*conditions))
        .order_by(ChatMessage.session_id.asc(), ChatMessage.created_at.asc(), ChatMessage.id.asc())
    )
    lines: dict[int, list[str]] = {}
    user_chars: dict[int, int] = {}
    last_at: dict[int, datetime] = {}
    for session_id, role, content, created_at in rows.all():
        if not str(content or "").strip():
            continue
        if created_at is not None:
            last_at[session_id] = max(last_at.get(session_id, created_at), created_at)
        if role == "user":
            user_chars[session_id] = user_chars.get(session_id, 0) + len(str(content).strip())
            lines.setdefault(session_id, []).append(f"user: {_clip(content, USER_TURN_CHAR_CAP)}")
        else:
            lines.setdefault(session_id, []).append(
                f"assistant: {_clip(content, ASSISTANT_TURN_CHAR_CAP)}"
            )
    return [
        _SessionSlice(sid, "\n".join(lines[sid]), user_chars[sid], last_at.get(sid))
        for sid in sorted(lines)
        if sid in user_chars
    ]


async def _recent_session_transcripts(db: AsyncSession, user_id: int, since: datetime) -> list[str]:
    slices = await _collect_session_slices(db, user_id, since, respect_markers=False)
    return [s.transcript for s in slices]


async def _mark_sessions_extracted(db: AsyncSession, slices: list[_SessionSlice]) -> None:
    for item in slices:
        if item.last_at is None:
            continue
        await db.execute(
            update(ChatSession)
            .where(
                ChatSession.id == item.session_id,
                or_(
                    ChatSession.memory_extracted_at.is_(None),
                    ChatSession.memory_extracted_at < item.last_at,
                ),
            )
            .values(memory_extracted_at=item.last_at)
        )


def _pack_chunks(session_transcripts: list[str]) -> list[str]:
    """Pack whole sessions into chunks up to MAX_CHUNK_CHARS; an oversized
    session keeps its most recent tail. Newest chunks win the chunk cap."""
    chunks: list[str] = []
    current = ""
    for transcript in session_transcripts:
        transcript = transcript[-MAX_CHUNK_CHARS:]
        if current and len(current) + len(transcript) + 2 > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{transcript}" if current else transcript
    if current:
        chunks.append(current)
    return chunks[-MAX_CHUNKS_PER_USER:]


async def _recent_transcript(db: AsyncSession, user_id: int, since: datetime) -> str:
    transcript = "\n\n".join(await _recent_session_transcripts(db, user_id, since))
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
    session_ids: list[int] | None = None,
    respect_markers: bool = True,
) -> dict[str, int]:
    """Extract, queue, and (narrowly) auto-approve memory candidates for one
    user. Returns counters for observability. Caller commits.

    Serialized process-wide so idle-session and nightly runs never extract
    (or write proposals for) the same content concurrently."""
    global _run_lock
    if _run_lock is None:
        _run_lock = asyncio.Lock()
    async with _run_lock:
        return await _run_memory_extraction_for_user(
            db, user_id, since_hours=since_hours, session_ids=session_ids, respect_markers=respect_markers
        )


async def _run_memory_extraction_for_user(
    db: AsyncSession,
    user_id: int,
    *,
    since_hours: int,
    session_ids: list[int] | None,
    respect_markers: bool,
) -> dict[str, int]:
    stats = {key: 0 for key in _STAT_KEYS}
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    slices = await _collect_session_slices(
        db, user_id, since, session_ids=session_ids, respect_markers=respect_markers
    )
    substantive = [s for s in slices if s.user_chars >= MIN_USER_CHARS]
    trivial = [s for s in slices if s.user_chars < MIN_USER_CHARS]
    if trivial:
        await _mark_sessions_extracted(db, trivial)  # nothing to learn; don't rescan
    if not substantive:
        stats["sessions"] = len(slices)
        return stats
    chunks = _pack_chunks([s.transcript for s in substantive])

    call_stats = _CallStats()
    lane_results: list[tuple[str, list[dict[str, Any]]]] = []
    for chunk in chunks:
        for lane in ("household", "project"):
            lane_results.append(
                (
                    lane,
                    await _extract_candidates_for_lane(
                        chunk, lane, stats=call_stats, db=db, user_id=user_id
                    ),
                )
            )
    candidates = _merge_lane_candidates(lane_results)
    stats["candidates"] = len(candidates)
    stats["sessions"] = len(slices)
    for key in ("llm_calls", "empty_responses", "parse_failures", "ungrounded", "filtered"):
        stats[key] = getattr(call_stats, key)
    unusable = call_stats.llm_calls and (
        call_stats.empty_responses + call_stats.parse_failures >= call_stats.llm_calls
    )
    if not unusable:
        await _mark_sessions_extracted(db, substantive)
    if not candidates:
        log.info("memory.extraction_completed", user_id=user_id, **stats)
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
            and candidate["kind"] == "fact"
            and not would_supersede
            and not candidate.get("sensitive")
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
                else "Sensitive content — operator review required" if candidate.get("sensitive")
                else "Standing directive — operator review required" if candidate["kind"] == "directive"
                else "Journal entry — operator review required" if candidate["kind"] == "journal"
                else ""
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
                    "sensitive": bool(candidate.get("sensitive")),
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


async def run_nightly_memory_extraction(
    db: AsyncSession, *, since_hours: int = 24, respect_markers: bool = True
) -> dict[str, int]:
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
    totals = {key: 0 for key in _STAT_KEYS}
    totals["users"] = 0
    totals["failed_users"] = 0
    for (user_id,) in users.all():
        try:
            stats = await run_memory_extraction_for_user(
                db, int(user_id), since_hours=since_hours, respect_markers=respect_markers
            )
        except Exception:
            totals["failed_users"] += 1
            log.warning("memory.extraction_failed", user_id=user_id, exc_info=True)
            continue
        totals["users"] += 1
        for key in _STAT_KEYS:
            totals[key] += stats[key]
    return totals


def extraction_totals_unusable(totals: dict[str, int]) -> str | None:
    """Reason string when a run produced no usable model output at all, so the
    caller can fail loudly instead of reporting a quiet "0 candidates"."""
    if totals.get("failed_users") and not totals.get("users"):
        return f"memory extraction failed for all {totals['failed_users']} user(s)"
    calls = int(totals.get("llm_calls") or 0)
    bad = int(totals.get("empty_responses") or 0) + int(totals.get("parse_failures") or 0)
    if calls and bad >= calls:
        return (
            f"memory extraction model returned no usable output "
            f"({totals.get('empty_responses', 0)} empty, {totals.get('parse_failures', 0)} unparseable "
            f"of {calls} calls); check the extraction model and token budget"
        )
    return None
