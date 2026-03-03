"""
FruitcakeAI v5 — Device token API (Phase 4)

POST   /devices/register   Upsert APNs device token for current user
DELETE /devices/{token}    Remove token on logout or device change
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import DeviceToken, User
from app.db.session import get_db

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterTokenRequest(BaseModel):
    token: str
    environment: str = "sandbox"    # "sandbox" | "production"


class DeviceTokenOut(BaseModel):
    id: int
    token: str
    environment: str

    class Config:
        from_attributes = True


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post(
    "/devices/register",
    response_model=DeviceTokenOut,
    status_code=status.HTTP_200_OK,
)
async def register_device(
    body: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upsert an APNs device token. Safe to call on every app launch."""
    if body.environment not in ("sandbox", "production"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="environment must be 'sandbox' or 'production'",
        )

    result = await db.execute(
        select(DeviceToken).where(DeviceToken.token == body.token)
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Update owner and environment in case the token was re-used
        existing.user_id = current_user.id
        existing.environment = body.environment
        return existing

    token = DeviceToken(
        user_id=current_user.id,
        token=body.token,
        environment=body.environment,
    )
    db.add(token)
    await db.flush()
    return token


@router.delete("/devices/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_device(
    token: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a device token (call on logout or when APNs reports it as invalid)."""
    result = await db.execute(
        select(DeviceToken).where(
            DeviceToken.token == token,
            DeviceToken.user_id == current_user.id,
        )
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    await db.delete(device)
