"""Authenticated user settings API."""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import User, UserAssistantPreferences
from app.db.session import get_db
from app.settings_service import EffectiveUserSettings, get_user_settings_resolver
from app.time_utils import is_valid_timezone_name


router = APIRouter()
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class SettingValueOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: bool | str | None
    source: str


class UserSettingsOut(BaseModel):
    public_id: str
    version: int
    default_chat_model: SettingValueOut
    default_vision_model: SettingValueOut
    chat_routing_preference: SettingValueOut
    timezone: SettingValueOut
    active_hours_start: SettingValueOut
    active_hours_end: SettingValueOut
    notifications_enabled: SettingValueOut
    delivery_enabled: SettingValueOut
    appearance: SettingValueOut
    reduce_motion: SettingValueOut


class UpdateUserSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = None
    chat_routing_preference: Literal["auto", "fast", "deep"] | None = None
    timezone: str | None = None
    active_hours_start: str | None = None
    active_hours_end: str | None = None
    notifications_enabled: bool | None = None
    delivery_enabled: bool | None = None
    appearance: Literal["system", "light", "dark"] | None = None
    reduce_motion: bool | None = None


def _to_response(resolved: EffectiveUserSettings) -> UserSettingsOut:
    return UserSettingsOut(
        public_id=resolved.public_id,
        version=resolved.version,
        **{
            key: SettingValueOut(value=item.value, source=item.source)
            for key, item in resolved.values.items()
        },
    )


@router.get("/me", response_model=UserSettingsOut)
async def get_my_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserSettingsOut:
    resolved = await get_user_settings_resolver().resolve(db, current_user)
    return _to_response(resolved)


@router.patch("/me", response_model=UserSettingsOut)
async def update_my_settings(
    body: UpdateUserSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserSettingsOut:
    fields = body.model_fields_set - {"expected_version"}
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No settings supplied")
    _validate_schedule_fields(body, fields)

    row = (
        await db.execute(
            select(UserAssistantPreferences)
            .where(UserAssistantPreferences.user_id == current_user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    current_version = int(row.version or 1) if row else 0
    if body.expected_version is not None and body.expected_version != current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Settings changed since they were loaded (current version: {current_version})",
        )

    if row is None:
        row = UserAssistantPreferences(user_id=current_user.id)
        db.add(row)

    if "chat_routing_preference" in fields:
        current_user.chat_routing_preference = body.chat_routing_preference or "auto"
    if "timezone" in fields:
        current_user.active_hours_tz = body.timezone
    if "active_hours_start" in fields:
        current_user.active_hours_start = body.active_hours_start
    if "active_hours_end" in fields:
        current_user.active_hours_end = body.active_hours_end
    for field in ("notifications_enabled", "delivery_enabled", "appearance", "reduce_motion"):
        if field in fields:
            setattr(row, field, getattr(body, field))

    row.version = current_version + 1
    await db.flush()
    # Publish cache-visible state only after the database transaction succeeds.
    await db.commit()
    get_user_settings_resolver().invalidate(current_user)
    return _to_response(await get_user_settings_resolver().resolve(db, current_user))


def _validate_schedule_fields(body: UpdateUserSettingsRequest, fields: set[str]) -> None:
    for field in (
        "chat_routing_preference",
        "notifications_enabled",
        "delivery_enabled",
        "appearance",
        "reduce_motion",
    ):
        if field in fields and getattr(body, field) is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")
    if "timezone" in fields and body.timezone is not None and not is_valid_timezone_name(body.timezone):
        raise HTTPException(status_code=422, detail="timezone must be a valid IANA timezone name")
    for field in ("active_hours_start", "active_hours_end"):
        value = getattr(body, field)
        if field in fields and value is not None and not _TIME_PATTERN.fullmatch(value):
            raise HTTPException(status_code=422, detail=f"{field} must use 24-hour HH:MM format")
