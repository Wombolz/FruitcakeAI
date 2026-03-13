"""FruitcakeAI v5 — RSS source and discovery APIs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import RSSSource, User
from app.db.session import get_db
from app.mcp.services import rss_sources

router = APIRouter()


class RSSSourceCreate(BaseModel):
    name: str
    url: str
    category: str = "news"
    update_interval_minutes: int = 60
    active: bool = True


class RSSSourcePatch(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    update_interval_minutes: Optional[int] = None
    active: Optional[bool] = None


class RSSSourceOut(BaseModel):
    id: int
    user_id: Optional[int]
    scope: str
    name: str
    url: str
    url_canonical: str
    category: str
    active: bool
    trust_level: str
    update_interval_minutes: int
    last_ok_at: Optional[str]
    last_error: Optional[str]


class RSSCandidateOut(BaseModel):
    id: int
    user_id: int
    seed_url: str
    url: str
    url_canonical: str
    title_hint: Optional[str]
    domain: str
    discovered_via: str
    status: str
    reason: Optional[str]
    reviewed_by: Optional[int]
    reviewed_at: Optional[str]
    created_at: Optional[str]


class DiscoverBody(BaseModel):
    seed_url: str
    max_candidates: int = 10


class ApproveBody(BaseModel):
    name: Optional[str] = None
    category: str = "news"


class RejectBody(BaseModel):
    reason: str = "Rejected by user"


@router.get("/rss/sources", response_model=List[RSSSourceOut])
async def list_sources(
    active_only: bool = False,
    category: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await rss_sources.list_effective_sources(
        db,
        user_id=current_user.id,
        active_only=active_only,
        category=category,
    )
    return rows


@router.post("/rss/sources", response_model=RSSSourceOut, status_code=status.HTTP_201_CREATED)
async def add_source(
    body: RSSSourceCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await rss_sources.add_source(
            db,
            user_id=current_user.id,
            name=body.name,
            url=body.url,
            category=body.category,
            update_interval_minutes=body.update_interval_minutes,
            active=body.active,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return {
        "id": row.id,
        "user_id": row.user_id,
        "scope": "user",
        "name": row.name,
        "url": row.url,
        "url_canonical": row.url_canonical,
        "category": row.category,
        "active": row.active,
        "trust_level": row.trust_level,
        "update_interval_minutes": row.update_interval_minutes,
        "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "last_error": row.last_error,
    }


@router.patch("/rss/sources/{source_id}", response_model=RSSSourceOut)
async def patch_source(
    source_id: int,
    body: RSSSourcePatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(RSSSource, source_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")

    if body.active is not None:
        try:
            row = await rss_sources.set_source_active(
                db,
                user_id=current_user.id,
                source_id=source_id,
                active=body.active,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    if row.user_id not in (current_user.id, None):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")

    # If user patches metadata on a global row, create override first.
    if row.user_id is None and any(v is not None for v in [body.name, body.category, body.update_interval_minutes]):
        row = await rss_sources.add_source(
            db,
            user_id=current_user.id,
            name=body.name or row.name,
            url=row.url,
            category=body.category or row.category,
            update_interval_minutes=body.update_interval_minutes or row.update_interval_minutes,
            active=row.active,
            trust_level="override",
        )
    else:
        if row.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
        if body.name is not None:
            row.name = body.name
        if body.category is not None:
            row.category = body.category
        if body.update_interval_minutes is not None:
            row.update_interval_minutes = max(5, min(int(body.update_interval_minutes), 1440))

    return {
        "id": row.id,
        "user_id": row.user_id,
        "scope": "user" if row.user_id else "global",
        "name": row.name,
        "url": row.url,
        "url_canonical": row.url_canonical,
        "category": row.category,
        "active": row.active,
        "trust_level": row.trust_level,
        "update_interval_minutes": row.update_interval_minutes,
        "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "last_error": row.last_error,
    }


@router.delete("/rss/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await rss_sources.remove_source(db, user_id=current_user.id, source_id=source_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found or not owned")


@router.get("/rss/candidates", response_model=List[RSSCandidateOut])
async def get_candidates(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await rss_sources.list_candidates(db, user_id=current_user.id, status=status)
    return [rss_sources.candidate_to_dict(r) for r in rows]


@router.post("/rss/candidates/discover", response_model=List[RSSCandidateOut])
async def discover_candidates(
    body: DiscoverBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        rows = await rss_sources.queue_discovered_candidates(
            db,
            user_id=current_user.id,
            seed_url=body.seed_url,
            max_candidates=max(1, min(body.max_candidates, 25)),
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return [rss_sources.candidate_to_dict(r) for r in rows]


@router.post("/rss/candidates/{candidate_id}/approve", response_model=RSSSourceOut)
async def approve_candidate(
    candidate_id: int,
    body: ApproveBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await rss_sources.approve_candidate(
            db,
            user_id=current_user.id,
            candidate_id=candidate_id,
            reviewer_id=current_user.id,
            name=body.name,
            category=body.category,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {
        "id": row.id,
        "user_id": row.user_id,
        "scope": "user",
        "name": row.name,
        "url": row.url,
        "url_canonical": row.url_canonical,
        "category": row.category,
        "active": row.active,
        "trust_level": row.trust_level,
        "update_interval_minutes": row.update_interval_minutes,
        "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "last_error": row.last_error,
    }


@router.post("/rss/candidates/{candidate_id}/reject", response_model=RSSCandidateOut)
async def reject_candidate(
    candidate_id: int,
    body: RejectBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await rss_sources.reject_candidate(
            db,
            user_id=current_user.id,
            candidate_id=candidate_id,
            reviewer_id=current_user.id,
            reason=body.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return rss_sources.candidate_to_dict(row)

