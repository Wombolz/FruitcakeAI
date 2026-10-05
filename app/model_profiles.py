"""Persistence and hot runtime snapshots for configured model capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import ModelProfile
from app.llm_registry import available_llm_models


TOOL_MODES = {"enabled", "text_only", "restricted"}
QWEN_38_REASONING_EFFORTS = ("low", "medium", "xhigh")


@dataclass(frozen=True)
class ModelProfileSnapshot:
    database_id: int
    public_id: str
    model_id: str
    display_name: str
    provider_family: str
    enabled: bool
    is_local: bool
    supports_text: bool
    supports_vision: bool
    supports_tools: bool
    supports_thinking: bool
    supports_native_streaming: bool
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str
    tool_mode: str
    allowed_tools: tuple[str, ...]
    blocked_tools: tuple[str, ...]
    keep_alive: str


class ModelProfileService:
    def __init__(self) -> None:
        self._loaded = False
        self._by_model: dict[str, ModelProfileSnapshot] = {}
        self._by_public_id: dict[str, ModelProfileSnapshot] = {}
        self._by_database_id: dict[int, ModelProfileSnapshot] = {}

    async def ensure_seeded(self, db: AsyncSession) -> None:
        existing = (await db.execute(select(ModelProfile))).scalars().all()
        existing_models = {row.model_id for row in existing}
        created = False
        for entry in available_llm_models():
            model_id = str(entry.get("id") or "").strip()
            if not model_id or model_id in existing_models:
                continue
            row = self._seed_profile(model_id, str(entry.get("provider") or "other"))
            db.add(row)
            created = True
        if created:
            await db.flush()
        await self.refresh(db)

    @property
    def profile_count(self) -> int:
        return len(self._by_model)

    async def ensure_loaded(self, db: AsyncSession) -> None:
        if not self._loaded:
            await self.ensure_seeded(db)

    async def refresh(self, db: AsyncSession) -> None:
        rows = (await db.execute(select(ModelProfile).order_by(ModelProfile.display_name, ModelProfile.id))).scalars().all()
        snapshots = [self._snapshot(row) for row in rows]
        self._by_model = {item.model_id: item for item in snapshots}
        self._by_public_id = {item.public_id: item for item in snapshots}
        self._by_database_id = {item.database_id: item for item in snapshots}
        self._loaded = True

    async def list_profiles(self, db: AsyncSession, *, enabled_only: bool = False) -> list[ModelProfileSnapshot]:
        await self.ensure_seeded(db)
        values = list(self._by_model.values())
        if enabled_only:
            values = [item for item in values if item.enabled]
        return sorted(values, key=lambda item: (item.display_name.lower(), item.model_id))

    def for_model(self, model_id: str | None) -> ModelProfileSnapshot | None:
        return self._by_model.get(str(model_id or "").strip())

    def for_public_id(self, public_id: str | None) -> ModelProfileSnapshot | None:
        return self._by_public_id.get(str(public_id or "").strip())

    def for_database_id(self, database_id: int | None) -> ModelProfileSnapshot | None:
        return self._by_database_id.get(int(database_id)) if database_id is not None else None

    def clear(self) -> None:
        self._by_model.clear()
        self._by_public_id.clear()
        self._by_database_id.clear()
        self._loaded = False

    @staticmethod
    def _seed_profile(model_id: str, provider_hint: str) -> ModelProfile:
        from app.agent.model_provider import is_local_ollama_model, is_native_openai_model

        is_local = is_local_ollama_model(model_id)
        provider_family = "ollama" if is_local else "openai" if is_native_openai_model(model_id) else provider_hint
        native_models = {part.strip() for part in settings.fruitcake_native_agent_streaming_models.split(",") if part.strip()}
        text_only_models = {part.strip() for part in settings.local_tool_text_only_models.split(",") if part.strip()}
        qwen_38 = "qwen3.8" in model_id.lower()
        reasoning_efforts = list(QWEN_38_REASONING_EFFORTS) if qwen_38 else []
        native_streaming = bool(settings.fruitcake_native_agent_streaming_enabled and model_id in native_models)
        default_reasoning = (
            "low" if qwen_38 else str(settings.fruitcake_native_agent_streaming_reasoning_effort or "").strip()
        ) if (qwen_38 or native_streaming) else None
        row = ModelProfile(
            model_id=model_id,
            display_name=model_id,
            provider_family=provider_family or "other",
            enabled=True,
            is_local=is_local,
            supports_text=True,
            supports_vision=model_id == str(settings.image_vision_model or "").strip(),
            supports_tools=model_id not in text_only_models,
            supports_thinking=bool(reasoning_efforts or native_streaming),
            supports_native_streaming=native_streaming,
            default_reasoning_effort=default_reasoning,
            tool_mode="text_only" if model_id in text_only_models else "enabled",
            keep_alive=str(settings.local_model_keep_alive or "").strip() if is_local else None,
        )
        row.reasoning_efforts = reasoning_efforts or ([default_reasoning] if default_reasoning else [])
        row.allowed_tools = []
        row.blocked_tools = []
        return row

    @staticmethod
    def _snapshot(row: ModelProfile) -> ModelProfileSnapshot:
        return ModelProfileSnapshot(
            database_id=row.id,
            public_id=row.public_id,
            model_id=row.model_id,
            display_name=row.display_name,
            provider_family=row.provider_family,
            enabled=bool(row.enabled),
            is_local=bool(row.is_local),
            supports_text=bool(row.supports_text),
            supports_vision=bool(row.supports_vision),
            supports_tools=bool(row.supports_tools),
            supports_thinking=bool(row.supports_thinking),
            supports_native_streaming=bool(row.supports_native_streaming),
            reasoning_efforts=tuple(row.reasoning_efforts),
            default_reasoning_effort=str(row.default_reasoning_effort or ""),
            tool_mode=str(row.tool_mode or "enabled"),
            allowed_tools=tuple(row.allowed_tools),
            blocked_tools=tuple(row.blocked_tools),
            keep_alive=str(row.keep_alive or ""),
        )


def model_profile_to_dict(profile: ModelProfileSnapshot) -> dict[str, Any]:
    return {
        "id": profile.model_id,
        "profile_id": profile.public_id,
        "model_id": profile.model_id,
        "provider": "local" if profile.is_local else profile.provider_family,
        "label": profile.display_name,
        "is_default_chat": profile.model_id == settings.llm_model,
        "is_default_task_small": profile.model_id == settings.task_small_model,
        "is_default_task_large": profile.model_id == settings.task_large_model,
        "display_name": profile.display_name,
        "provider_family": profile.provider_family,
        "enabled": profile.enabled,
        "is_local": profile.is_local,
        "capabilities": {
            "text": profile.supports_text,
            "vision": profile.supports_vision,
            "tools": profile.supports_tools,
            "thinking": profile.supports_thinking,
            "native_streaming": profile.supports_native_streaming,
        },
        "reasoning_efforts": list(profile.reasoning_efforts),
        "default_reasoning_effort": profile.default_reasoning_effort or None,
        "tool_mode": profile.tool_mode,
        "allowed_tools": list(profile.allowed_tools),
        "blocked_tools": list(profile.blocked_tools),
        "keep_alive": profile.keep_alive or None,
    }


_service = ModelProfileService()


def get_model_profile_service() -> ModelProfileService:
    return _service


def reasoning_effort_for_model(model_id: str | None, requested: str | None) -> str | None:
    profile = _service.for_model(model_id)
    if profile is None:
        return str(requested or "").strip() or None
    candidate = str(requested or "").strip()
    if candidate and candidate in profile.reasoning_efforts:
        return candidate
    return profile.default_reasoning_effort or None
