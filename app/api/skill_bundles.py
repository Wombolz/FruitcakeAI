"""Native folder imports upload frozen text, never server filesystem paths."""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from starlette.responses import JSONResponse

from app.auth.dependencies import require_admin
from app.db.models import Skill, User
from app.db.session import get_db
from app.skills.bundle_service import preview_bundle
from app.skills.service import SkillConflictError, SkillValidationError, get_skill_service

router = APIRouter()


class BundleBodyLimit:
    """Bound JSON imports before FastAPI decodes their text content."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        targets = {"/admin/skill-bundles/preview", "/admin/skill-bundles/install"}
        if scope["type"] != "http" or scope.get("path") not in targets:
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            event = await receive()
            if event["type"] != "http.request":
                return
            chunk = event.get("body", b"")
            size += len(chunk)
            if size > 2_000_000:
                return await JSONResponse({"detail": "Skill bundle request exceeds 2 MB"}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not event.get("more_body"):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks)}
            return await receive()

        await self.app(scope, replay, send)


class BundleRequest(BaseModel):
    files: dict[str, str] = Field(max_length=32)
    scope: str = "shared"
    is_pinned: bool = False


class BundleInstallRequest(BundleRequest):
    preview_hash: str


async def _preview(body, user):
    try:
        return await preview_bundle(body.files, user_context=user, scope=body.scope, pinned=body.is_pinned)
    except SkillValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/preview")
async def preview(body: BundleRequest, user: User = Depends(require_admin)):
    item, missing, estimated, warnings = await _preview(body, user)
    return {
        "name": item.name, "slug": item.slug, "description": item.description,
        "required_tools": item.allowed_tool_additions, "missing_tools": missing,
        "estimated_tokens": estimated, "warnings": warnings,
        "preview_hash": item.preview_hash, "file_paths": list(item.bundle_files),
        "can_install": not missing and not any("exceed" in w for w in warnings),
    }


@router.post("/install", status_code=201)
async def install(body: BundleInstallRequest, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    item, missing, _, warnings = await _preview(body, user)
    if body.preview_hash != item.preview_hash:
        raise HTTPException(409, "Preview changed; review the bundle again")
    if missing or any("exceed" in w for w in warnings):
        raise HTTPException(400, "Resolve missing tools or prompt-budget warnings before installing")
    try:
        existing = (await db.execute(select(Skill).where(
            Skill.slug == item.slug, Skill.personal_user_id == item.personal_user_id,
            Skill.is_active.is_(True), Skill.content_hash == item.preview_hash,
        ))).scalar_one_or_none()
        if existing is not None:
            return {"id": existing.id, "name": existing.name, "slug": existing.slug}
        skill = await get_skill_service().install_preview(db, preview=item, installed_by=user.id)
        await db.commit()
        return {"id": skill.id, "name": skill.name, "slug": skill.slug}
    except SkillConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{skill_id}")
async def detail(skill_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(404, "Skill not found")
    return {"id": skill.id, "files": json.loads(skill.bundle_files_json) if skill.bundle_files_json else {"SKILL.md": skill.system_prompt_addition}}
