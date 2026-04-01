from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import Secret, User
from app.db.session import get_db
from app.db.models import SecretAccessEvent
from app.secrets_service import (
    SecretConfigurationError,
    encrypt_secret_value,
    list_secret_access_events,
    mask_secret_value,
    normalize_secret_name,
)

router = APIRouter()


class SecretCreate(BaseModel):
    name: str
    value: str
    provider: str = ""


class SecretUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    is_active: Optional[bool] = None


class SecretRotate(BaseModel):
    value: str


class SecretDisableOut(BaseModel):
    id: int
    is_active: bool


class SecretOut(BaseModel):
    id: int
    name: str
    provider: str
    masked_preview: str
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime]
    last_used_at: Optional[datetime]

    class Config:
        from_attributes = True


class SecretAccessEventOut(BaseModel):
    id: int
    secret_id: Optional[int]
    secret_name: str
    task_id: Optional[int]
    tool_name: str
    success: bool
    error_class: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


def _serialize_secret(secret: Secret) -> SecretOut:
    masked_preview = "****"
    try:
        from app.secrets_service import decrypt_secret_value

        masked_preview = mask_secret_value(decrypt_secret_value(secret.ciphertext))
    except SecretConfigurationError:
        masked_preview = "****"
    except Exception:
        pass
    return SecretOut(
        id=secret.id,
        name=secret.name,
        provider=secret.provider or "",
        masked_preview=masked_preview,
        is_active=bool(secret.is_active),
        created_at=secret.created_at,
        updated_at=secret.updated_at,
        last_used_at=secret.last_used_at,
    )


async def _get_owned_secret(db: AsyncSession, *, user_id: int, secret_id: int) -> Secret:
    result = await db.execute(select(Secret).where(Secret.id == secret_id, Secret.user_id == user_id))
    secret = result.scalar_one_or_none()
    if secret is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found")
    return secret


def _serialize_secret_access_event(event: SecretAccessEvent) -> SecretAccessEventOut:
    return SecretAccessEventOut(
        id=int(event.id),
        secret_id=int(event.secret_id) if event.secret_id is not None else None,
        secret_name=event.secret_name,
        task_id=int(event.task_id) if event.task_id is not None else None,
        tool_name=event.tool_name,
        success=bool(event.success),
        error_class=event.error_class,
        created_at=event.created_at,
    )


@router.get("/secrets", response_model=list[SecretOut])
async def list_secrets(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Secret).where(Secret.user_id == current_user.id).order_by(Secret.name.asc(), Secret.id.desc())
    )
    return [_serialize_secret(secret) for secret in result.scalars().all()]


@router.post("/secrets", response_model=SecretOut, status_code=status.HTTP_201_CREATED)
async def create_secret(
    body: SecretCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = normalize_secret_name(body.name)
    value = body.value.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name is required")
    if not value:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="value is required")

    secret = Secret(
        user_id=current_user.id,
        name=name,
        provider=(body.provider or "").strip(),
        ciphertext=encrypt_secret_value(value),
        is_active=True,
    )
    db.add(secret)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Secret name already exists") from exc
    await db.refresh(secret)
    return _serialize_secret(secret)


@router.post("/secrets/{secret_id}/disable", response_model=SecretDisableOut)
async def disable_secret(
    secret_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    secret = await _get_owned_secret(db, user_id=current_user.id, secret_id=secret_id)
    secret.is_active = False
    await db.flush()
    return SecretDisableOut(id=int(secret.id), is_active=bool(secret.is_active))


@router.get("/secrets/{secret_id}", response_model=SecretOut)
async def get_secret(
    secret_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    secret = await _get_owned_secret(db, user_id=current_user.id, secret_id=secret_id)
    return _serialize_secret(secret)


@router.get("/secrets/{secret_id}/access-events", response_model=list[SecretAccessEventOut])
async def get_secret_access_events(
    secret_id: int,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    secret = await _get_owned_secret(db, user_id=current_user.id, secret_id=secret_id)
    events = await list_secret_access_events(
        db,
        user_id=current_user.id,
        secret_id=int(secret.id),
        limit=limit,
    )
    return [_serialize_secret_access_event(event) for event in events]


@router.patch("/secrets/{secret_id}", response_model=SecretOut)
async def update_secret(
    secret_id: int,
    body: SecretUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    secret = await _get_owned_secret(db, user_id=current_user.id, secret_id=secret_id)
    if body.name is not None:
        name = normalize_secret_name(body.name)
        if not name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name is required")
        secret.name = name
    if body.provider is not None:
        secret.provider = body.provider.strip()
    if body.is_active is not None:
        secret.is_active = bool(body.is_active)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Secret name already exists") from exc
    await db.refresh(secret)
    return _serialize_secret(secret)


@router.post("/secrets/{secret_id}/rotate", response_model=SecretOut)
async def rotate_secret(
    secret_id: int,
    body: SecretRotate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    value = body.value.strip()
    if not value:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="value is required")
    secret = await _get_owned_secret(db, user_id=current_user.id, secret_id=secret_id)
    secret.ciphertext = encrypt_secret_value(value)
    secret.is_active = True
    await db.flush()
    await db.refresh(secret)
    return _serialize_secret(secret)
