"""
User-scoped workspace artifact access.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.auth.dependencies import get_current_user
from app.db.models import User
from app.mcp.servers.filesystem import resolve_workspace_path_for_user

router = APIRouter()

_ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@router.get("/images")
async def get_workspace_image(
    path: str = Query(..., min_length=1),
    current_user: User = Depends(get_current_user),
):
    """Serve a current user's workspace image through normal API auth."""
    try:
        _, resolved = resolve_workspace_path_for_user(int(current_user.id), path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    suffix = resolved.suffix.lower()
    if suffix not in _ALLOWED_IMAGE_SUFFIXES:
        raise HTTPException(status_code=400, detail="Path is not a supported image file")

    media_type = mimetypes.guess_type(str(resolved))[0] or _fallback_image_media_type(suffix)
    if not media_type or not media_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Path is not a supported image file")

    return FileResponse(
        Path(resolved),
        media_type=media_type,
        filename=resolved.name,
    )


def _fallback_image_media_type(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix.lower(), "application/octet-stream")
