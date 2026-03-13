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
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Memory

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)

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

        # Record access for all returned memories (fire-and-forget).
        # Pass IDs only — _record_accesses opens its own session so the
        # caller's session can close without racing the background task.
        import asyncio
        if results:
            asyncio.create_task(self._record_accesses([m.id for m in results]))

        return results

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

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
        Persist a new memory for the user.

        Returns the created Memory, or a string message if a duplicate was
        suppressed (cosine distance < DEDUP_THRESHOLD against existing active memories).
        """
        embedding = await _embed(content)

        # Write-time deduplication (pgvector only)
        if embedding is not None and _USE_PGVECTOR:
            try:
                dup = await db.execute(
                    select(Memory).where(
                        and_(
                            Memory.user_id == user_id,
                            Memory.is_active == True,
                            Memory.embedding.isnot(None),
                            Memory.embedding.cosine_distance(embedding) < DEDUP_THRESHOLD,
                        )
                    ).limit(1)
                )
                if dup.scalar_one_or_none():
                    log.info("memory.dedup_suppressed", user_id=user_id)
                    return "Memory already exists (duplicate suppressed)"
            except Exception:
                log.warning("memory.dedup_check_failed", exc_info=True)

        memory = Memory(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            importance=max(0.0, min(1.0, importance)),
            tags=json.dumps(tags or []),
            expires_at=expires_at,
        )
        if embedding is not None:
            memory.embedding = embedding

        db.add(memory)
        await db.flush()  # get id without committing (caller handles commit)
        log.info("memory.created", memory_id=memory.id, user_id=user_id, type=memory_type)
        return memory

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _record_accesses(self, memory_ids: list[int]) -> None:
        """
        Increment access_count for each accessed memory.
        Runs as a fire-and-forget background task (never blocks retrieval).

        Opens its own session so the caller's session can close independently
        without causing a state-change race on the shared connection.
        """
        if not memory_ids:
            return
        try:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Memory).where(Memory.id.in_(memory_ids))
                )
                for m in result.scalars().all():
                    m.access_count = (m.access_count or 0) + 1
                await db.commit()
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
