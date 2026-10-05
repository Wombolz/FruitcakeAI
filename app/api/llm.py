from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.session import get_db
from app.model_profiles import get_model_profile_service, model_profile_to_dict
from app.model_access import allowed_model_profiles

router = APIRouter()


@router.get("/models")
async def list_llm_models(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    profiles = await allowed_model_profiles(db, current_user.id)
    return {"models": [model_profile_to_dict(profile) for profile in profiles]}
