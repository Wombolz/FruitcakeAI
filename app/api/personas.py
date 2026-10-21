"""Authenticated authoring of personality only; bundled policies stay read-only."""
import json

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.models import PersonaDefinition, PersonaRevision, PersonaTestOptions, User
from app.db.session import get_db
from app.personas import service

router = APIRouter()


class RevisionRequest(service.PersonaDraft):
    expected_version: int = Field(ge=1)


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)


class DeleteRequest(VersionRequest):
    confirmation_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class DefaultRequest(service.PersonaReference):
    expected_version: int = Field(ge=0)


class DuplicateRequest(service.PersonaReference):
    display_name: str = Field(min_length=1, max_length=100)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    draft: service.PersonaDraft | None = None
    reference: service.PersonaReference | None = None

    @model_validator(mode="after")
    def one_source(self):
        if (self.draft is None) == (self.reference is None):
            raise ValueError("Choose exactly one draft or saved reference.")
        return self


class TestConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reference: service.PersonaReference
    llm_model: str = Field(min_length=1, max_length=200)
    memory_recall_enabled: bool = False


def definition_response(row):
    return {"id": row.id, "persona": "custom:" + row.id, "version": row.head_version,
            "archived": row.archived_at is not None}


@router.get("")
async def catalog(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    definitions = (await db.execute(select(PersonaDefinition, PersonaRevision).join(PersonaRevision, and_(
        PersonaRevision.persona_id == PersonaDefinition.id,
        PersonaRevision.version == PersonaDefinition.head_version)).where(
        PersonaDefinition.user_id == user.id).order_by(PersonaDefinition.created_at, PersonaDefinition.id)
        .offset(offset).limit(limit))).all()
    items = []
    for row, revision in definitions:
        items.append({**definition_response(row), "personality": json.loads(revision.payload_json)})
    return {"bundled": [service.builtin_snapshot(key) for key in service.list_personas()],
            "custom": items, "offset": offset, "limit": limit, "default": await service.get_default(db, user)}


@router.get("/default")
async def default(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.get_default(db, user)


@router.put("/default")
async def set_default(body: DefaultRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.set_default(db, user, body, body.expected_version)


@router.post("", status_code=201)
async def create(body: service.PersonaDraft, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return definition_response(await service.create_definition(db, user, body))


@router.post("/duplicate", status_code=201)
async def duplicate(body: DuplicateRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    value = await service.resolve_reference(db, user, body)
    try:
        fields = {key: val for key, val in value["personality"].items() if key in service.PersonaDraft.model_fields}
        draft = service.PersonaDraft.model_validate({**fields, "display_name": body.display_name})
    except ValidationError:
        raise HTTPException(422, "The duplicated personality exceeds authoring limits; use a new draft.") from None
    return definition_response(await service.create_definition(db, user, draft))


@router.post("/preview")
async def preview(body: PreviewRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.agent.tools import get_tools_for_user
    if body.draft is not None:
        payload = await service.resolved_draft(db, user, body.draft)
        value = service.snapshot("custom:preview", None, payload)
    else:
        value = await service.resolve_reference(db, user, body.reference)
    ctx = service.context_from_snapshot(user, value)
    layers = []
    remaining = 64000
    for layer in ctx.stable_prompt_layers():
        content = layer.content if layer.source != "session_context" else "[Current account and selected persona; no conversation history.]"
        if len(content) > remaining:
            raise HTTPException(413, "Prompt preview exceeds the supported limit.")
        remaining -= len(content)
        layers.append({"source": layer.source, "content": content})
    return {"layers": layers, "tools": sorted({item["function"]["name"] for item in get_tools_for_user(ctx)}),
            "blocked_tools": ctx.blocked_tools, "content_filter": ctx.content_filter,
            "exclusions": ["Conversation history", "Recalled memories", "Time and dynamically selected skills", "Image bytes"],
            "visual_generation_available": False}


@router.post("/assets", status_code=201)
async def upload_asset(file: UploadFile = File(...), user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    from app.personas.assets import MAX_IMAGE_BYTES, store_image
    try:
        data = await file.read(MAX_IMAGE_BYTES + 1)
        return await store_image(db, user, data)
    finally:
        await file.close()


@router.get("/assets/{asset_id}")
async def image_asset(asset_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.personas.assets import owned_asset
    row = await owned_asset(db, user, asset_id)
    return Response(row.data, media_type="image/png", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/test-conversations", status_code=201)
async def test_conversation(body: TestConversationRequest, user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    from app.api.chat import CreateSessionRequest, create_session
    from app.model_access import model_id_is_allowed
    if not await model_id_is_allowed(db, user.id, body.llm_model):
        raise HTTPException(403, "Select a model available to your account.")
    value = await service.resolve_reference(db, user, body.reference)
    session = await create_session(CreateSessionRequest(title="Persona test: " + value["personality"]["display_name"]), db, user)
    await service.select_session(db, user, session.id, body.reference, expected_version=1)
    session.llm_model = body.llm_model
    db.add(PersonaTestOptions(session_id=session.id, memory_recall_enabled=body.memory_recall_enabled))
    await db.flush()
    service.audit(db, user, "test_created", session_id=session.id, persona=value["persona"], revision=value["revision"])
    return {"session_id": session.id, "selection": value, "llm_model": session.llm_model,
            "memory_recall_enabled": body.memory_recall_enabled}


@router.get("/{persona_id}/revisions")
async def revisions(persona_id: str, before: int | None = Query(default=None, ge=1),
                    limit: int = Query(default=25, ge=1, le=100),
                    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await service.owned_definition(db, user, persona_id)
    query = select(PersonaRevision).where(PersonaRevision.persona_id == row.id)
    if before is not None:
        query = query.where(PersonaRevision.version < before)
    records = (await db.execute(query.order_by(PersonaRevision.version.desc()).limit(limit))).scalars().all()
    return {**definition_response(row), "revisions": [
        {"version": item.version, "personality": json.loads(item.payload_json)} for item in records]}


@router.get("/{persona_id}/deletion-preview")
async def deletion_preview(persona_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.deletion_preview(db, user, persona_id)


@router.delete("/{persona_id}")
async def delete_persona(persona_id: str, body: DeleteRequest,
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.delete_definition(db, user, persona_id, body.expected_version, body.confirmation_token)


@router.put("/{persona_id}")
async def revise(persona_id: str, body: RevisionRequest, user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)):
    draft = service.PersonaDraft.model_validate(body.model_dump(exclude={"expected_version"}))
    return definition_response(await service.revise_definition(db, user, persona_id, body.expected_version, draft))


@router.post("/{persona_id}/archive", status_code=204)
async def archive(persona_id: str, body: VersionRequest, user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    await service.archive_definition(db, user, persona_id, body.expected_version)
