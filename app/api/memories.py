"""
FruitcakeAI v5 — Memories API (Phase 4)

GET    /memories                         List active memories for current user
POST   /memories                         Create a memory (for admin/testing — agent uses create_memory tool)
GET    /memories/export                  Export memories for current user
POST   /memories/bulk-delete             Deactivate all memories for current user
POST   /memories/{id}/recall            Record an explicit memory recall/open action
PATCH  /memories/{id}                   Update importance or tags
DELETE /memories/{id}                   Deactivate (soft-delete)
POST   /memories/graph/entities         Create a graph entity
POST   /memories/graph/relations        Create a graph relation
POST   /memories/graph/observations     Add a graph observation
PATCH  /memories/graph/entities/{id}    Update one graph entity
DELETE /memories/graph/entities/{id}    Soft-deactivate one graph entity
PATCH  /memories/graph/observations/{id} Update one graph observation
DELETE /memories/graph/observations/{id} Soft-deactivate one graph observation
GET    /memories/graph/search           Search graph entities
GET    /memories/graph/entities/{id}    Open one graph node
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import LLMUsageEvent, Memory, MemoryEntity, MemoryObservation, MemoryProposal, MemoryRelation, User
from app.db.session import get_db
from app.memory.graph_service import get_graph_memory_service
from app.memory.review_service import (
    create_flat_memory_from_proposal,
    decode_proposal_payload,
    sync_artifact_candidate_status,
)
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


class MemoryBulkDeleteResponse(BaseModel):
    deactivated_count: int
    deleted_at: datetime


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


class MemoryEntityPatch(BaseModel):
    name: Optional[str] = None
    entity_type: Optional[str] = None
    aliases: Optional[List[str]] = None
    confidence: Optional[float] = None
    is_active: Optional[bool] = None


class MemoryObservationPatch(BaseModel):
    content: Optional[str] = None
    observed_at: Optional[datetime] = None
    confidence: Optional[float] = None
    is_active: Optional[bool] = None


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


class MemoryEntitySummaryOut(BaseModel):
    id: int
    name: str
    entity_type: str


class MemoryEntityListOut(MemoryEntityOut):
    relation_count: int = 0
    observation_count: int = 0


class MemoryRelationOut(BaseModel):
    id: int
    from_entity: MemoryEntitySummaryOut
    to_entity: MemoryEntitySummaryOut
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
    is_active: bool
    source_memory_id: Optional[int]
    source_session_id: Optional[int]
    source_task_id: Optional[int]

    class Config:
        from_attributes = True


class MemoryGraphNodeOut(BaseModel):
    entity: MemoryEntityOut
    relation_count: int
    observation_count: int
    relations: List[MemoryRelationOut]
    observations: List[MemoryObservationOut]


class MemoryProposalOut(BaseModel):
    id: int
    proposal_type: str
    source_type: str
    status: str
    task_id: Optional[int]
    task_run_id: Optional[int]
    content: str
    confidence: float
    reason: Optional[str]
    created_at: Optional[datetime]
    resolved_at: Optional[datetime]
    resolved_by_user_id: Optional[int]
    approved_memory_id: Optional[int]
    proposal: Dict[str, Any]


class MemoryProposalApprovalOut(BaseModel):
    proposal: MemoryProposalOut
    memory: MemoryOut


class LLMUsageEventOut(BaseModel):
    scope_label: str
    task_id: Optional[int]
    session_id: Optional[int]
    task_run_id: Optional[int]
    source: str
    stage: Optional[str]
    model: str
    total_tokens: int
    estimated_cost_usd: Optional[float]
    created_at: Optional[datetime]


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


@router.get("/memories/usage", response_model=List[LLMUsageEventOut])
async def list_llm_usage_events(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
):
    bounded_limit = max(1, min(int(limit or 20), 100))
    result = await db.execute(
        select(LLMUsageEvent)
        .where(LLMUsageEvent.user_id == current_user.id)
        .order_by(desc(LLMUsageEvent.created_at), desc(LLMUsageEvent.id))
        .limit(bounded_limit)
    )
    rows = result.scalars().all()
    return [_llm_usage_event_out(row) for row in rows]


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


@router.get("/memories/review", response_model=List[MemoryProposalOut])
async def list_memory_review_proposals(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = None,
    source_type: Optional[str] = None,
):
    query = (
        select(MemoryProposal)
        .where(MemoryProposal.user_id == current_user.id)
        .order_by(
            MemoryProposal.status.asc(),
            desc(MemoryProposal.created_at),
            desc(MemoryProposal.id),
        )
    )
    if status_filter:
        query = query.where(MemoryProposal.status == status_filter)
    if source_type:
        query = query.where(MemoryProposal.source_type == source_type)
    result = await db.execute(query)
    proposals = result.scalars().all()
    return [_memory_proposal_out(proposal) for proposal in proposals]


@router.get("/memories/review/{proposal_id}", response_model=MemoryProposalOut)
async def get_memory_review_proposal(
    proposal_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proposal = await _get_owned_memory_proposal(db=db, proposal_id=proposal_id, user_id=current_user.id)
    return _memory_proposal_out(proposal)


@router.post("/memories/review/{proposal_id}/approve", response_model=MemoryProposalApprovalOut)
async def approve_memory_review_proposal(
    proposal_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proposal = await _get_owned_memory_proposal(db=db, proposal_id=proposal_id, user_id=current_user.id)
    if proposal.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Memory proposal has already been resolved",
        )
    memory = await create_flat_memory_from_proposal(db, proposal=proposal, user_id=current_user.id)
    proposal.status = "approved"
    proposal.approved_memory_id = memory.id
    proposal.resolved_by_user_id = current_user.id
    proposal.resolved_at = datetime.now(timezone.utc)
    await sync_artifact_candidate_status(db, proposal=proposal)
    await db.commit()
    await db.refresh(proposal)
    await db.refresh(memory)
    return MemoryProposalApprovalOut(
        proposal=_memory_proposal_out(proposal),
        memory=MemoryOut.from_orm(memory),
    )


@router.post("/memories/review/{proposal_id}/reject", response_model=MemoryProposalOut)
async def reject_memory_review_proposal(
    proposal_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proposal = await _get_owned_memory_proposal(db=db, proposal_id=proposal_id, user_id=current_user.id)
    if proposal.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Memory proposal has already been resolved",
        )
    proposal.status = "rejected"
    proposal.resolved_by_user_id = current_user.id
    proposal.resolved_at = datetime.now(timezone.utc)
    await sync_artifact_candidate_status(db, proposal=proposal)
    await db.commit()
    await db.refresh(proposal)
    return _memory_proposal_out(proposal)


@router.get("/memories/export")
async def export_memories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Memory)
        .where(Memory.user_id == current_user.id)
        .order_by(desc(Memory.importance), desc(Memory.created_at))
    )
    payload = [MemoryOut.from_orm(memory).model_dump(mode="json") for memory in result.scalars().all()]
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="fruitcakeai-memories.json"'},
    )


@router.post("/memories/bulk-delete", response_model=MemoryBulkDeleteResponse)
async def bulk_delete_memories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Memory).where(
            and_(
                Memory.user_id == current_user.id,
                Memory.is_active == True,
            )
        )
    )
    memories = list(result.scalars().all())
    deleted_at = datetime.now(timezone.utc)
    for memory in memories:
        memory.is_active = False
    return MemoryBulkDeleteResponse(
        deactivated_count=len(memories),
        deleted_at=deleted_at,
    )


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
    entity_lookup = await _load_entity_lookup(
        db,
        current_user.id,
        {relation.from_entity_id, relation.to_entity_id},
    )
    return _serialize_relation(relation, entity_lookup)


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


@router.patch("/memories/graph/entities/{entity_id}", response_model=MemoryEntityOut)
async def update_graph_entity(
    entity_id: int,
    body: MemoryEntityPatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = get_graph_memory_service()
    try:
        entity = await svc.get_owned_entity(db=db, entity_id=entity_id, user_id=current_user.id, include_inactive=True)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory entity not found")

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Entity name is required.")
        entity.name = name
        entity.normalized_name = " ".join(name.lower().split())
    if body.entity_type is not None:
        entity.entity_type = (body.entity_type or "unknown").strip() or "unknown"
    if body.aliases is not None:
        entity.aliases_list = [item.strip() for item in body.aliases if item and item.strip()]
    if body.confidence is not None:
        entity.confidence = max(0.0, min(1.0, body.confidence))
    if body.is_active is not None:
        entity.is_active = body.is_active
    entity.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(entity)
    return MemoryEntityOut.from_orm(entity)


@router.delete("/memories/graph/entities/{entity_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def deactivate_graph_entity(
    entity_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    svc = get_graph_memory_service()
    try:
        entity = await svc.get_owned_entity(db=db, entity_id=entity_id, user_id=current_user.id, include_inactive=True)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory entity not found")
    entity.is_active = False
    entity.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/memories/graph/observations/{observation_id}", response_model=MemoryObservationOut)
async def update_graph_observation(
    observation_id: int,
    body: MemoryObservationPatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = get_graph_memory_service()
    try:
        observation = await svc.get_owned_observation(
            db=db,
            observation_id=observation_id,
            user_id=current_user.id,
            include_inactive=True,
        )
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory observation not found")

    if body.content is not None:
        content = body.content.strip()
        if observation.source_memory_id is None and not content:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Observation requires content or source_memory_id.",
            )
        observation.content = content or None
    if body.observed_at is not None:
        observation.observed_at = body.observed_at
    if body.confidence is not None:
        observation.confidence = max(0.0, min(1.0, body.confidence))
    if body.is_active is not None:
        observation.is_active = body.is_active
    await db.commit()
    await db.refresh(observation)
    return MemoryObservationOut.model_validate(observation)


@router.delete(
    "/memories/graph/observations/{observation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def deactivate_graph_observation(
    observation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    svc = get_graph_memory_service()
    try:
        observation = await svc.get_owned_observation(
            db=db,
            observation_id=observation_id,
            user_id=current_user.id,
            include_inactive=True,
        )
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory observation not found")
    observation.is_active = False
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/memories/graph/entities", response_model=List[MemoryEntityListOut])
async def list_graph_entities(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
    include_inactive: bool = False,
):
    filters = [MemoryEntity.user_id == current_user.id]
    if not include_inactive:
        filters.append(MemoryEntity.is_active == True)

    result = await db.execute(
        select(MemoryEntity)
        .where(and_(*filters))
        .order_by(desc(MemoryEntity.confidence), desc(MemoryEntity.created_at))
        .limit(max(1, min(limit, 100)))
    )
    entities = list(result.scalars().all())
    counts = await _load_graph_counts(db, current_user.id, [entity.id for entity in entities])
    return [
        MemoryEntityListOut(
            **MemoryEntityOut.from_orm(entity).model_dump(),
            relation_count=counts["relations"].get(entity.id, 0),
            observation_count=counts["observations"].get(entity.id, 0),
        )
        for entity in entities
    ]


@router.get("/memories/graph/search", response_model=List[MemoryEntityListOut])
async def search_graph_entities(
    q: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 10,
):
    svc = get_graph_memory_service()
    entities = await svc.search_entities(db=db, user_id=current_user.id, query=q, limit=limit)
    counts = await _load_graph_counts(db, current_user.id, [entity.id for entity in entities])
    return [
        MemoryEntityListOut(
            **MemoryEntityOut.from_orm(entity).model_dump(),
            relation_count=counts["relations"].get(entity.id, 0),
            observation_count=counts["observations"].get(entity.id, 0),
        )
        for entity in entities
    ]


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
    entity_ids = {graph["entity"].id}
    for rel in graph["relations"]:
        entity_ids.add(rel.from_entity_id)
        entity_ids.add(rel.to_entity_id)
    entity_lookup = await _load_entity_lookup(db, current_user.id, entity_ids)
    return MemoryGraphNodeOut(
        entity=MemoryEntityOut.from_orm(graph["entity"]),
        relation_count=len(graph["relations"]),
        observation_count=len(graph["observations"]),
        relations=[_serialize_relation(item, entity_lookup) for item in graph["relations"]],
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


async def _get_owned_memory_proposal(
    *,
    db: AsyncSession,
    proposal_id: int,
    user_id: int,
) -> MemoryProposal:
    result = await db.execute(
        select(MemoryProposal).where(
            MemoryProposal.id == proposal_id,
            MemoryProposal.user_id == user_id,
        )
    )
    proposal = result.scalar_one_or_none()
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory proposal not found",
        )
    return proposal


def _memory_proposal_out(proposal: MemoryProposal) -> MemoryProposalOut:
    return MemoryProposalOut(
        id=proposal.id,
        proposal_type=proposal.proposal_type,
        source_type=proposal.source_type,
        status=proposal.status,
        task_id=proposal.task_id,
        task_run_id=proposal.task_run_id,
        content=proposal.content,
        confidence=float(proposal.confidence or 0.0),
        reason=proposal.reason,
        created_at=proposal.created_at,
        resolved_at=proposal.resolved_at,
        resolved_by_user_id=proposal.resolved_by_user_id,
        approved_memory_id=proposal.approved_memory_id,
        proposal=decode_proposal_payload(proposal.proposal_json),
    )


def _llm_usage_scope_label(event: LLMUsageEvent) -> str:
    if event.task_id is not None:
        return f"task:{event.task_id}"
    if event.session_id is not None:
        return f"chat:{event.session_id}"
    return f"source:{event.source}"


def _llm_usage_event_out(event: LLMUsageEvent) -> LLMUsageEventOut:
    return LLMUsageEventOut(
        scope_label=_llm_usage_scope_label(event),
        task_id=event.task_id,
        session_id=event.session_id,
        task_run_id=event.task_run_id,
        source=event.source,
        stage=event.stage,
        model=event.model,
        total_tokens=event.total_tokens,
        estimated_cost_usd=event.estimated_cost_usd,
        created_at=event.created_at,
    )


async def _load_entity_lookup(
    db: AsyncSession,
    user_id: int,
    entity_ids: set[int],
) -> dict[int, MemoryEntity]:
    if not entity_ids:
        return {}
    result = await db.execute(
        select(MemoryEntity).where(
            and_(MemoryEntity.user_id == user_id, MemoryEntity.id.in_(sorted(entity_ids)))
        )
    )
    return {entity.id: entity for entity in result.scalars().all()}


async def _load_graph_counts(
    db: AsyncSession,
    user_id: int,
    entity_ids: list[int],
) -> dict[str, dict[int, int]]:
    if not entity_ids:
        return {"relations": {}, "observations": {}}

    observation_counts_result = await db.execute(
        select(MemoryObservation.entity_id, func.count(MemoryObservation.id))
        .where(
            and_(
                MemoryObservation.user_id == user_id,
                MemoryObservation.entity_id.in_(entity_ids),
                MemoryObservation.is_active == True,
            )
        )
        .group_by(MemoryObservation.entity_id)
    )
    observation_counts = {int(entity_id): int(count) for entity_id, count in observation_counts_result.all()}

    relation_counts_result = await db.execute(
        select(MemoryRelation.from_entity_id, MemoryRelation.to_entity_id)
        .where(
            and_(
                MemoryRelation.user_id == user_id,
                or_(
                    MemoryRelation.from_entity_id.in_(entity_ids),
                    MemoryRelation.to_entity_id.in_(entity_ids),
                ),
            )
        )
    )
    relation_counts = {entity_id: 0 for entity_id in entity_ids}
    for from_entity_id, to_entity_id in relation_counts_result.all():
        if from_entity_id in relation_counts:
            relation_counts[int(from_entity_id)] += 1
        if to_entity_id in relation_counts:
            relation_counts[int(to_entity_id)] += 1

    return {"relations": relation_counts, "observations": observation_counts}


def _serialize_relation(
    relation: MemoryRelation,
    entity_lookup: dict[int, MemoryEntity],
) -> MemoryRelationOut:
    from_entity = entity_lookup.get(relation.from_entity_id)
    to_entity = entity_lookup.get(relation.to_entity_id)
    if from_entity is None or to_entity is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Memory graph relation integrity error")
    return MemoryRelationOut(
        id=relation.id,
        from_entity=MemoryEntitySummaryOut(
            id=from_entity.id,
            name=from_entity.name,
            entity_type=from_entity.entity_type,
        ),
        to_entity=MemoryEntitySummaryOut(
            id=to_entity.id,
            name=to_entity.name,
            entity_type=to_entity.entity_type,
        ),
        relation_type=relation.relation_type,
        confidence=relation.confidence,
        source_memory_id=relation.source_memory_id,
        source_session_id=relation.source_session_id,
        source_task_id=relation.source_task_id,
    )
