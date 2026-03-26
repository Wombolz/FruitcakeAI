from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.db.models import User
from app.llm_registry import available_llm_models

router = APIRouter()


@router.get("/models")
async def list_llm_models(
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    del current_user
    return {"models": available_llm_models()}
