"""
FruitcakeAI v5 — Memories API (Phase 4)

GET    /memories           List active memories for current user
POST   /memories           Create a memory (for admin/testing — agent uses create_memory tool)
POST   /memories/{id}/recall  Record an explicit memory recall/open action
PATCH  /memories/{id}      Update importance or tags
DELETE /memories/{id}      Deactivate (soft-delete)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import Memory, User
from app.db.session import get_db
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


class BulkDeleteMemoriesOut(BaseModel):
    deactivated_count: int
    deleted_at: datetime


class MemoryExportOut(BaseModel):
    user_id: int
    exported_at: datetime
    memory_count: int
    memories: List[MemoryOut]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/memories", response_model=List[MemoryOut])
async def list_memories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    include_inactive: bool = False,
):
    svc = get_memory_service()
    memories = await svc.list_for_user(db, current_user.id, include_inactive=include_inactive)
    memories.sort(key=lambda m: ((m.importance or 0.0), m.created_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return [MemoryOut.from_orm(m) for m in memories]


@router.get("/memories/export")
async def export_memories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = get_memory_service()
    memories = await svc.list_for_user(db, current_user.id, include_inactive=True)
    exported_at = datetime.now(timezone.utc)
    payload = MemoryExportOut(
        user_id=current_user.id,
        exported_at=exported_at,
        memory_count=len(memories),
        memories=[MemoryOut.from_orm(m) for m in memories],
    )
    filename = f"fruitcakeai-memories-user-{current_user.id}-{exported_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    return JSONResponse(
        content=jsonable_encoder(payload.model_dump()),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@router.post("/memories/bulk-delete", response_model=BulkDeleteMemoriesOut)
async def bulk_delete_memories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = get_memory_service()
    deleted_at = datetime.now(timezone.utc)
    deactivated_count = await svc.deactivate_all_for_user(db=db, user_id=current_user.id)
    return BulkDeleteMemoriesOut(
        deactivated_count=deactivated_count,
        deleted_at=deleted_at,
    )


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


# ── Internal helper ───────────────────────────────────────────────────────────

async def _get_owned_memory(memory_id: int, user_id: int, db: AsyncSession) -> Memory:
    result = await db.execute(
        select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id)
    )
    m = result.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    return m
