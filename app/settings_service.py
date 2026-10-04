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
        preferences = await self._load_preferences(db, user)
        values: dict[str, ResolvedSetting] = {
            "default_chat_model": ResolvedSetting(settings.llm_model, "deployment"),
            "default_vision_model": ResolvedSetting(settings.image_vision_model or None, "deployment"),
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
