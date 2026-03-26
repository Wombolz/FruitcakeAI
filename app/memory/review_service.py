from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Memory, MemoryProposal, TaskRunArtifact
from app.memory.service import get_memory_service


def decode_proposal_payload(raw: str | None) -> Dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Memory proposal payload is malformed",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Memory proposal payload is malformed",
        )
    return payload


def encode_proposal_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(payload or {}, ensure_ascii=True, sort_keys=True)


def topic_watcher_tags(topic: str) -> List[str]:
    tags = ["topic_watcher"]
    topic = (topic or "").strip().lower()
    if topic:
        slug = re.sub(r"[^a-z0-9]+", "_", topic).strip("_")
        if slug:
            tags.append(slug)
    return tags


def parse_optional_iso_datetime(raw: str | None) -> Optional[datetime]:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Memory proposal payload is malformed",
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


async def find_existing_memory_for_proposal(
    db: AsyncSession,
    *,
    user_id: int,
    proposal: MemoryProposal,
) -> Optional[Memory]:
    payload = decode_proposal_payload(proposal.proposal_json)
    memory_type = str(payload.get("memory_type") or "").strip()
    content = str(payload.get("content") or "").strip()
    if memory_type not in {"semantic", "procedural", "episodic"} or not content:
        return None

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Memory)
        .where(
            and_(
                Memory.user_id == user_id,
                Memory.is_active == True,
                Memory.memory_type == memory_type,
                Memory.content == content,
                (Memory.expires_at.is_(None) | (Memory.expires_at >= now)),
            )
        )
        .order_by(desc(Memory.created_at), desc(Memory.id))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_flat_memory_from_proposal(
    db: AsyncSession,
    *,
    proposal: MemoryProposal,
    user_id: int,
):
    payload = decode_proposal_payload(proposal.proposal_json)
    memory_type = str(payload.get("memory_type") or "").strip()
    content = str(payload.get("content") or "").strip()
    if proposal.proposal_type != "flat_memory_create" or memory_type not in {"semantic", "procedural", "episodic"} or not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Memory proposal payload is malformed",
        )

    expires_at = payload.get("expires_at")
    topic = str(payload.get("topic") or "").strip()
    existing = await find_existing_memory_for_proposal(db, user_id=user_id, proposal=proposal)
    if existing is not None:
        return existing

    svc = get_memory_service()
    result = await svc.create(
        db=db,
        user_id=user_id,
        memory_type=memory_type,
        content=content,
        importance=0.65,
        tags=topic_watcher_tags(topic),
        expires_at=parse_optional_iso_datetime(expires_at) if expires_at else None,
    )
    if isinstance(result, str):
        existing = await find_existing_memory_for_proposal(db, user_id=user_id, proposal=proposal)
        if existing is not None:
            return existing
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result)
    return result


async def latest_memory_candidates_artifact(
    db: AsyncSession,
    *,
    task_run_id: int,
) -> Optional[TaskRunArtifact]:
    result = await db.execute(
        select(TaskRunArtifact)
        .where(TaskRunArtifact.task_run_id == task_run_id, TaskRunArtifact.artifact_type == "memory_candidates")
        .order_by(desc(TaskRunArtifact.id))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def sync_artifact_candidate_status(
    db: AsyncSession,
    *,
    proposal: MemoryProposal,
) -> None:
    if not proposal.task_run_id:
        return
    artifact = await latest_memory_candidates_artifact(db, task_run_id=int(proposal.task_run_id))
    if artifact is None:
        return
    payload = decode_proposal_payload(artifact.content_json)
    candidates = payload.get("candidates") or []
    changed = False
    proposal_payload = proposal.proposal_payload
    proposal_key = str(proposal.proposal_key or "")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_key = str(candidate.get("proposal_key") or candidate.get("candidate_key") or "")
        if candidate_key and candidate_key == proposal_key:
            candidate["status"] = proposal.status
            candidate["approved_memory_id"] = proposal.approved_memory_id
            candidate["approved_at"] = proposal.resolved_at.isoformat() if proposal.approved_memory_id and proposal.resolved_at else None
            candidate["approved_by_user_id"] = proposal.resolved_by_user_id if proposal.approved_memory_id else None
            candidate["rejected_at"] = proposal.resolved_at.isoformat() if proposal.status == "rejected" and proposal.resolved_at else None
            candidate["rejected_by_user_id"] = proposal.resolved_by_user_id if proposal.status == "rejected" else None
            candidate["proposal_id"] = proposal.id
            changed = True
            break
        if not candidate_key and str(candidate.get("content") or "").strip() == str(proposal.content or "").strip():
            candidate["status"] = proposal.status
            candidate["approved_memory_id"] = proposal.approved_memory_id
            candidate["approved_at"] = proposal.resolved_at.isoformat() if proposal.approved_memory_id and proposal.resolved_at else None
            candidate["approved_by_user_id"] = proposal.resolved_by_user_id if proposal.approved_memory_id else None
            candidate["rejected_at"] = proposal.resolved_at.isoformat() if proposal.status == "rejected" and proposal.resolved_at else None
            candidate["rejected_by_user_id"] = proposal.resolved_by_user_id if proposal.status == "rejected" else None
            candidate["proposal_id"] = proposal.id
            changed = True
            break
    if changed:
        artifact.content_json = encode_proposal_payload(payload)
