"""Authenticated user-owned integration management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import IntegrationOAuthState, User, UserIntegration
from app.db.session import get_db
from app.integrations.service import (
    GOOGLE_CALENDAR_SCOPES,
    connect_apple_calendar,
    connect_google_calendar,
    disconnect_integration,
    exchange_google_code,
    list_user_integrations,
    refresh_google_integration,
)


router = APIRouter()
_STATE_TTL_MINUTES = 10


class AppleCalendarConnect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=255)
    app_password: str = Field(min_length=4, max_length=255)
    url: HttpUrl = "https://caldav.icloud.com"
    default_calendar: str = Field(default="home", min_length=1, max_length=255)


class GoogleCalendarCallback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    state: str = Field(min_length=1)
    code_verifier: str = Field(min_length=43, max_length=128)


def _serialize(row: UserIntegration) -> dict:
    return {
        "id": row.public_id,
        "provider": row.provider,
        "service": row.service,
        "status": row.status,
        "account_identifier": row.account_identifier,
        "scopes": row.scopes,
        "config": {
            key: value
            for key, value in row.config.items()
            if key in {"url", "username", "default_calendar"}
        },
        "expires_at": row.expires_at,
        "last_success_at": row.last_success_at,
        "error_class": row.error_class,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("")
async def list_integrations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_user_integrations(db, current_user.id)
    return {"integrations": [_serialize(row) for row in rows]}


@router.post("/apple/calendar/connect", status_code=status.HTTP_201_CREATED)
async def connect_apple(
    body: AppleCalendarConnect,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.mcp.servers.calendar import verify_apple_caldav

    if body.url.scheme != "https":
        raise HTTPException(status_code=422, detail="Apple CalDAV URL must use HTTPS")
    try:
        await verify_apple_caldav(
            url=str(body.url).rstrip("/"),
            username=body.username.strip(),
            password=body.app_password.strip(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Apple Calendar credentials could not be verified",
        ) from exc
    row = await connect_apple_calendar(
        db,
        user_id=current_user.id,
        username=body.username.strip(),
        app_password=body.app_password.strip(),
        url=str(body.url).rstrip("/"),
        default_calendar=body.default_calendar.strip(),
    )
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.get("/google/calendar/auth-url")
async def google_auth_url(
    code_challenge: str = Query(min_length=43, max_length=128),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not settings.google_oauth_client_id or not settings.google_oauth_redirect_uri:
        raise HTTPException(status_code=503, detail="Google Calendar OAuth is not configured")
    now = datetime.now(timezone.utc)
    nonce = secrets.token_urlsafe(18)
    expires_at = now + timedelta(minutes=_STATE_TTL_MINUTES)
    db.add(IntegrationOAuthState(
        nonce=nonce,
        user_id=current_user.id,
        provider="google",
        service="calendar",
        redirect_uri=settings.google_oauth_redirect_uri,
        code_challenge=code_challenge,
        expires_at=expires_at,
    ))
    await db.flush()
    state_token = jwt.encode(
        {
            "sub": current_user.public_id,
            "purpose": "google_calendar_oauth",
            "redirect_uri": settings.google_oauth_redirect_uri,
            "nonce": nonce,
            "iat": now,
            "exp": expires_at,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    query = urlencode({
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_CALENDAR_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state_token,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return {"authorization_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"}


@router.post("/google/calendar/callback", status_code=status.HTTP_201_CREATED)
async def google_callback(
    body: GoogleCalendarCallback,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        state_payload = jwt.decode(
            body.state,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise HTTPException(status_code=400, detail="Google OAuth state is invalid or expired") from exc
    if (
        state_payload.get("purpose") != "google_calendar_oauth"
        or state_payload.get("sub") != current_user.public_id
        or state_payload.get("redirect_uri") != settings.google_oauth_redirect_uri
    ):
        raise HTTPException(status_code=400, detail="Google OAuth state does not match this user")
    nonce = str(state_payload.get("nonce") or "")
    state_row = (await db.execute(
        select(IntegrationOAuthState)
        .where(
            IntegrationOAuthState.nonce == nonce,
            IntegrationOAuthState.user_id == current_user.id,
            IntegrationOAuthState.provider == "google",
            IntegrationOAuthState.service == "calendar",
        )
        .with_for_update()
    )).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    state_expires_at = state_row.expires_at if state_row is not None else None
    if state_expires_at is not None and state_expires_at.tzinfo is None:
        state_expires_at = state_expires_at.replace(tzinfo=timezone.utc)
    if state_row is None or state_row.consumed_at is not None or state_expires_at <= now:
        raise HTTPException(status_code=400, detail="Google OAuth state is unavailable or already used")
    if state_row.redirect_uri != settings.google_oauth_redirect_uri:
        raise HTTPException(status_code=400, detail="Google OAuth redirect does not match")
    verifier_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(body.code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    if not secrets.compare_digest(verifier_challenge, state_row.code_challenge):
        raise HTTPException(status_code=400, detail="Google OAuth PKCE verification failed")
    try:
        token_payload = await exchange_google_code(
            code=body.code,
            code_verifier=body.code_verifier,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
        account_identifier = await _google_account_identifier(str(token_payload.get("access_token") or ""))
        row = await connect_google_calendar(
            db,
            user_id=current_user.id,
            token_payload=token_payload,
            account_identifier=account_identifier,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state_row.consumed_at = now
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.post("/{public_id}/refresh")
async def refresh_integration(
    public_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _owned_integration(db, current_user.id, public_id)
    if row.provider != "google" or row.service != "calendar":
        raise HTTPException(status_code=400, detail="This integration does not support token refresh")
    try:
        row = await refresh_google_integration(db, user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.post("/{public_id}/disconnect")
async def disconnect(
    public_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _owned_integration(db, current_user.id, public_id)
    await disconnect_integration(db, row)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


async def _owned_integration(db: AsyncSession, user_id: int, public_id: str) -> UserIntegration:
    row = (await db.execute(select(UserIntegration).where(
        UserIntegration.public_id == public_id,
        UserIntegration.user_id == user_id,
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Integration not found")
    return row


async def _google_account_identifier(access_token: str) -> str:
    if not access_token:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code < 400:
            return str(response.json().get("email") or "")
    except Exception:
        pass
    return ""
