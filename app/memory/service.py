"""
FruitcakeAI v5 — MemoryService
Phase 4: Persistent per-user memory with 3-tier semantic retrieval.

Tier 1 — Standing: all active semantic + procedural memories (always included)
Tier 2 — Recent high-importance: episodic, last 7 days, importance >= 0.6
Tier 3 — Query-similar: top-k episodic via cosine distance (pgvector)

Write-time deduplication: cosine distance < 0.12 suppresses a new memory
that is semantically equivalent to an existing active one.

Memory immutability: never edit, only deactivate + create new.
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

# pgvector cosine distance threshold for write-time deduplication
DEDUP_THRESHOLD = 0.12

# Tier 2: only include recent episodic memories above this importance
TIER2_IMPORTANCE_FLOOR = 0.6
TIER2_DAYS = 7

# Tier 3: how many similar episodic memories to include
TIER3_TOP_K = 5

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
    ) -> list[Memory]:
        """
        Build the memory context for an agent session.

        Returns a deduplicated list of Memory objects across all 3 tiers,
        ordered: standing (tier 1) → recent high-importance (tier 2) →
        query-similar (tier 3).
        """
        now = datetime.now(timezone.utc)
        seen_ids: set[int] = set()
        results: list[Memory] = []

        # --- Tier 1: standing memories (semantic + procedural, always included) ---
        tier1 = await db.execute(
            select(Memory).where(
                and_(
                    Memory.user_id == user_id,
                    Memory.is_active == True,
                    Memory.memory_type.in_(["semantic", "procedural"]),
                    # exclude expired records
                    (Memory.expires_at == None) | (Memory.expires_at > now),
                )
            )
        )
        for m in tier1.scalars().all():
            seen_ids.add(m.id)
            results.append(m)

        # --- Tier 2: recent high-importance episodic ---
        cutoff = now - timedelta(days=TIER2_DAYS)
        tier2 = await db.execute(
            select(Memory).where(
                and_(
                    Memory.user_id == user_id,
                    Memory.is_active == True,
                    Memory.memory_type == "episodic",
                    Memory.importance >= TIER2_IMPORTANCE_FLOOR,
                    Memory.created_at >= cutoff,
                    (Memory.expires_at == None) | (Memory.expires_at > now),
                )
            ).order_by(Memory.importance.desc())
        )
        for m in tier2.scalars().all():
            if m.id not in seen_ids:
                seen_ids.add(m.id)
                results.append(m)

        # --- Tier 3: query-similar episodic (pgvector only) ---
        if query and _USE_PGVECTOR:
            embedding = await _embed(query)
            if embedding is not None:
                try:
                    tier3 = await db.execute(
                        select(Memory).where(
                            and_(
                                Memory.user_id == user_id,
                                Memory.is_active == True,
                                Memory.memory_type == "episodic",
                                (Memory.expires_at == None) | (Memory.expires_at > now),
                                Memory.embedding.isnot(None),
                            )
                        ).order_by(
                            Memory.embedding.cosine_distance(embedding)
                        ).limit(TIER3_TOP_K)
                    )
                    for m in tier3.scalars().all():
                        if m.id not in seen_ids:
                            seen_ids.add(m.id)
                            results.append(m)
                except Exception:
                    log.warning("memory.tier3_failed", exc_info=True)

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
        duplicate = await self._find_near_duplicate(db, user_id, text, resolved_kind)
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
    ) -> Memory | None:
        """Find an active head memory semantically equivalent to `content`.

        Uses pgvector cosine distance when available; otherwise a bounded
        text-similarity fallback so SQLite deployments and tests still get
        write-time dedup rather than silently skipping it.
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
                    if found is not None:
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
                "semantic": "[fact]",
                "procedural": "[rule]",
                "episodic": "[memory]",
            }.get(m.memory_type, "[memory]")
            lines.append(f"{prefix} {m.content}")
        return "\n".join(lines)


# Module-level singleton
_service: MemoryService | None = None


def get_memory_service() -> MemoryService:
    global _service
    if _service is None:
        _service = MemoryService()
    return _service
