"""
FruitcakeAI v5 — Memories API (Phase 4)

GET    /memories                         List active memories for current user
POST   /memories                         Create a memory (for admin/testing — agent uses create_memory tool)
POST   /memories/{id}/recall            Record an explicit memory recall/open action
PATCH  /memories/{id}                   Update importance or tags
DELETE /memories/{id}                   Deactivate (soft-delete)
POST   /memories/graph/entities         Create a graph entity
POST   /memories/graph/relations        Create a graph relation
POST   /memories/graph/observations     Add a graph observation
GET    /memories/graph/search           Search graph entities
GET    /memories/graph/entities/{id}    Open one graph node
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import Memory, MemoryEntity, MemoryObservation, MemoryRelation, User
from app.db.session import get_db
from app.memory.graph_service import get_graph_memory_service
from app.memory.service import get_memory_service

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class MemoryCreate(BaseModel):
    memory_type: str                     # "semantic" | "procedural" | "episodic"
    content: str
    importance: float = 0.5
    tags: List[str] = []
    expires_at: Optional[datetime] = None


class MemoryPatch(BaseModel):
    importance: Optional[float] = None
    tags: Optional[List[str]] = None


class MemoryOut(BaseModel):
    id: int
    memory_type: str
    content: str
    importance: float
    access_count: int
    last_accessed_at: Optional[datetime]
    tags: List[str]
    is_active: bool
    expires_at: Optional[datetime]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True

    @classmethod
    def from_orm(cls, obj: Memory) -> "MemoryOut":
        return cls(
            id=obj.id,
            memory_type=obj.memory_type,
            content=obj.content,
            importance=obj.importance,
            access_count=obj.access_count,
            last_accessed_at=obj.last_accessed_at,
            tags=obj.tags_list,
            is_active=obj.is_active,
            expires_at=obj.expires_at,
            created_at=obj.created_at,
        )


class MemoryEntityCreate(BaseModel):
    name: str
    entity_type: str = "unknown"
    aliases: List[str] = []
    confidence: float = 0.5


class MemoryRelationCreate(BaseModel):
    from_entity_id: int
    to_entity_id: int
    relation_type: str
    confidence: float = 0.5
    source_memory_id: Optional[int] = None
    source_session_id: Optional[int] = None
    source_task_id: Optional[int] = None


class MemoryObservationCreate(BaseModel):
    entity_id: int
    content: Optional[str] = None
    observed_at: Optional[datetime] = None
    confidence: float = 0.5
    source_memory_id: Optional[int] = None
    source_session_id: Optional[int] = None
    source_task_id: Optional[int] = None


class MemoryEntityOut(BaseModel):
    id: int
    name: str
    entity_type: str
    aliases: List[str]
    confidence: float
    is_active: bool

    class Config:
        from_attributes = True

    @classmethod
    def from_orm(cls, obj: MemoryEntity) -> "MemoryEntityOut":
        return cls(
            id=obj.id,
            name=obj.name,
            entity_type=obj.entity_type,
            aliases=obj.aliases_list,
            confidence=obj.confidence,
            is_active=obj.is_active,
        )


class MemoryRelationOut(BaseModel):
    id: int
    from_entity_id: int
    to_entity_id: int
    relation_type: str
    confidence: float
    source_memory_id: Optional[int]
    source_session_id: Optional[int]
    source_task_id: Optional[int]

    class Config:
        from_attributes = True


class MemoryObservationOut(BaseModel):
    id: int
    entity_id: int
    content: Optional[str]
    observed_at: Optional[datetime]
    confidence: float
    source_memory_id: Optional[int]
    source_session_id: Optional[int]
    source_task_id: Optional[int]

    class Config:
        from_attributes = True


class MemoryGraphNodeOut(BaseModel):
    entity: MemoryEntityOut
    relations: List[MemoryRelationOut]
    observations: List[MemoryObservationOut]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/memories", response_model=List[MemoryOut])
async def list_memories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    include_inactive: bool = False,
):
    filters = [Memory.user_id == current_user.id]
    if not include_inactive:
        filters.append(Memory.is_active == True)

    result = await db.execute(
        select(Memory)
        .where(and_(*filters))
        .order_by(desc(Memory.importance), desc(Memory.created_at))
    )
    memories = result.scalars().all()
    return [MemoryOut.from_orm(m) for m in memories]


@router.post("/memories", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def create_memory(
    body: MemoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.memory_type not in ("semantic", "procedural", "episodic"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="memory_type must be 'semantic', 'procedural', or 'episodic'",
        )

    svc = get_memory_service()
    result = await svc.create(
        db=db,
        user_id=current_user.id,
        memory_type=body.memory_type,
        content=body.content,
        importance=body.importance,
        tags=body.tags,
        expires_at=body.expires_at,
    )

    if isinstance(result, str):
        # Dedup suppression
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result,
        )

    return MemoryOut.from_orm(result)


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
async def update_memory(
    memory_id: int,
    body: MemoryPatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    memory = await _get_owned_memory(memory_id, current_user.id, db)

    if body.importance is not None:
        memory.importance = max(0.0, min(1.0, body.importance))
    if body.tags is not None:
        memory.tags_list = body.tags

    return MemoryOut.from_orm(memory)


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = get_memory_service()
    found = await svc.deactivate(db=db, memory_id=memory_id, user_id=current_user.id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")


@router.post("/memories/{memory_id}/recall", response_model=MemoryOut)
async def recall_memory(
    memory_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    memory = await _get_owned_memory(memory_id, current_user.id, db)
    svc = get_memory_service()
    await svc.mark_accessed([memory.id], mode="direct_recall", db=db)
    await db.refresh(memory)
    return MemoryOut.from_orm(memory)


@router.post("/memories/graph/entities", response_model=MemoryEntityOut, status_code=status.HTTP_201_CREATED)
async def create_graph_entity(
    body: MemoryEntityCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = get_graph_memory_service()
    try:
        entity = await svc.create_entity(
            db=db,
            user_id=current_user.id,
            name=body.name,
            entity_type=body.entity_type,
            aliases=body.aliases,
            confidence=body.confidence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return MemoryEntityOut.from_orm(entity)


@router.post("/memories/graph/relations", response_model=MemoryRelationOut, status_code=status.HTTP_201_CREATED)
async def create_graph_relation(
    body: MemoryRelationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = get_graph_memory_service()
    try:
        relation = await svc.create_relation(
            db=db,
            user_id=current_user.id,
            from_entity_id=body.from_entity_id,
            to_entity_id=body.to_entity_id,
            relation_type=body.relation_type,
            confidence=body.confidence,
            source_memory_id=body.source_memory_id,
            source_session_id=body.source_session_id,
            source_task_id=body.source_task_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return MemoryRelationOut.model_validate(relation)


@router.post("/memories/graph/observations", response_model=MemoryObservationOut, status_code=status.HTTP_201_CREATED)
async def add_graph_observation(
    body: MemoryObservationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = get_graph_memory_service()
    try:
        observation = await svc.add_observation(
            db=db,
            user_id=current_user.id,
            entity_id=body.entity_id,
            content=body.content,
            observed_at=body.observed_at,
            confidence=body.confidence,
            source_memory_id=body.source_memory_id,
            source_session_id=body.source_session_id,
            source_task_id=body.source_task_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return MemoryObservationOut.model_validate(observation)


@router.get("/memories/graph/search", response_model=List[MemoryEntityOut])
async def search_graph_entities(
    q: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 10,
):
    svc = get_graph_memory_service()
    entities = await svc.search_entities(db=db, user_id=current_user.id, query=q, limit=limit)
    return [MemoryEntityOut.from_orm(entity) for entity in entities]


@router.get("/memories/graph/entities/{entity_id}", response_model=MemoryGraphNodeOut)
async def open_graph_entity(
    entity_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = get_graph_memory_service()
    try:
        graph = await svc.open_entity_graph(db=db, user_id=current_user.id, entity_id=entity_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory entity not found")
    return MemoryGraphNodeOut(
        entity=MemoryEntityOut.from_orm(graph["entity"]),
        relations=[MemoryRelationOut.model_validate(item) for item in graph["relations"]],
        observations=[MemoryObservationOut.model_validate(item) for item in graph["observations"]],
    )


# ── Internal helper ───────────────────────────────────────────────────────────

async def _get_owned_memory(memory_id: int, user_id: int, db: AsyncSession) -> Memory:
    result = await db.execute(
        select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id)
    )
    m = result.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    return m
