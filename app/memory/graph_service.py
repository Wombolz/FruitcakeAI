from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Memory, MemoryEntity, MemoryObservation, MemoryRelation

log = structlog.get_logger(__name__)


def _normalize_entity_name(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


class GraphMemoryService:
    async def find_entity(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        name: str,
        entity_type: Optional[str] = None,
    ) -> MemoryEntity | None:
        normalized = _normalize_entity_name(name)
        if not normalized:
            return None
        filters = [
            MemoryEntity.user_id == user_id,
            MemoryEntity.is_active == True,
            MemoryEntity.normalized_name == normalized,
        ]
        if entity_type:
            filters.append(MemoryEntity.entity_type == entity_type)
        result = await db.execute(select(MemoryEntity).where(and_(*filters)).limit(1))
        return result.scalar_one_or_none()

    async def create_entity(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        confidence: float = 0.5,
    ) -> MemoryEntity:
        normalized = _normalize_entity_name(name)
        if not normalized:
            raise ValueError("Entity name is required.")
        entity = MemoryEntity(
            user_id=user_id,
            name=name.strip(),
            normalized_name=normalized,
            entity_type=(entity_type or "unknown").strip() or "unknown",
            confidence=max(0.0, min(1.0, confidence)),
            is_active=True,
        )
        entity.aliases_list = [a.strip() for a in (aliases or []) if a and a.strip()]
        db.add(entity)
        await db.flush()
        log.info("memory_graph.entity_created", entity_id=entity.id, user_id=user_id, name=entity.name)
        return entity

    async def find_or_create_entity(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        confidence: float = 0.5,
    ) -> tuple[MemoryEntity, bool]:
        found = await self.find_entity(db, user_id=user_id, name=name, entity_type=entity_type)
        if found is not None:
            return found, False
        entity = await self.create_entity(
            db,
            user_id=user_id,
            name=name,
            entity_type=entity_type,
            aliases=aliases,
            confidence=confidence,
        )
        return entity, True

    async def create_relation(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        from_entity_id: int,
        to_entity_id: int,
        relation_type: str,
        confidence: float = 0.5,
        source_memory_id: int | None = None,
        source_session_id: int | None = None,
        source_task_id: int | None = None,
    ) -> MemoryRelation:
        from_entity = await self._get_owned_entity(db, entity_id=from_entity_id, user_id=user_id)
        to_entity = await self._get_owned_entity(db, entity_id=to_entity_id, user_id=user_id)
        if from_entity.id == to_entity.id:
            raise ValueError("Relations must connect two distinct entities.")
        if not (relation_type or "").strip():
            raise ValueError("relation_type is required.")
        if source_memory_id is not None:
            await self._ensure_owned_memory(db, memory_id=source_memory_id, user_id=user_id)
        relation = MemoryRelation(
            user_id=user_id,
            from_entity_id=from_entity.id,
            to_entity_id=to_entity.id,
            relation_type=relation_type.strip(),
            confidence=max(0.0, min(1.0, confidence)),
            source_memory_id=source_memory_id,
            source_session_id=source_session_id,
            source_task_id=source_task_id,
        )
        db.add(relation)
        await db.flush()
        log.info("memory_graph.relation_created", relation_id=relation.id, user_id=user_id)
        return relation

    async def add_observation(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        entity_id: int,
        content: str | None = None,
        observed_at: datetime | None = None,
        confidence: float = 0.5,
        source_memory_id: int | None = None,
        source_session_id: int | None = None,
        source_task_id: int | None = None,
    ) -> MemoryObservation:
        entity = await self._get_owned_entity(db, entity_id=entity_id, user_id=user_id)
        if source_memory_id is None and not (content or "").strip():
            raise ValueError("Observation requires content or source_memory_id.")
        if source_memory_id is not None:
            await self._ensure_owned_memory(db, memory_id=source_memory_id, user_id=user_id)
        observation = MemoryObservation(
            user_id=user_id,
            entity_id=entity.id,
            content=(content or "").strip() or None,
            observed_at=observed_at,
            confidence=max(0.0, min(1.0, confidence)),
            source_memory_id=source_memory_id,
            source_session_id=source_session_id,
            source_task_id=source_task_id,
        )
        db.add(observation)
        await db.flush()
        log.info("memory_graph.observation_created", observation_id=observation.id, user_id=user_id)
        return observation

    async def search_entities(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        query: str,
        limit: int = 10,
    ) -> list[MemoryEntity]:
        normalized = _normalize_entity_name(query)
        if not normalized:
            return []
        result = await db.execute(
            select(MemoryEntity)
            .where(
                and_(
                    MemoryEntity.user_id == user_id,
                    MemoryEntity.is_active == True,
                )
            )
            .order_by(MemoryEntity.confidence.desc(), MemoryEntity.created_at.desc())
        )
        matches: list[MemoryEntity] = []
        for entity in result.scalars().all():
            aliases = [_normalize_entity_name(alias) for alias in entity.aliases_list]
            if normalized in entity.normalized_name or any(normalized in alias for alias in aliases):
                matches.append(entity)
        return matches[: max(1, min(limit, 25))]

    async def open_entity_graph(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        entity_id: int,
    ) -> dict[str, object]:
        entity = await self._get_owned_entity(db, entity_id=entity_id, user_id=user_id)
        relations_result = await db.execute(
            select(MemoryRelation).where(
                and_(
                    MemoryRelation.user_id == user_id,
                    or_(
                        MemoryRelation.from_entity_id == entity.id,
                        MemoryRelation.to_entity_id == entity.id,
                    ),
                )
            )
        )
        observations_result = await db.execute(
            select(MemoryObservation)
            .where(
                and_(
                    MemoryObservation.user_id == user_id,
                    MemoryObservation.entity_id == entity.id,
                )
            )
            .order_by(MemoryObservation.created_at.desc())
        )
        return {
            "entity": entity,
            "relations": list(relations_result.scalars().all()),
            "observations": list(observations_result.scalars().all()),
        }

    async def _get_owned_entity(self, db: AsyncSession, *, entity_id: int, user_id: int) -> MemoryEntity:
        result = await db.execute(
            select(MemoryEntity).where(
                and_(
                    MemoryEntity.id == entity_id,
                    MemoryEntity.user_id == user_id,
                    MemoryEntity.is_active == True,
                )
            )
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise ValueError("Memory entity not found.")
        return entity

    async def _ensure_owned_memory(self, db: AsyncSession, *, memory_id: int, user_id: int) -> Memory:
        result = await db.execute(
            select(Memory).where(and_(Memory.id == memory_id, Memory.user_id == user_id, Memory.is_active == True))
        )
        memory = result.scalar_one_or_none()
        if memory is None:
            raise ValueError("Source memory not found.")
        return memory


_service: GraphMemoryService | None = None


def get_graph_memory_service() -> GraphMemoryService:
    global _service
    if _service is None:
        _service = GraphMemoryService()
    return _service
