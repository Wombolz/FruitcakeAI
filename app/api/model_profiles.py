"""Administrator model-profile policy API."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db.models import ModelProfile, User
from app.db.session import get_db
from app.model_profiles import TOOL_MODES, get_model_profile_service, model_profile_to_dict


router = APIRouter()


class ModelProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    provider_family: str = Field(min_length=1, max_length=50)
    enabled: bool = True
    is_local: bool = False
    supports_text: bool = True
    supports_vision: bool = False
    supports_tools: bool = True
    supports_thinking: bool = False
    supports_native_streaming: bool = False
    reasoning_efforts: list[str] = Field(default_factory=list)
    default_reasoning_effort: str | None = None
    tool_mode: Literal["enabled", "text_only", "restricted"] = "enabled"
    allowed_tools: list[str] = Field(default_factory=list)
    blocked_tools: list[str] = Field(default_factory=list)
    keep_alive: str | None = None


class ModelProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    supports_text: bool | None = None
    supports_vision: bool | None = None
    supports_tools: bool | None = None
    supports_thinking: bool | None = None
    supports_native_streaming: bool | None = None
    reasoning_efforts: list[str] | None = None
    default_reasoning_effort: str | None = None
    tool_mode: Literal["enabled", "text_only", "restricted"] | None = None
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] | None = None
    keep_alive: str | None = None


@router.get("/model-profiles")
async def list_model_profiles(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    del current_user
    profiles = await get_model_profile_service().list_profiles(db)
    return {"profiles": [model_profile_to_dict(profile) for profile in profiles]}


@router.post("/model-profiles", status_code=status.HTTP_201_CREATED)
async def create_model_profile(
    body: ModelProfileCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    service = get_model_profile_service()
    await service.ensure_seeded(db)
    duplicate = (
        await db.execute(select(ModelProfile).where(ModelProfile.model_id == body.model_id.strip()))
    ).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status_code=409, detail="A model profile already exists for this model")
    _validate_profile_policy(
        body.model_id,
        body.reasoning_efforts,
        body.default_reasoning_effort,
        body.tool_mode,
        body.supports_tools,
        body.supports_thinking,
    )
    row = ModelProfile(
        model_id=body.model_id.strip(),
        display_name=body.display_name.strip(),
        provider_family=body.provider_family.strip().lower(),
        enabled=body.enabled,
        is_local=body.is_local,
        supports_text=body.supports_text,
        supports_vision=body.supports_vision,
        supports_tools=body.supports_tools,
        supports_thinking=body.supports_thinking,
        supports_native_streaming=body.supports_native_streaming,
        default_reasoning_effort=body.default_reasoning_effort,
        tool_mode=body.tool_mode,
        keep_alive=(body.keep_alive or "").strip() or None,
        updated_by_user_id=current_user.id,
    )
    row.reasoning_efforts = _normalized_values(body.reasoning_efforts)
    row.allowed_tools = _normalized_values(body.allowed_tools)
    row.blocked_tools = _normalized_values(body.blocked_tools)
    db.add(row)
    await db.flush()
    await db.commit()
    await service.refresh(db)
    return model_profile_to_dict(service.for_model(row.model_id))


@router.patch("/model-profiles/{public_id}")
async def update_model_profile(
    public_id: str,
    body: ModelProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    row = (
        await db.execute(select(ModelProfile).where(ModelProfile.public_id == public_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Model profile not found")
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=400, detail="No profile changes supplied")
    for field in (
        "display_name", "enabled", "supports_text", "supports_vision", "supports_tools",
        "supports_thinking", "supports_native_streaming", "tool_mode",
    ):
        if field in fields and getattr(body, field) is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")

    reasoning_efforts = _normalized_values(body.reasoning_efforts) if "reasoning_efforts" in fields else row.reasoning_efforts
    default_reasoning = body.default_reasoning_effort if "default_reasoning_effort" in fields else row.default_reasoning_effort
    tool_mode = body.tool_mode if "tool_mode" in fields else row.tool_mode
    supports_tools = body.supports_tools if "supports_tools" in fields else row.supports_tools
    supports_thinking = body.supports_thinking if "supports_thinking" in fields else row.supports_thinking
    _validate_profile_policy(
        row.model_id,
        reasoning_efforts,
        default_reasoning,
        tool_mode,
        bool(supports_tools),
        bool(supports_thinking),
    )

    for field in (
        "display_name", "enabled", "supports_text", "supports_vision", "supports_tools",
        "supports_thinking", "supports_native_streaming", "default_reasoning_effort", "tool_mode",
    ):
        if field in fields:
            setattr(row, field, getattr(body, field))
    if "reasoning_efforts" in fields:
        row.reasoning_efforts = reasoning_efforts
    if "allowed_tools" in fields:
        row.allowed_tools = _normalized_values(body.allowed_tools)
    if "blocked_tools" in fields:
        row.blocked_tools = _normalized_values(body.blocked_tools)
    if "keep_alive" in fields:
        row.keep_alive = (body.keep_alive or "").strip() or None
    row.updated_by_user_id = current_user.id
    await db.flush()
    await db.commit()
    service = get_model_profile_service()
    await service.refresh(db)
    return model_profile_to_dict(service.for_model(row.model_id))


def _normalized_values(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in (values or []) if str(value).strip()))


def _validate_profile_policy(
    model_id: str,
    reasoning_efforts: list[str],
    default_reasoning_effort: str | None,
    tool_mode: str,
    supports_tools: bool,
    supports_thinking: bool,
) -> None:
    efforts = _normalized_values(reasoning_efforts)
    if "qwen3.8" in model_id.lower() and any(value not in {"low", "medium", "xhigh"} for value in efforts):
        raise HTTPException(status_code=422, detail="Qwen 3.8 reasoning_efforts support only low, medium, and xhigh")
    if default_reasoning_effort and default_reasoning_effort not in efforts:
        raise HTTPException(status_code=422, detail="default_reasoning_effort must be one of reasoning_efforts")
    if tool_mode not in TOOL_MODES:
        raise HTTPException(status_code=422, detail="Invalid tool_mode")
    if tool_mode != "text_only" and not supports_tools:
        raise HTTPException(status_code=422, detail="tool_mode must be text_only when tools are unsupported")
    if (efforts or default_reasoning_effort) and not supports_thinking:
        raise HTTPException(status_code=422, detail="reasoning settings require supports_thinking")
