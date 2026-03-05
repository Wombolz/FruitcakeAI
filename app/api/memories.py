"""
FruitcakeAI v5 — Memories API (Phase 4)

GET    /memories           List active memories for current user
POST   /memories           Create a memory (for admin/testing — agent uses create_memory tool)
PATCH  /memories/{id}      Update importance or tags
DELETE /memories/{id}      Deactivate (soft-delete)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, desc, select
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
            tags=obj.tags_list,
            is_active=obj.is_active,
            expires_at=obj.expires_at,
            created_at=obj.created_at,
        )


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


# ── Internal helper ───────────────────────────────────────────────────────────

async def _get_owned_memory(memory_id: int, user_id: int, db: AsyncSession) -> Memory:
    result = await db.execute(
        select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id)
    )
    m = result.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    return m
