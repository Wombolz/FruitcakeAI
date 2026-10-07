"""
User-scoped workspace artifact access.
"""

from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import User
from app.mcp.servers.filesystem import resolve_workspace_path_for_user

router = APIRouter()

_ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_UPLOAD_CHUNK_BYTES = 1024 * 1024


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


@router.get("/files")
async def get_workspace_file(
    path: str = Query(..., min_length=1),
    current_user: User = Depends(get_current_user),
):
    """Download a file from the current user's workspace through normal API auth."""
    try:
        _, resolved = resolve_workspace_path_for_user(int(current_user.id), path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Workspace file not found")

    media_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
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


@router.post("/uploads")
async def upload_workspace_file(
    file: UploadFile = File(...),
    target_dir: str = Form("uploads"),
    current_user: User = Depends(get_current_user),
):
    """Upload a user-selected file into the current user's workspace."""
    safe_dir = str(target_dir or "uploads").strip() or "uploads"
    try:
        workspace_root, upload_dir = resolve_workspace_path_for_user(int(current_user.id), safe_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    original_name = Path(file.filename or "attachment").name
    safe_name = _safe_upload_filename(original_name)
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"
    target_path = upload_dir / stored_name
    max_bytes = max(1, int(settings.upload_max_size_mb)) * 1024 * 1024

    size = 0
    upload_dir.mkdir(parents=True, exist_ok=True)
    exceeded_limit = False
    try:
        with target_path.open("wb") as handle:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    exceeded_limit = True
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds {settings.upload_max_size_mb} MB limit",
                    )
                handle.write(chunk)
    finally:
        await file.close()
        if exceeded_limit:
            target_path.unlink(missing_ok=True)

    relative_path = target_path.relative_to(workspace_root)
    media_type = file.content_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    return {
        "path": str(relative_path),
        "filename": original_name,
        "stored_filename": stored_name,
        "media_type": media_type,
        "size_bytes": size,
        "is_image": target_path.suffix.lower() in _ALLOWED_IMAGE_SUFFIXES,
    }


def _safe_upload_filename(filename: str) -> str:
    name = Path(filename or "attachment").name.strip() or "attachment"
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return cleaned[:160] or "attachment"
