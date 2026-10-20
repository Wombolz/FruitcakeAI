"""
User-scoped workspace artifact access.
"""

from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import User
from app.db.session import get_db
from app.coding_runtime.repository import RepositoryError
from app.user_workspace import UserWorkspaceBrowser
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


# The current user selects the root. No repository grant or client user/path root.
_workspace_browser = UserWorkspaceBrowser()


async def _workspace_operation(operation, user, db):
    try:
        result = await operation
        await db.refresh(user)
        if not user.is_active:
            raise HTTPException(status_code=401, detail="User no longer active")
        return result
    except RepositoryError as exc:
        status = {"file_denied": 403, "file_missing": 404, "file_changed": 409,
                  "workspace_changed": 409, "invalid_request": 400, "repository_limit": 413,
                  "repository_timeout": 504}.get(exc.code, 403)
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from None
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Workspace inspection timed out") from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Workspace file no longer exists") from None
    except (OSError, UnicodeError):
        raise HTTPException(status_code=403, detail="Workspace path cannot be read safely") from None


def _private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/explorer")
async def workspace_explorer(response: Response, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _private_response(response)
    _, reference = await _workspace_operation(_workspace_browser.describe(int(current_user.id)), current_user, db)
    return reference


@router.get("/explorer/directory")
async def workspace_directory(response: Response, root_id: str = Query(..., min_length=64, max_length=64),
    path: str = Query("", max_length=1024), offset: int = Query(0, ge=0, le=4096),
    version: str | None = Query(None, min_length=64, max_length=64),
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _private_response(response)
    return await _workspace_operation(_workspace_browser.browse(int(current_user.id), root_id,
        path=path, offset=offset, version=version), current_user, db)


@router.get("/explorer/file")
async def workspace_source(response: Response, root_id: str = Query(..., min_length=64, max_length=64),
    path: str = Query(..., min_length=1, max_length=1024),
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _private_response(response)
    return await _workspace_operation(_workspace_browser.browse(int(current_user.id), root_id, path=path, file=True), current_user, db)


@router.get("/explorer/download")
async def workspace_download(root_id: str = Query(..., min_length=64, max_length=64),
    path: str = Query(..., min_length=1, max_length=1024),
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from urllib.parse import quote
    data = await _workspace_operation(_workspace_browser.download(int(current_user.id), root_id, path), current_user, db)
    # Always a download, never an executable same-origin HTML response.
    return _private_response(Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(Path(path).name, safe=""),
        "Content-Security-Policy": "sandbox; default-src 'none'"}))
