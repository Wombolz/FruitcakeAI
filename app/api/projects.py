"""User-owned project organization for chats and future session domains."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import ChatSession, User, UserProject
from app.db.session import get_db


router = APIRouter()


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    color: Optional[str] = Field(default=None, max_length=20)
    icon: Optional[str] = Field(default=None, max_length=100)


class ProjectPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    color: Optional[str] = Field(default=None, max_length=20)
    icon: Optional[str] = Field(default=None, max_length=100)
    archived: Optional[bool] = None


class ProjectOrderPatch(BaseModel):
    project_ids: list[int]


class ChatProjectPatch(BaseModel):
    project_id: Optional[int] = None


class ChatPinPatch(BaseModel):
    pinned: bool


class ProjectOut(BaseModel):
    id: int
    public_id: str
    name: str
    description: Optional[str]
    color: Optional[str]
    icon: Optional[str]
    sort_index: int
    archived_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ChatOrganizationOut(BaseModel):
    session_id: int
    project_id: Optional[int]
    is_pinned: bool


def _clean_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


async def _owned_project(project_id: int, user_id: int, db: AsyncSession) -> UserProject:
    project = (
        await db.execute(
            select(UserProject).where(
                UserProject.id == project_id,
                UserProject.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _owned_chat(session_id: int, user_id: int, db: AsyncSession) -> ChatSession:
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.user_id == user_id,
                ChatSession.is_task_session == False,
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    include_archived: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[UserProject]:
    query = select(UserProject).where(UserProject.user_id == current_user.id)
    if not include_archived:
        query = query.where(UserProject.archived_at.is_(None))
    result = await db.execute(query.order_by(UserProject.sort_index, UserProject.id))
    return list(result.scalars().all())


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserProject:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must not be blank")
    await db.execute(
        sa.update(UserProject)
        .where(UserProject.user_id == current_user.id, UserProject.archived_at.is_(None))
        .values(sort_index=UserProject.sort_index + 1)
    )
    project = UserProject(
        user_id=current_user.id,
        name=name,
        description=_clean_optional(body.description),
        color=_clean_optional(body.color),
        icon=_clean_optional(body.icon),
        sort_index=0,
    )
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return project


@router.patch("/projects/order", response_model=list[ProjectOut])
async def reorder_projects(
    body: ProjectOrderPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[UserProject]:
    projects = list(
        (
            await db.execute(
                select(UserProject).where(
                    UserProject.user_id == current_user.id,
                    UserProject.archived_at.is_(None),
                )
            )
        ).scalars().all()
    )
    current_ids = [project.id for project in projects]
    if len(body.project_ids) != len(set(body.project_ids)) or sorted(current_ids) != sorted(body.project_ids):
        raise HTTPException(status_code=422, detail="Project IDs must match active projects exactly")
    by_id = {project.id: project for project in projects}
    for index, project_id in enumerate(body.project_ids):
        by_id[project_id].sort_index = index
    await db.flush()
    for project_id in body.project_ids:
        await db.refresh(by_id[project_id])
    return [by_id[project_id] for project_id in body.project_ids]


@router.patch("/projects/{project_id:int}", response_model=ProjectOut)
async def update_project(
    project_id: int,
    body: ProjectPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserProject:
    project = await _owned_project(project_id, current_user.id, db)
    fields = body.model_fields_set
    if "name" in fields:
        assert body.name is not None
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="name must not be blank")
        project.name = name
    if "description" in fields:
        project.description = _clean_optional(body.description)
    if "color" in fields:
        project.color = _clean_optional(body.color)
    if "icon" in fields:
        project.icon = _clean_optional(body.icon)
    if "archived" in fields:
        project.archived_at = datetime.now(timezone.utc) if body.archived else None
    await db.flush()
    await db.refresh(project)
    return project


@router.delete("/projects/{project_id:int}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    project = await _owned_project(project_id, current_user.id, db)
    await db.execute(
        sa.update(ChatSession)
        .where(ChatSession.user_id == current_user.id, ChatSession.project_id == project.id)
        .values(project_id=None)
    )
    await db.delete(project)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/chat/sessions/{session_id:int}/project", response_model=ChatOrganizationOut)
async def assign_chat_project(
    session_id: int,
    body: ChatProjectPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatOrganizationOut:
    session = await _owned_chat(session_id, current_user.id, db)
    if body.project_id is not None:
        project = await _owned_project(body.project_id, current_user.id, db)
        if project.archived_at is not None:
            raise HTTPException(status_code=409, detail="Cannot assign a chat to an archived project")
    session.project_id = body.project_id
    await db.flush()
    return ChatOrganizationOut(
        session_id=session.id,
        project_id=session.project_id,
        is_pinned=session.is_pinned,
    )


@router.patch("/chat/sessions/{session_id:int}/pin", response_model=ChatOrganizationOut)
async def update_chat_pin(
    session_id: int,
    body: ChatPinPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatOrganizationOut:
    session = await _owned_chat(session_id, current_user.id, db)
    session.is_pinned = body.pinned
    await db.flush()
    return ChatOrganizationOut(
        session_id=session.id,
        project_id=session.project_id,
        is_pinned=session.is_pinned,
    )
