"""Per-user model access policy with backward-compatible deployment defaults."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserModelAccess
from app.model_profiles import ModelProfileSnapshot, get_model_profile_service


async def model_access_map(db: AsyncSession, user_id: int) -> dict[int, bool]:
    rows = (
        await db.execute(select(UserModelAccess).where(UserModelAccess.user_id == user_id))
    ).scalars().all()
    return {int(row.model_profile_id): bool(row.allowed) for row in rows}


async def allowed_model_profiles(db: AsyncSession, user_id: int) -> list[ModelProfileSnapshot]:
    profiles = await get_model_profile_service().list_profiles(db, enabled_only=True)
    policy = await model_access_map(db, user_id)
    if not policy:
        return profiles
    return [profile for profile in profiles if policy.get(profile.database_id, False)]


async def model_is_allowed(db: AsyncSession, user_id: int, profile: ModelProfileSnapshot | None) -> bool:
    if profile is None or not profile.enabled:
        return False
    policy = await model_access_map(db, user_id)
    return not policy or policy.get(profile.database_id, False)


async def model_id_is_allowed(db: AsyncSession, user_id: int, model_id: str | None) -> bool:
    service = get_model_profile_service()
    await service.ensure_loaded(db)
    return await model_is_allowed(db, user_id, service.for_model(model_id))
