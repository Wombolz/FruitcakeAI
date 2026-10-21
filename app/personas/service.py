"""Immutable personality revisions. Callers own the surrounding transaction."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, cast, delete, or_, select, type_coerce, update

from app.agent.context import UserContext, _DEFAULT_IDENTITY
from app.agent.persona_loader import get_persona, list_personas, persona_exists
from app.agent.persona_policy import PersonaPolicyError
from app.db.models import (AdminPolicyEvent, ChatRun, ChatRunPersonaSnapshot, ChatSession,
                           PersonaDefinition, PersonaRevision, PersonaSelection, User,
                           UserPersonaDefault, PersonaTestOptions, PersonaVisualAsset)


class VisualIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    portrait_asset_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    sheet_asset_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")


class PersonaDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    personality_mode: Literal["default", "neutral"] = "neutral"
    identity: str | None = Field(default=None, max_length=2000)
    tone: str | None = Field(default=None, max_length=300)
    behavior_instructions: list[str] = Field(default_factory=list, max_length=24)
    visual_identity: VisualIdentity = Field(default_factory=VisualIdentity)

    @model_validator(mode="after")
    def bounded_text(self):
        values = [self.display_name, self.description, self.identity or "", self.tone or "", *self.behavior_instructions]
        if not self.display_name.strip() or self.display_name != self.display_name.strip():
            raise ValueError("Display name must not be blank or padded.")
        if any(len(item) > 1000 for item in self.behavior_instructions):
            raise ValueError("Each instruction is limited to 1000 characters.")
        if any(any(ord(c) < 32 and c not in "\n\t" for c in item) for item in values):
            raise ValueError("Control characters are not allowed.")
        try:
            size = sum(len(item.encode("utf-8")) for item in values)
        except UnicodeEncodeError:
            raise ValueError("Text must be valid UTF-8.") from None
        if size > 16384:
            raise ValueError("Persona text is limited to 16 KiB.")
        return self


class PersonaReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    persona: str = Field(min_length=1, max_length=100)
    revision: int | None = Field(default=None, ge=1)


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def draft_payload(draft):
    # Inherit once when saving, not on every turn: revisions remain reproducible.
    payload = draft.model_dump()
    inherited = get_persona("family_assistant") if draft.personality_mode == "default" else {}
    if payload["identity"] is None:
        payload["identity"] = inherited.get("identity", _DEFAULT_IDENTITY) if inherited else ""
    if payload["tone"] is None:
        payload["tone"] = inherited.get("tone", "helpful") if inherited else ""
    return payload


async def resolved_draft(db, user, draft):
    from app.personas.assets import owned_asset
    payload = draft_payload(draft)
    hashes = {}
    for asset_id in payload["visual_identity"].values():
        if asset_id:
            asset = await owned_asset(db, user, asset_id)
            hashes[asset_id] = asset.sha256
    if hashes:
        payload["visual_asset_hashes"] = hashes
    return payload


def snapshot(key, revision, payload, *, policy_source=None):
    result = {"schema_version": 1, "persona": key, "revision": revision, "personality": payload}
    if policy_source is not None:
        result["policy_source"] = policy_source
    result["fingerprint"] = hashlib.sha256(encode(result).encode()).hexdigest()
    return result


def builtin_snapshot(key):
    if not persona_exists(key):
        raise HTTPException(400, f"Unknown persona '{key}'")
    pc = get_persona(key)
    payload = {"display_name": pc.get("display_name", key), "description": pc.get("description", ""),
               "personality_mode": pc.get("personality_mode", "default"),
               "identity": pc.get("identity", _DEFAULT_IDENTITY), "tone": pc.get("tone", "helpful"),
               "behavior_instructions": pc.get("behavior_instructions", [])}
    return snapshot(key, None, payload)


async def lock_owner(db, user):
    # Serialize owner mutations without conflicting with a new chat's FK key-share lock.
    owner = (await db.execute(select(User).where(User.id == user.id).with_for_update(key_share=True)
                              .execution_options(populate_existing=True))).scalar_one_or_none()
    if owner is None or not owner.is_active:
        raise HTTPException(403, "The account is unavailable.")
    return owner


def audit(db, user, action, **metadata):
    db.add(AdminPolicyEvent(actor_user_id=user.id, target_user_id=user.id,
                            action="persona." + action, summary_json=encode(metadata)))


async def owned_definition(db, user, persona_id):
    row = (await db.execute(select(PersonaDefinition).where(
        PersonaDefinition.id == persona_id, PersonaDefinition.user_id == user.id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Persona not found")
    return row


async def resolve_reference(db, user, ref):
    if not ref.persona.startswith("custom:"):
        if ref.revision is not None:
            raise HTTPException(422, "Bundled personas do not have numbered revisions.")
        return builtin_snapshot(ref.persona)
    definition = await owned_definition(db, user, ref.persona.removeprefix("custom:"))
    if definition.archived_at is not None:
        raise HTTPException(409, "Archived personas cannot be selected for new use.")
    if ref.revision is None:
        raise HTTPException(422, "Select an explicit persona revision.")
    revision = (await db.execute(select(PersonaRevision).where(
        PersonaRevision.persona_id == definition.id, PersonaRevision.version == ref.revision))).scalar_one_or_none()
    if revision is None:
        raise HTTPException(404, "Persona revision not found")
    return snapshot(ref.persona, revision.version, json.loads(revision.payload_json))


def context_from_snapshot(user, value):
    key = value["persona"]
    # Personality snapshots never freeze or supply account permissions.
    policy_key = (user.persona or "family_assistant") if key.startswith("custom:") or value.get("policy_source") == "account" else key
    try:
        ctx = UserContext.from_user(user, persona_name=policy_key)
    except PersonaPolicyError as exc:
        raise HTTPException(400, str(exc)) from None
    payload = value["personality"]
    return replace(ctx, persona=key, persona_description=payload["description"],
                   persona_identity=payload["identity"], persona_tone=payload["tone"],
                   behavior_instructions=list(payload["behavior_instructions"]),
                   neutral_personality=payload["personality_mode"] == "neutral")


async def create_definition(db, user, draft):
    await lock_owner(db, user)
    definition = PersonaDefinition(user_id=user.id, head_version=1)
    db.add(definition)
    await db.flush()
    db.add(PersonaRevision(persona_id=definition.id, version=1, payload_json=encode(await resolved_draft(db, user, draft))))
    audit(db, user, "created", persona_id=definition.id, revision=1)
    await db.flush()
    return definition


async def revise_definition(db, user, persona_id, expected_version, draft):
    await lock_owner(db, user)
    definition = await owned_definition(db, user, persona_id)
    result = await db.execute(update(PersonaDefinition).where(
        PersonaDefinition.id == definition.id, PersonaDefinition.head_version == expected_version,
        PersonaDefinition.archived_at.is_(None)).values(head_version=expected_version + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "Persona changed or was archived; reload before saving.")
    db.add(PersonaRevision(persona_id=persona_id, version=expected_version + 1,
                           payload_json=encode(await resolved_draft(db, user, draft))))
    audit(db, user, "revised", persona_id=persona_id, revision=expected_version + 1)
    await db.flush()
    return definition


async def set_default(db, user, ref, expected_version):
    await lock_owner(db, user)
    row = await db.get(UserPersonaDefault, user.id)
    if (row.version if row else 0) != expected_version:
        raise HTTPException(409, "Default changed; reload before saving.")
    value = await resolve_reference(db, user, ref)
    context_from_snapshot(user, value)
    if row is None:
        row = UserPersonaDefault(user_id=user.id, version=1, snapshot_json=encode(value))
        db.add(row)
    else:
        row.version += 1
        row.snapshot_json = encode(value)
    audit(db, user, "default_selected", persona=value["persona"], revision=value["revision"])
    await db.flush()
    return {"selection": value, "version": row.version}


async def get_default(db, user):
    row = await db.get(UserPersonaDefault, user.id)
    return {"selection": json.loads(row.snapshot_json) if row else builtin_snapshot(user.persona or "family_assistant"),
            "version": row.version if row else 0}


async def archive_definition(db, user, persona_id, expected_version):
    await lock_owner(db, user)
    definition = await owned_definition(db, user, persona_id)
    default = await get_default(db, user)
    if default["selection"]["persona"] == "custom:" + persona_id:
        raise HTTPException(409, "Choose another personal default before archiving this persona.")
    if definition.head_version != expected_version or definition.archived_at is not None:
        raise HTTPException(409, "Persona changed or was archived; reload before archiving.")
    definition.archived_at = datetime.now(timezone.utc)
    audit(db, user, "archived", persona_id=persona_id, revision=expected_version)
    await db.flush()


def _matches_persona(db, column, key):
    # SQLite's CAST(text AS JSON) is numeric coercion, unlike PostgreSQL's cast.
    value = type_coerce(column, JSON) if db.get_bind().dialect.name == "sqlite" else cast(column, JSON)
    return value["persona"].as_string() == key


def _asset_ids(payload):
    return {value for value in payload.get("visual_identity", {}).values() if value}


async def _deletion_state(db, user, definition, *, lock=False):
    key = "custom:" + definition.id
    selected_ids = select(PersonaSelection.session_id).where(_matches_persona(db, PersonaSelection.snapshot_json, key))
    historical_ids = select(ChatRun.session_id).join(ChatRunPersonaSnapshot).where(
        ChatRun.user_id == user.id, _matches_persona(db, ChatRunPersonaSnapshot.snapshot_json, key))
    query = select(ChatSession).where(ChatSession.user_id == user.id, or_(
        ChatSession.persona == key, ChatSession.id.in_(selected_ids), ChatSession.id.in_(historical_ids)))
    if lock:
        query = query.order_by(ChatSession.id).with_for_update(key_share=True).execution_options(populate_existing=True)
    sessions = (await db.execute(query)).scalars().all()
    selections = (await db.execute(select(PersonaSelection).join(ChatSession).where(
        ChatSession.user_id == user.id, _matches_persona(db, PersonaSelection.snapshot_json, key)))).scalars().all()
    runs = (await db.execute(select(ChatRunPersonaSnapshot).join(ChatRun).where(
        ChatRun.user_id == user.id, _matches_persona(db, ChatRunPersonaSnapshot.snapshot_json, key)))).scalars().all()
    default = await db.get(UserPersonaDefault, user.id, populate_existing=True)
    reset_default = default is not None and json.loads(default.snapshot_json)["persona"] == key
    affected = {row.id for row in sessions if row.persona == key} | {row.session_id for row in selections}
    # Confirmation binds to usage as well as the head: a newly attached chat/default
    # requires a new confirmation even when nobody edited the persona itself.
    evidence = {"id": definition.id, "version": definition.head_version,
                "archived": definition.archived_at is not None,
                "sessions": sorted(affected),
                "selections": sorted((row.session_id, row.version) for row in selections),
                "runs": sorted(row.run_id for row in runs),
                "default_version": default.version if reset_default else None}
    impact = {"expected_version": definition.head_version,
              "affected_conversations": len(affected), "resets_default": reset_default,
              "confirmation_token": hashlib.sha256(encode(evidence).encode()).hexdigest()}
    return impact, sessions, selections, runs, default


async def deletion_preview(db, user, persona_id):
    definition = await owned_definition(db, user, persona_id)
    impact, *_ = await _deletion_state(db, user, definition)
    return impact


async def delete_definition(db, user, persona_id, expected_version, confirmation_token):
    from app.chat_runtime import get_chat_run_manager
    await lock_owner(db, user)
    definition = await owned_definition(db, user, persona_id)
    impact, sessions, selections, runs, default = await _deletion_state(db, user, definition, lock=True)
    if definition.head_version != expected_version or impact["confirmation_token"] != confirmation_token:
        raise HTTPException(409, "Persona or usage changed; review deletion again.")
    ids = [row.id for row in sessions]
    busy = (await db.execute(select(ChatRun.id).where(ChatRun.user_id == user.id,
        ChatRun.session_id.in_(ids), ChatRun.status.in_(("running", "waiting_approval", "stopping"))).limit(1))).first()
    if busy or any([await get_chat_run_manager().is_active(session_id) for session_id in ids]):
        raise HTTPException(409, "Finish or stop affected conversations and pending approvals before deleting this persona.")

    # Assistant voice, but exactly the account policy used by the custom persona.
    # The built-in Assistant's own scope filters must not silently narrow access.
    fallback = snapshot("family_assistant", None, builtin_snapshot("family_assistant")["personality"], policy_source="account")
    context_from_snapshot(user, fallback)
    replacement = encode(fallback)
    candidates = set()
    revisions = (await db.execute(select(PersonaRevision).where(PersonaRevision.persona_id == persona_id))).scalars().all()
    for revision in revisions:
        candidates.update(_asset_ids(json.loads(revision.payload_json)))
    for row in [*selections, *runs, *([default] if impact["resets_default"] else [])]:
        candidates.update(_asset_ids(json.loads(row.snapshot_json)["personality"]))
        row.snapshot_json = replacement
        if hasattr(row, "version"):
            row.version += 1
    selected_ids = {row.session_id for row in selections}
    for session in sessions:
        if session.persona == "custom:" + persona_id or session.id in selected_ids:
            session.persona = "family_assistant"
            if session.id not in selected_ids:
                row = await db.get(PersonaSelection, session.id)
                if row is None:
                    db.add(PersonaSelection(session_id=session.id, version=1, snapshot_json=replacement))
                else:
                    row.snapshot_json = replacement
                    row.version += 1
    await db.flush()
    # Explicit deletion rather than ORM cascades also works in SQLite fixtures.
    await db.execute(delete(PersonaRevision).where(PersonaRevision.persona_id == persona_id))
    await db.delete(definition)
    await db.flush()

    if candidates:
        retained = set()
        payloads = (await db.execute(select(PersonaRevision.payload_json).join(PersonaDefinition).where(
            PersonaDefinition.user_id == user.id))).scalars().all()
        for payload in payloads:
            retained.update(_asset_ids(json.loads(payload)))
        for query in (
            select(PersonaSelection.snapshot_json).join(ChatSession).where(ChatSession.user_id == user.id),
            select(ChatRunPersonaSnapshot.snapshot_json).join(ChatRun).where(ChatRun.user_id == user.id),
            select(UserPersonaDefault.snapshot_json).where(UserPersonaDefault.user_id == user.id),
        ):
            for payload in (await db.execute(query)).scalars():
                retained.update(_asset_ids(json.loads(payload)["personality"]))
        await db.execute(delete(PersonaVisualAsset).where(PersonaVisualAsset.user_id == user.id,
            PersonaVisualAsset.id.in_(candidates - retained)))
    audit(db, user, "deleted", persona_id=persona_id, affected_conversations=impact["affected_conversations"],
          resets_default=impact["resets_default"])
    await db.flush()
    return {"affected_conversations": impact["affected_conversations"], "resets_default": impact["resets_default"],
            "replacement": fallback, "default": await get_default(db, user)}


async def lock_session(db, user, session_id):
    session = (await db.execute(select(ChatSession).where(
        ChatSession.id == session_id, ChatSession.user_id == user.id,
        ChatSession.is_active.is_(True)).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


async def initialize_session(db, user, session):
    await lock_owner(db, user)
    default = await get_default(db, user)
    value = default["selection"]
    context_from_snapshot(user, value)
    session.persona = value["persona"]
    db.add(PersonaSelection(session_id=session.id, snapshot_json=encode(value), version=1))
    await db.flush()


async def select_session(db, user, session_id, ref, expected_version=None):
    from app.chat_runtime import get_chat_run_manager
    await lock_owner(db, user)
    session = await lock_session(db, user, session_id)
    busy = (await db.execute(select(ChatRun.id).where(
        ChatRun.session_id == session_id,
        ChatRun.status.in_(("running", "waiting_approval", "stopping"))).limit(1))).scalar_one_or_none()
    if busy or await get_chat_run_manager().is_active(session_id):
        raise HTTPException(409, "Wait for the current turn or approval to finish before changing persona.")
    row = await db.get(PersonaSelection, session_id)
    if ref.persona.startswith("custom:") and expected_version is None:
        raise HTTPException(422, "Custom selections require expected_version.")
    if expected_version is not None and expected_version != (row.version if row else 0):
        raise HTTPException(409, "Session persona changed; reload before selecting.")
    value = await resolve_reference(db, user, ref)
    context_from_snapshot(user, value)
    session.persona = value["persona"]
    if row is None:
        row = PersonaSelection(session_id=session_id, snapshot_json=encode(value), version=1)
        db.add(row)
    else:
        row.snapshot_json = encode(value)
        row.version += 1
    audit(db, user, "session_selected", session_id=session_id, persona=value["persona"], revision=value["revision"])
    await db.flush()
    return session


async def session_selection(db, user, session_id):
    session = await lock_session(db, user, session_id)
    row = await db.get(PersonaSelection, session_id)
    value = json.loads(row.snapshot_json) if row else builtin_snapshot(session.persona)
    result = {"selection": value, "version": row.version if row else 0, "update_available": False, "archived": False}
    if value["persona"].startswith("custom:"):
        definition = await owned_definition(db, user, value["persona"].removeprefix("custom:"))
        result.update(update_available=definition.head_version > value["revision"],
                      archived=definition.archived_at is not None)
    options = await db.get(PersonaTestOptions, session_id)
    result["test_options"] = {"memory_recall_enabled": options.memory_recall_enabled} if options else None
    return result


async def session_context(db, user, session):
    session = await lock_session(db, user, session.id)
    row = await db.get(PersonaSelection, session.id)
    if row is None:
        # Legacy chats pin their current bundled style on the first new turn.
        row = PersonaSelection(session_id=session.id, version=1,
                               snapshot_json=encode(builtin_snapshot(session.persona)))
        db.add(row)
        await db.flush()
    value = json.loads(row.snapshot_json)
    ctx = context_from_snapshot(user, value)
    options = await db.get(PersonaTestOptions, session.id)
    ctx.memory_recall_enabled = options.memory_recall_enabled if options else True
    return ctx, value


def save_run_snapshot(db, run, value):
    db.add(ChatRunPersonaSnapshot(run_id=run.id, snapshot_json=encode(value)))


async def resume_context(db, user, session, run):
    row = await db.get(ChatRunPersonaSnapshot, run.id)
    if row is None:
        # Pre-migration runs have no recoverable historical style revision.
        value = builtin_snapshot(session.persona)
        save_run_snapshot(db, run, value)
    else:
        value = json.loads(row.snapshot_json)
    ctx = context_from_snapshot(user, value)
    options = await db.get(PersonaTestOptions, session.id)
    ctx.memory_recall_enabled = options.memory_recall_enabled if options else True
    return ctx
