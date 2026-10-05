"""Typed, layered resolution for user-owned assistant settings."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import time
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import User, UserAssistantPreferences
from app.time_utils import resolve_effective_timezone


@dataclass(frozen=True)
class ResolvedSetting:
    value: Any
    source: str


@dataclass(frozen=True)
class EffectiveUserSettings:
    public_id: str
    version: int
    values: Mapping[str, ResolvedSetting]

    def value(self, key: str) -> Any:
        return self.values[key].value

    @property
    def provenance(self) -> dict[str, str]:
        return {key: item.source for key, item in self.values.items()}


@dataclass(frozen=True)
class _PreferenceSnapshot:
    preferred_model_profile_id: int | None
    preferred_vision_model_profile_id: int | None
    preferred_reasoning_effort: str
    notifications_enabled: bool
    delivery_enabled: bool
    appearance: str
    reduce_motion: bool
    version: int


class UserSettingsResolver:
    """Resolve deployment, user, and explicit override layers with provenance."""

    def __init__(self, *, max_entries: int = 256, ttl_seconds: float = 30.0) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[float, _PreferenceSnapshot | None]] = OrderedDict()

    async def resolve(
        self,
        db: AsyncSession,
        user: User,
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> EffectiveUserSettings:
        from app.model_access import allowed_model_profiles, model_access_map
        from app.model_profiles import get_model_profile_service

        profile_service = get_model_profile_service()
        await profile_service.ensure_loaded(db)
        allowed_profiles = await allowed_model_profiles(db, user.id)
        allowed_ids = {profile.database_id for profile in allowed_profiles}
        explicit_model_policy = bool(await model_access_map(db, user.id))
        preferences = await self._load_preferences(db, user)
        preferred_model = profile_service.for_database_id(
            preferences.preferred_model_profile_id if preferences else None
        )
        if preferred_model is not None and (
            not preferred_model.enabled or preferred_model.database_id not in allowed_ids
        ):
            preferred_model = None
        deployment_model = profile_service.for_model(settings.llm_model)
        if deployment_model is not None and (
            not deployment_model.enabled or deployment_model.database_id not in allowed_ids
        ):
            deployment_model = None
        fallback_model = next((profile for profile in allowed_profiles if profile.supports_text), None)
        effective_model = preferred_model or deployment_model or fallback_model
        preferred_vision = profile_service.for_database_id(
            preferences.preferred_vision_model_profile_id if preferences else None
        )
        if preferred_vision is not None and (
            not preferred_vision.enabled
            or not preferred_vision.supports_vision
            or preferred_vision.database_id not in allowed_ids
        ):
            preferred_vision = None
        fallback_vision = next((profile for profile in allowed_profiles if profile.supports_vision), None)
        effective_vision = preferred_vision or fallback_vision
        deployment_vision_value = None if explicit_model_policy else (settings.image_vision_model or None)
        requested_reasoning = preferences.preferred_reasoning_effort if preferences and preferred_model else ""
        user_reasoning = (
            requested_reasoning
            if effective_model is not None and requested_reasoning in effective_model.reasoning_efforts
            else ""
        )
        effective_reasoning = user_reasoning or (
            effective_model.default_reasoning_effort if effective_model is not None else ""
        )
        values: dict[str, ResolvedSetting] = {
            "default_chat_model": ResolvedSetting(
                effective_model.model_id if effective_model else None,
                "user" if preferred_model else ("deployment" if deployment_model else "admin"),
            ),
            "model_profile_id": ResolvedSetting(
                effective_model.public_id if effective_model else None,
                "user" if preferred_model else ("deployment" if deployment_model else "admin"),
            ),
            "default_vision_model": ResolvedSetting(
                effective_vision.model_id if effective_vision else deployment_vision_value,
                "user" if preferred_vision else ("admin" if explicit_model_policy else "deployment"),
            ),
            "vision_model_profile_id": ResolvedSetting(
                effective_vision.public_id if effective_vision else None,
                "user" if preferred_vision else ("admin" if explicit_model_policy else "deployment"),
            ),
            "reasoning_effort": ResolvedSetting(
                effective_reasoning or None,
                "user" if user_reasoning else ("admin" if effective_reasoning else "deployment"),
            ),
            "chat_routing_preference": ResolvedSetting(
                user.chat_routing_preference or "auto",
                "user",
            ),
            "timezone": ResolvedSetting(
                resolve_effective_timezone(None, user.active_hours_tz),
                "user" if user.active_hours_tz else "deployment",
            ),
            "active_hours_start": ResolvedSetting(user.active_hours_start, "user" if user.active_hours_start else "deployment"),
            "active_hours_end": ResolvedSetting(user.active_hours_end, "user" if user.active_hours_end else "deployment"),
            "notifications_enabled": ResolvedSetting(
                preferences.notifications_enabled if preferences else True,
                "user" if preferences else "deployment",
            ),
            "delivery_enabled": ResolvedSetting(
                preferences.delivery_enabled if preferences else True,
                "user" if preferences else "deployment",
            ),
            "appearance": ResolvedSetting(
                preferences.appearance if preferences else "system",
                "user" if preferences else "deployment",
            ),
            "reduce_motion": ResolvedSetting(
                preferences.reduce_motion if preferences else False,
                "user" if preferences else "deployment",
            ),
        }
        for key, value in (overrides or {}).items():
            if key not in values or value is None:
                continue
            values[key] = ResolvedSetting(value, "session_task_override")
        return EffectiveUserSettings(
            public_id=user.public_id,
            version=preferences.version if preferences else 0,
            values=values,
        )

    def invalidate(self, user: User | str) -> None:
        public_id = user if isinstance(user, str) else user.public_id
        self._cache.pop(public_id, None)

    async def _load_preferences(
        self,
        db: AsyncSession,
        user: User,
    ) -> _PreferenceSnapshot | None:
        now = time.monotonic()
        cached = self._cache.get(user.public_id)
        if cached and now - cached[0] < self._ttl_seconds:
            self._cache.move_to_end(user.public_id)
            return cached[1]

        row = (
            await db.execute(
                select(UserAssistantPreferences).where(UserAssistantPreferences.user_id == user.id)
            )
        ).scalar_one_or_none()
        snapshot = None if row is None else _PreferenceSnapshot(
            preferred_model_profile_id=row.preferred_model_profile_id,
            preferred_vision_model_profile_id=row.preferred_vision_model_profile_id,
            preferred_reasoning_effort=str(row.preferred_reasoning_effort or ""),
            notifications_enabled=bool(row.notifications_enabled),
            delivery_enabled=bool(row.delivery_enabled),
            appearance=str(row.appearance or "system"),
            reduce_motion=bool(row.reduce_motion),
            version=int(row.version or 1),
        )
        self._cache[user.public_id] = (now, snapshot)
        self._cache.move_to_end(user.public_id)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return snapshot


_resolver = UserSettingsResolver()


def get_user_settings_resolver() -> UserSettingsResolver:
    return _resolver
