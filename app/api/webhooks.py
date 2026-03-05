"""
FruitcakeAI v5 — Webhooks API (Phase 5.1)

POST   /webhooks/trigger/{key}   Inbound trigger (external, no auth)
GET    /webhooks                 List current user's webhook configs
POST   /webhooks                 Create webhook config
DELETE /webhooks/{id}            Delete webhook config
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import UserContext
from app.agent.core import run_agent
from app.auth.dependencies import get_current_user
from app.db.models import ChatMessage, ChatSession, User, WebhookConfig
from app.db.session import AsyncSessionLocal, get_db

router = APIRouter()
log = structlog.get_logger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────

class WebhookCreate(BaseModel):
    name: str
    instruction: str
    active: bool = True


class WebhookOut(BaseModel):
    id: int
    name: str
    webhook_key: str
    instruction: str
    active: bool
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class WebhookTriggerOut(BaseModel):
    accepted: bool
    webhook_id: int


# ── Public trigger endpoint (no auth) ────────────────────────────────────────

@router.post("/webhooks/trigger/{webhook_key}", response_model=WebhookTriggerOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_webhook(
    webhook_key: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> WebhookTriggerOut:
    """
    Trigger an active webhook by secret key.
    External callers (GitHub/Zapier/IFTTT) should POST JSON payloads.
    """
    result = await db.execute(
        select(WebhookConfig).where(
            WebhookConfig.webhook_key == webhook_key,
            WebhookConfig.active == True,
        )
    )
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

    payload = await _parse_payload(request)
    background_tasks.add_task(_execute_webhook, cfg.id, payload)

    return WebhookTriggerOut(accepted=True, webhook_id=cfg.id)


# ── Authenticated webhook config CRUD ────────────────────────────────────────

@router.get("/webhooks", response_model=List[WebhookOut])
async def list_webhooks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[WebhookConfig]:
    result = await db.execute(
        select(WebhookConfig)
        .where(WebhookConfig.user_id == current_user.id)
        .order_by(desc(WebhookConfig.created_at))
    )
    return result.scalars().all()


@router.post("/webhooks", response_model=WebhookOut, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookConfig:
    webhook = WebhookConfig(
        user_id=current_user.id,
        name=body.name.strip(),
        instruction=body.instruction.strip(),
        webhook_key=await _new_webhook_key(db),
        active=body.active,
    )
    if not webhook.name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name is required")
    if not webhook.instruction:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="instruction is required")

    db.add(webhook)
    await db.flush()
    return webhook


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_webhook(
    webhook_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    result = await db.execute(
        select(WebhookConfig).where(
            WebhookConfig.id == webhook_id,
            WebhookConfig.user_id == current_user.id,
        )
    )
    webhook = result.scalar_one_or_none()
    if webhook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    await db.delete(webhook)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Internals ─────────────────────────────────────────────────────────────────

async def _new_webhook_key(db: AsyncSession) -> str:
    """Generate a unique webhook key."""
    for _ in range(5):
        key = secrets.token_urlsafe(24)
        existing = await db.execute(
            select(WebhookConfig.id).where(WebhookConfig.webhook_key == key)
        )
        if existing.scalar_one_or_none() is None:
            return key
    raise HTTPException(status_code=500, detail="Failed to generate unique webhook key")


async def _parse_payload(request: Request) -> Dict[str, Any]:
    """Best-effort JSON payload parsing for webhook callers."""
    body = await request.body()
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    return parsed if isinstance(parsed, dict) else {"payload": parsed}


async def _execute_webhook(webhook_id: int, payload: Dict[str, Any]) -> None:
    """Execute webhook instruction in an isolated agent session."""
    # Load webhook + owner and create hidden task-style session
    # Extract needed values before session closes to avoid DetachedInstanceError
    session_id: Optional[int] = None
    user_id: Optional[int] = None
    persona: Optional[str] = None
    cfg_name: Optional[str] = None
    cfg_instruction: Optional[str] = None

    async with AsyncSessionLocal() as db:
        cfg = await db.get(WebhookConfig, webhook_id)
        if cfg is None or not cfg.active:
            return

        user = await db.get(User, cfg.user_id)
        if user is None:
            log.warning("webhook.user_not_found", webhook_id=webhook_id, user_id=cfg.user_id)
            return

        persona = user.persona or "family_assistant"
        cfg_name = cfg.name
        cfg_instruction = cfg.instruction

        session = ChatSession(
            user_id=user.id,
            title=f"[Webhook] {cfg_name}",
            persona=persona,
            is_task_session=True,
        )
        db.add(session)
        await db.flush()
        session_id = session.id
        user_id = user.id
        await db.commit()

    # Re-fetch user and build context while session is open; user becomes detached after
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None:
            log.warning("webhook.user_not_found", webhook_id=webhook_id, user_id=user_id)
            return
        user_context = UserContext.from_user(user, persona_name=persona)
    user_context.session_id = session_id

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload_json = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    prompt = (
        f"[Webhook: {cfg_name}]\n"
        f"{cfg_instruction}\n\n"
        f"Current time: {now}\n"
        f"Webhook payload JSON: {payload_json}"
    )

    try:
        result = await run_agent([{"role": "user", "content": prompt}], user_context, mode="task")
    except Exception as exc:
        log.error("webhook.execution_failed", webhook_id=webhook_id, error=str(exc), exc_info=True)
        result = f"Webhook execution failed: {exc}"

    # Persist execution transcript in the isolated session
    async with AsyncSessionLocal() as db:
        db.add(ChatMessage(session_id=session_id, role="user", content=prompt))
        db.add(ChatMessage(session_id=session_id, role="assistant", content=result))
        await db.commit()
