"""
FruitcakeAI v5 — MemoryService

Memory v2: memory is a budgeted context product, not a store. The store may
grow without bound; what enters the prompt each turn is small, ranked, and
budgeted (memory_context_token_budget):

- directive: standing rules, always injected, small by construction (capped)
- profile:   facts tagged "profile" (household basics), always injected, capped
- fact:      durable subject-keyed facts, injected only when relevant to the
             query (relevance gate), ranked by relevance x importance
- journal:   time-bound events, relevance-gated and decayed by age
- sensitive: conflict/legal/medical-type memories are never injected
             unprompted; only a strong match to the query admits them

Only heads are retrievable (is_active, not superseded). Writes flow through
one enforced pipeline (propose_write): exclusions, near-dup, supersede, caps.
Memory immutability: never edit, only supersede/deactivate + create new.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Iterable, Literal

import structlog
from sqlalchemy import and_, func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Memory

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)

# Legacy cognitive taxonomy → retrieval-contract kinds.
# directive: always injected (small by construction, capped)
# fact:      durable subject-keyed household fact (head-per-subject_key)
# journal:   time-bound event, decays, retrieved only when query-relevant
KIND_FROM_MEMORY_TYPE = {
    "procedural": "directive",
    "semantic": "fact",
    "episodic": "journal",
}
MEMORY_TYPE_FROM_KIND = {v: k for k, v in KIND_FROM_MEMORY_TYPE.items()}
VALID_KINDS = frozenset(KIND_FROM_MEMORY_TYPE.values())

# Write-pipeline exclusions: material that belongs in tasks, plans, or
# runtime preservation — never in durable memory. Conservative on purpose;
# the system-prompt guidance still does the soft filtering, these catch the
# categories that pollute stores fastest.
_EXCLUDED_CONTENT_PATTERNS = (
    r"^\s*(?:currently|now)\s+(?:working on|doing|running)\b",
    r"\b(?:step \d+ of|in progress|todo:|to-do:)\b",
    r"\bthis (?:turn|session|conversation)\b.*\b(?:only|temporar)",
    r"^\s*the user (?:just )?(?:asked|said|wants) me to\b",
)


def _normalize_subject_key(subject: str | None, attribute: str | None) -> str | None:
    """Build the fact-identity key: 'joey:allergy'. Both parts required."""
    subject_part = re.sub(r"[^a-z0-9]+", "_", str(subject or "").strip().lower()).strip("_")
    attribute_part = re.sub(r"[^a-z0-9]+", "_", str(attribute or "").strip().lower()).strip("_")
    if not subject_part or not attribute_part:
        return None
    return f"{subject_part}:{attribute_part}"[:200]


def _normalized_content(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _text_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalized_content(a), _normalized_content(b)).ratio()


@dataclass
class MemoryWriteResult:
    """Outcome of a write proposal through the memory pipeline."""

    action: Literal["created", "deduplicated", "superseded", "refused"]
    memory: Memory | None
    reason: str = ""


def _estimate_tokens(text: str) -> int:
    return max(1, len(str(text or "")) // 4)


_WORD_RE = re.compile(r"[a-z0-9]+")

# Facts never score to zero: a fact with no lexical overlap may still be a
# vocabulary mismatch rather than a true irrelevance (SQLite deployments have
# no embeddings). The floor keeps high-importance facts able to compete for
# leftover budget while real matches rank far above them.
PROFILE_TAG = "profile"
SENSITIVE_TAGS = frozenset(
    {"sensitive", "legal", "custody", "incident", "co-parent", "co-parenting"}
)
SENSITIVE_CONTENT_RE = re.compile(
    r"\b(harass\w*|defam\w*|stalk\w*|abus\w*|assault\w*|restraining order|"
    r"lawsuit|sued|police|arrest\w*|custody|divorce|diagnos\w*|"
    r"threat\w*|blocked (?:him|her|them)|accus\w*)\b",
    re.IGNORECASE,
)

# Words that carry no retrieval signal; kept out of lexical relevance so
# "what is my name" does not match everything containing "is" and "my".
_STOPWORDS = frozenset(
    "a an and are as at be but by can did do does for from had has have how i "
    "if in into is it its me my of on or our so than that the their them then "
    "there these they this to us was we were what when where which who why "
    "will with would you your about tell show give please".split()
)


def is_sensitive_text(content: str) -> bool:
    return bool(SENSITIVE_CONTENT_RE.search(content or ""))


def is_sensitive_memory(memory: Memory) -> bool:
    """Sensitive memories are only injected on a strong match to the query."""
    tags = {str(t).strip().lower() for t in (memory.tags_list or [])}
    return bool(tags & SENSITIVE_TAGS) or is_sensitive_text(memory.content)


def is_profile_memory(memory: Memory) -> bool:
    tags = {str(t).strip().lower() for t in (memory.tags_list or [])}
    return PROFILE_TAG in tags and (memory.kind or "") == "fact" and not is_sensitive_memory(memory)


def _stem(word: str) -> str:
    """Ultra-light suffix stripping so 'prefers'/'prefer', 'visiting'/'visit'
    match without a stemming dependency. Deliberately crude — this only backs
    the lexical fallback path; pgvector handles morphology properly."""
    for suffix in ("ing", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in (_stem(w) for w in _WORD_RE.findall(str(text or "").lower()))
        if len(token) > 1
    }


def _content_terms(text: str) -> set[str]:
    return {
        token
        for token in (_stem(w) for w in _WORD_RE.findall(str(text or "").lower()) if w not in _STOPWORDS)
        if len(token) > 1
    }


def _build_idf(token_sets: list[set[str]]) -> dict[str, float]:
    """Inverse document frequency over the candidate pool. Rare terms (names,
    specific values) discriminate; terms shared across the store ("school",
    "family") barely count. Measured on the eval harness: flat overlap
    collapses once contents share vocabulary; IDF weighting restores rank."""
    import math

    df: dict[str, int] = {}
    for tokens in token_sets:
        for token in tokens:
            df[token] = df.get(token, 0) + 1
    n_docs = max(1, len(token_sets))
    return {token: 1.0 + math.log(n_docs / (1 + count)) for token, count in df.items()}


def _weighted_relevance(query_tokens: set[str], content_tokens: set[str], idf: dict[str, float]) -> float:
    """IDF-weighted share of the query's content terms found in the memory.

    Query terms absent from the whole pool still count (at rare-term weight),
    so a single shared word in a long query is a weak match, not a full one.
    """
    if not query_tokens or not content_tokens or not idf:
        return 0.0
    unknown_weight = max(idf.values())
    denom = sum(idf.get(t, unknown_weight) for t in query_tokens)
    if denom <= 0:
        return 0.0
    hit = sum(idf.get(t, 0.0) for t in query_tokens & content_tokens)
    return hit / denom


def _rare_term_hit(
    query_tokens: set[str], content_tokens: set[str], idf: dict[str, float]
) -> float:
    """Best shared term's specificity as a fraction of the most specific term
    in the pool (0.0 when nothing is shared). A name or place in the query
    that appears in few memories anchors them even when the rest of the
    question is phrased differently ("allergy" vs "allergic to nuts")."""
    if not idf:
        return 0.0
    top = max(idf.values())
    shared = [idf[t] for t in query_tokens & content_tokens if t in idf]
    return (max(shared) / top) if shared and top > 0 else 0.0


def _journal_decay(created_at: datetime | None, now: datetime) -> float:
    if created_at is None:
        return 1.0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / JOURNAL_HALF_LIFE_DAYS)


def _memory_score(memory: Memory, relevance: float, now: datetime) -> float:
    importance = max(0.0, min(1.0, memory.importance or 0.5))
    score = relevance * (0.4 + 0.6 * importance)
    if (memory.kind or "") == "journal":
        score *= _journal_decay(memory.created_at, now)
    return score

# pgvector cosine distance threshold for write-time deduplication
DEDUP_THRESHOLD = 0.12

# Retrieval scoring
JOURNAL_HALF_LIFE_DAYS = 14        # journal relevance halves every 2 weeks
CANDIDATE_POOL_LIMIT = 400         # facts+journal heads considered per turn
VECTOR_CANDIDATE_LIMIT = 60        # pgvector top-k merged into the pool
NO_QUERY_NEUTRAL_RELEVANCE = 0.5   # relevance when no query text exists
DIRECTIVE_BUDGET_FLOOR = 200       # tokens always left for facts/journal

_USE_PGVECTOR = settings.database_url.startswith("postgresql")


async def _embed(text: str) -> list[float] | None:
    """
    Generate an embedding for the given text.
    Returns None when pgvector is not available (e.g. SQLite in tests).
    Reuses the same embedding model already loaded by the RAG service.
    """
    if not _USE_PGVECTOR:
        return None
    try:
        from app.rag.service import get_rag_service
        svc = get_rag_service()
        # LlamaIndex embed model is loaded during RAGService.startup()
        embed_model = svc._index._embed_model if svc._loaded else None
        if embed_model is None:
            return None
        result = await embed_model.aget_text_embedding(text)
        return result
    except Exception:
        log.warning("memory.embed_failed", exc_info=True)
        return None


class MemoryService:
    """
    Async service for reading and writing agent memories.
    All methods accept an AsyncSession to fit the FastAPI dependency pattern.
    """

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def retrieve_for_context(
        self,
        db: AsyncSession,
        user_id: int,
        query: str | None = None,
        *,
        token_budget: int | None = None,
    ) -> list[Memory]:
        """
        Build the memory context for an agent turn: ranked and budgeted.

        - directives are always included (small by construction — write cap)
        - facts and journal heads are scored relevance x importance, journal
          additionally decayed by age, then greedily fill the remaining
          token budget
        Returns directives first, then scored memories in rank order.
        """
        budget = int(token_budget or settings.memory_context_token_budget)
        now = datetime.now(timezone.utc)
        head_conditions = and_(
            Memory.user_id == user_id,
            Memory.is_active == True,
            Memory.superseded_by_id.is_(None),
            (Memory.expires_at == None) | (Memory.expires_at > now),
        )

        pool_rows = await db.execute(
            select(Memory)
            .where(head_conditions)
            .order_by(Memory.created_at.desc())
            .limit(CANDIDATE_POOL_LIMIT + 50)
        )
        heads = list(pool_rows.scalars().all())
        by_id = {m.id: m for m in heads}
        query_text = str(query or "").strip()

        # --- vector similarity for the nearest heads (pgvector only) ---
        vector_sim: dict[int, float] = {}
        if query_text and _USE_PGVECTOR:
            embedding = await _embed(query_text)
            if embedding is not None:
                try:
                    distance = Memory.embedding.cosine_distance(embedding).label("distance")
                    vector_rows = await db.execute(
                        select(Memory, distance)
                        .where(and_(head_conditions, Memory.embedding.isnot(None)))
                        .order_by(distance)
                        .limit(VECTOR_CANDIDATE_LIMIT)
                    )
                    for memory, dist in vector_rows.all():
                        by_id.setdefault(memory.id, memory)
                        vector_sim[memory.id] = max(0.0, 1.0 - float(dist if dist is not None else 1.0))
                except Exception:
                    log.warning("memory.vector_candidates_failed", exc_info=True)

        # --- lexical relevance (IDF-weighted, stopwords removed) ---
        lexical: dict[int, float] = {}
        rare_hit: dict[int, float] = {}
        if query_text:
            content_tokens = {mid: _content_terms(m.content) for mid, m in by_id.items()}
            idf = _build_idf(list(content_tokens.values()))
            query_tokens = _content_terms(query_text)
            for mid, tokens in content_tokens.items():
                lexical[mid] = _weighted_relevance(query_tokens, tokens, idf)
                rare_hit[mid] = _rare_term_hit(query_tokens, tokens, idf)

        min_vec = settings.memory_min_vector_similarity
        min_lex = settings.memory_min_lexical_relevance
        min_vec_sens = settings.memory_sensitive_min_vector_similarity
        min_lex_sens = settings.memory_sensitive_min_lexical_relevance

        # --- tier 1: directives (always, unless sensitive) + profile ---
        results: list[Memory] = []
        seen_ids: set[int] = set()
        directives = sorted(
            (m for m in by_id.values() if m.kind == "directive"),
            key=lambda m: (m.importance or 0.0, m.created_at or now),
            reverse=True,
        )
        for memory in directives:
            if is_sensitive_memory(memory):
                continue  # may still qualify below on a strong query match
            results.append(memory)
            seen_ids.add(memory.id)
        directive_tokens = sum(_estimate_tokens(m.content) for m in results)

        profile = sorted(
            (m for m in by_id.values() if is_profile_memory(m)),
            key=lambda m: (m.importance or 0.0, m.created_at or now),
            reverse=True,
        )[: settings.memory_profile_cap]
        for memory in profile:
            results.append(memory)
            seen_ids.add(memory.id)
        always_tokens = directive_tokens + sum(_estimate_tokens(m.content) for m in profile)
        remaining_budget = max(DIRECTIVE_BUDGET_FLOOR, budget - always_tokens)

        # --- tier 2: relevance-gated facts / journal / sensitive items ---
        scored: list[tuple[float, Memory]] = []
        for mid, memory in by_id.items():
            if mid in seen_ids:
                continue
            vec = vector_sim.get(mid, 0.0)
            lex = lexical.get(mid, 0.0)
            sensitive = is_sensitive_memory(memory)
            vec_gate = min_vec_sens if sensitive else min_vec
            lex_gate = min_lex_sens if sensitive else min_lex
            rare = rare_hit.get(mid, 0.0)
            rare_ok = (not sensitive) and rare >= settings.memory_rare_term_ratio
            if not (vec >= vec_gate or lex >= lex_gate or rare_ok):
                continue
            relevance = max(
                lex,
                (vec - 0.5) * 2.0 if vec >= vec_gate else 0.0,
                rare * 0.5 if rare_ok else 0.0,
            )
            score = _memory_score(memory, max(relevance, 0.01), now)
            if score > 0.0:
                scored.append((score, memory))

        used_tokens = 0
        relevant_count = 0
        for score, memory in sorted(scored, key=lambda pair: pair[0], reverse=True):
            if relevant_count >= settings.memory_max_relevant:
                break
            cost = _estimate_tokens(memory.content)
            if used_tokens + cost > remaining_budget:
                continue
            used_tokens += cost
            relevant_count += 1
            seen_ids.add(memory.id)
            results.append(memory)

        return results

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def propose_write(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        content: str,
        memory_type: str | None = None,
        kind: str | None = None,
        subject: str | None = None,
        attribute: str | None = None,
        importance: float = 0.5,
        tags: list[str] | None = None,
        expires_at: datetime | None = None,
        source: str = "chat_tool",
        confidence: float = 0.7,
    ) -> MemoryWriteResult:
        """
        The single enforced write path. Every memory write — agent tool,
        API, proposal approval, extraction — goes through this pipeline:

        1. exclusion rules (refused, with a reason the caller can surface)
        2. near-duplicate check (deduplicated: bump the existing head)
        3. subject_key conflict check (superseded: new head, audited chain)
        4. directive cap (refused unless the write supersedes)
        """
        text = str(content or "").strip()
        if not text:
            return MemoryWriteResult("refused", None, "Memory content is required.")

        resolved_kind = (kind or "").strip().lower() or KIND_FROM_MEMORY_TYPE.get(
            str(memory_type or "").strip().lower(), ""
        )
        if resolved_kind not in VALID_KINDS:
            return MemoryWriteResult(
                "refused",
                None,
                "Invalid memory kind. Use directive (standing rule), fact "
                "(durable subject fact), or journal (time-bound event).",
            )

        exclusion = self._excluded_reason(text)
        if exclusion:
            return MemoryWriteResult("refused", None, exclusion)

        subject_key = _normalize_subject_key(subject, attribute)

        # --- near-duplicate check (pgvector when available, text fallback) ---
        duplicate = await self._find_near_duplicate(
            db, user_id, text, resolved_kind, subject_key=subject_key
        )
        if duplicate is not None:
            duplicate.importance = max(duplicate.importance or 0.0, max(0.0, min(1.0, importance)))
            if subject_key and not duplicate.subject_key:
                duplicate.subject_key = subject_key
            await db.flush()
            log.info("memory.dedup_suppressed", user_id=user_id, existing_id=duplicate.id)
            return MemoryWriteResult(
                "deduplicated",
                duplicate,
                "An equivalent memory already exists; its importance was refreshed instead.",
            )

        # --- subject_key conflict: supersede the old head ---
        conflicting_head: Memory | None = None
        if subject_key:
            head = await db.execute(
                select(Memory).where(
                    and_(
                        Memory.user_id == user_id,
                        Memory.subject_key == subject_key,
                        Memory.is_active == True,
                        Memory.superseded_by_id.is_(None),
                    )
                ).limit(1)
            )
            conflicting_head = head.scalar_one_or_none()

        # --- directive cap (superseding an existing directive is exempt) ---
        if resolved_kind == "directive" and conflicting_head is None:
            active_directives = await db.execute(
                select(sa_func.count(Memory.id)).where(
                    and_(
                        Memory.user_id == user_id,
                        Memory.kind == "directive",
                        Memory.is_active == True,
                    )
                )
            )
            if int(active_directives.scalar() or 0) >= int(settings.memory_directive_cap):
                return MemoryWriteResult(
                    "refused",
                    None,
                    f"Directive cap reached ({settings.memory_directive_cap}). "
                    "Directives are standing rules and must stay few: supersede an "
                    "existing directive (same subject/attribute) or ask the user "
                    "which one to retire.",
                )

        memory = Memory(
            user_id=user_id,
            memory_type=MEMORY_TYPE_FROM_KIND[resolved_kind],
            kind=resolved_kind,
            subject_key=subject_key,
            content=text,
            importance=max(0.0, min(1.0, importance)),
            tags=json.dumps(tags or []),
            expires_at=expires_at,
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
        )
        embedding = await _embed(text)
        if embedding is not None:
            memory.embedding = embedding

        db.add(memory)
        await db.flush()  # get id without committing (caller handles commit)

        if conflicting_head is not None:
            conflicting_head.superseded_by_id = memory.id
            conflicting_head.is_active = False
            await db.flush()
            log.info(
                "memory.superseded",
                user_id=user_id,
                old_id=conflicting_head.id,
                new_id=memory.id,
                subject_key=subject_key,
            )
            return MemoryWriteResult(
                "superseded",
                memory,
                f"Replaced the previous memory for '{subject_key}' (old version kept in the audit chain).",
            )

        log.info("memory.created", memory_id=memory.id, user_id=user_id, kind=resolved_kind)
        return MemoryWriteResult("created", memory, "Memory saved.")

    async def create(
        self,
        db: AsyncSession,
        user_id: int,
        memory_type: str,
        content: str,
        importance: float = 0.5,
        tags: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> Memory | str:
        """
        Compatibility wrapper over propose_write for existing callers.

        Returns the resulting Memory (created/superseded/deduplicated head),
        or a string message when the pipeline refused the write.
        """
        result = await self.propose_write(
            db,
            user_id,
            content=content,
            memory_type=memory_type,
            importance=importance,
            tags=tags,
            expires_at=expires_at,
        )
        if result.action == "refused":
            return result.reason
        return result.memory

    def _excluded_reason(self, content: str) -> str | None:
        lowered = _normalized_content(content)
        if len(lowered) < 8:
            return "Memory content is too short to be a durable, self-contained statement."
        for pattern in _EXCLUDED_CONTENT_PATTERNS:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                return (
                    "This looks like temporary task/session state, which belongs in "
                    "tasks or plans rather than durable memory."
                )
        return None

    async def _find_near_duplicate(
        self,
        db: AsyncSession,
        user_id: int,
        content: str,
        kind: str,
        *,
        subject_key: str | None = None,
    ) -> Memory | None:
        """Find an active head memory semantically equivalent to `content`.

        Uses pgvector cosine distance when available; otherwise a bounded
        text-similarity fallback so SQLite deployments and tests still get
        write-time dedup rather than silently skipping it.

        Never matches across DIFFERENT subject_keys: "Emma attends Lincoln"
        and "Liam attends Lincoln" are near-identical strings but distinct
        facts — treating one as a duplicate of the other would silently
        swallow corrections (caught by the eval harness's conflict probes).
        """
        if _USE_PGVECTOR:
            embedding = await _embed(content)
            if embedding is not None:
                try:
                    dup = await db.execute(
                        select(Memory).where(
                            and_(
                                Memory.user_id == user_id,
                                Memory.is_active == True,
                                Memory.superseded_by_id.is_(None),
                                Memory.embedding.isnot(None),
                                Memory.embedding.cosine_distance(embedding) < DEDUP_THRESHOLD,
                            )
                        ).limit(1)
                    )
                    found = dup.scalar_one_or_none()
                    if found is not None and not (
                        found.subject_key and subject_key and found.subject_key != subject_key
                    ):
                        return found
                except Exception:
                    log.warning("memory.dedup_check_failed", exc_info=True)

        threshold = float(settings.memory_dedup_similarity_threshold)
        recent = await db.execute(
            select(Memory)
            .where(
                and_(
                    Memory.user_id == user_id,
                    Memory.is_active == True,
                    Memory.superseded_by_id.is_(None),
                    Memory.kind == kind,
                )
            )
            .order_by(Memory.created_at.desc())
            .limit(200)
        )
        for candidate in recent.scalars().all():
            if candidate.subject_key and subject_key and candidate.subject_key != subject_key:
                continue
            if _text_similarity(content, candidate.content or "") >= threshold:
                return candidate
        return None

    async def deactivate(self, db: AsyncSession, memory_id: int, user_id: int) -> bool:
        """
        Soft-delete a memory (set is_active=False).
        Returns True if found and deactivated, False if not found or wrong user.
        """
        result = await db.execute(
            select(Memory).where(
                and_(Memory.id == memory_id, Memory.user_id == user_id)
            )
        )
        memory = result.scalar_one_or_none()
        if memory is None:
            return False
        memory.is_active = False
        log.info("memory.deactivated", memory_id=memory_id, user_id=user_id)
        return True

    async def mark_accessed(
        self,
        memory_ids: Iterable[int],
        *,
        mode: Literal["direct_recall", "task_materialized", "chat_materialized"],
        db: AsyncSession | None = None,
    ) -> None:
        """
        Record deliberate memory use. Passive retrieval paths must not call this.
        """
        ids = [int(m) for m in memory_ids if m is not None]
        if not ids:
            return
        await self._record_accesses(ids, mode=mode, db=db)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _record_accesses(
        self,
        memory_ids: list[int],
        *,
        mode: str,
        db: AsyncSession | None = None,
    ) -> None:
        """
        Increment access_count for each accessed memory.
        Used only for explicit recall/material-use paths.
        """
        if not memory_ids:
            return
        try:
            now = datetime.now(timezone.utc)
            if db is not None:
                result = await db.execute(
                    select(Memory).where(Memory.id.in_(memory_ids))
                )
                for m in result.scalars().all():
                    m.access_count = (m.access_count or 0) + 1
                    m.last_accessed_at = now
                await db.flush()
            else:
                from app.db.session import AsyncSessionLocal

                async with AsyncSessionLocal() as db_session:
                    result = await db_session.execute(
                        select(Memory).where(Memory.id.in_(memory_ids))
                    )
                    for m in result.scalars().all():
                        m.access_count = (m.access_count or 0) + 1
                        m.last_accessed_at = now
                    await db_session.commit()
            log.info("memory.access_recorded", mode=mode, count=len(memory_ids))
        except Exception:
            log.warning("memory.record_access_failed", exc_info=True)

    def format_for_prompt(self, memories: list[Memory]) -> str:
        """
        Render a memory list into a compact block suitable for injection
        into an agent system prompt.
        """
        if not memories:
            return ""
        lines = ["## What I know about you\n"]
        for m in memories:
            prefix = {
                "directive": "[rule]",
                "fact": "[fact]",
                "journal": "[event]",
            }.get(m.kind or "", "[memory]")
            lines.append(f"{prefix} {m.content}")
        return "\n".join(lines)


# Module-level singleton
_service: MemoryService | None = None


def get_memory_service() -> MemoryService:
    global _service
    if _service is None:
        _service = MemoryService()
    return _service
