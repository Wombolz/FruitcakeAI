"""Persistence and credential resolution for user-owned integrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Secret, UserIntegration
from app.secrets_service import decrypt_secret_value, encrypt_secret_value


GOOGLE_CALENDAR_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar",
)


@dataclass(frozen=True)
class ResolvedIntegration:
    public_id: str
    provider: str
    service: str
    account_identifier: str
    scopes: tuple[str, ...]
    config: dict[str, Any]
    access_token: str = ""
    refresh_token: str = ""
    credential: str = ""
    expires_at: datetime | None = None


async def list_user_integrations(db: AsyncSession, user_id: int) -> list[UserIntegration]:
    return list((await db.execute(
        select(UserIntegration)
        .where(UserIntegration.user_id == user_id)
        .order_by(UserIntegration.provider, UserIntegration.service)
    )).scalars().all())


async def get_user_integration(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    service: str = "calendar",
    for_update: bool = False,
) -> UserIntegration | None:
    query = select(UserIntegration).where(
        UserIntegration.user_id == user_id,
        UserIntegration.provider == provider,
        UserIntegration.service == service,
    )
    if for_update:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def connect_apple_calendar(
    db: AsyncSession,
    *,
    user_id: int,
    username: str,
    app_password: str,
    url: str,
    default_calendar: str,
) -> UserIntegration:
    row = await get_user_integration(
        db, user_id=user_id, provider="apple", service="calendar", for_update=True
    )
    if row is None:
        row = UserIntegration(user_id=user_id, provider="apple", service="calendar")
        db.add(row)
        await db.flush()
    secret = await _upsert_secret(
        db,
        user_id=user_id,
        secret_id=row.credential_secret_id,
        name=f"integration:{row.public_id}:credential",
        provider="apple_caldav",
        value=app_password,
    )
    row.credential_secret_id = secret.id
    row.account_identifier = username
    row.config = {"url": url, "username": username, "default_calendar": default_calendar}
    row.scopes = ["calendar"]
    row.status = "connected"
    row.error_class = None
    row.error_message = None
    await db.flush()
    return row


async def connect_google_calendar(
    db: AsyncSession,
    *,
    user_id: int,
    token_payload: dict[str, Any],
    account_identifier: str = "",
) -> UserIntegration:
    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("Google token exchange did not return an access token")
    row = await get_user_integration(
        db, user_id=user_id, provider="google", service="calendar", for_update=True
    )
    if row is None:
        row = UserIntegration(user_id=user_id, provider="google", service="calendar")
        db.add(row)
        await db.flush()
    access_secret = await _upsert_secret(
        db,
        user_id=user_id,
        secret_id=row.access_token_secret_id,
        name=f"integration:{row.public_id}:access-token",
        provider="google_oauth",
        value=access_token,
    )
    row.access_token_secret_id = access_secret.id
    refresh_token = str(token_payload.get("refresh_token") or "").strip()
    if refresh_token:
        refresh_secret = await _upsert_secret(
            db,
            user_id=user_id,
            secret_id=row.refresh_token_secret_id,
            name=f"integration:{row.public_id}:refresh-token",
            provider="google_oauth",
            value=refresh_token,
        )
        row.refresh_token_secret_id = refresh_secret.id
    expires_in = max(0, int(token_payload.get("expires_in") or 0))
    row.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in) if expires_in else None
    row.account_identifier = account_identifier or row.account_identifier
    row.config = {"default_calendar": "primary"}
    row.scopes = str(token_payload.get("scope") or " ".join(GOOGLE_CALENDAR_SCOPES)).split()
    row.status = "connected"
    row.error_class = None
    row.error_message = None
    await db.flush()
    return row


async def disconnect_integration(db: AsyncSession, row: UserIntegration) -> None:
    for secret_id in (
        row.access_token_secret_id,
        row.refresh_token_secret_id,
        row.credential_secret_id,
    ):
        if secret_id is None:
            continue
        secret = await db.get(Secret, secret_id)
        if secret is not None and secret.user_id == row.user_id:
            secret.is_active = False
    row.status = "disconnected"
    row.error_class = None
    row.error_message = None
    await db.flush()


async def resolve_user_integration(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    service: str = "calendar",
) -> ResolvedIntegration | None:
    row = await get_user_integration(db, user_id=user_id, provider=provider, service=service)
    if row is None or row.status != "connected":
        return None
    access = await _secret_value(db, row.access_token_secret_id, user_id)
    refresh = await _secret_value(db, row.refresh_token_secret_id, user_id)
    credential = await _secret_value(db, row.credential_secret_id, user_id)
    return ResolvedIntegration(
        public_id=row.public_id,
        provider=row.provider,
        service=row.service,
        account_identifier=str(row.account_identifier or ""),
        scopes=tuple(row.scopes),
        config=dict(row.config),
        access_token=access,
        refresh_token=refresh,
        credential=credential,
        expires_at=row.expires_at,
    )


async def refresh_google_integration(db: AsyncSession, *, user_id: int) -> UserIntegration:
    row = await get_user_integration(
        db, user_id=user_id, provider="google", service="calendar", for_update=True
    )
    if row is None or row.status != "connected":
        raise ValueError("Google Calendar is not connected")
    refresh_token = await _secret_value(db, row.refresh_token_secret_id, user_id)
    if not refresh_token:
        raise ValueError("Google Calendar connection has no refresh token")
    payload = await _post_google_token({
        "client_id": settings.google_oauth_client_id,
        "client_secret": settings.google_oauth_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    await connect_google_calendar(
        db,
        user_id=user_id,
        token_payload={**payload, "refresh_token": refresh_token},
        account_identifier=str(row.account_identifier or ""),
    )
    return row


async def exchange_google_code(*, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    return await _post_google_token({
        "client_id": settings.google_oauth_client_id,
        "client_secret": settings.google_oauth_client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })


async def _post_google_token(data: dict[str, Any]) -> dict[str, Any]:
    if not settings.google_oauth_client_id:
        raise ValueError("Google OAuth is not configured")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post("https://oauth2.googleapis.com/token", data=data)
    if response.status_code >= 400:
        raise ValueError("Google OAuth token exchange failed")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Google OAuth returned an invalid response")
    return payload


async def _upsert_secret(
    db: AsyncSession,
    *,
    user_id: int,
    secret_id: int | None,
    name: str,
    provider: str,
    value: str,
) -> Secret:
    secret = await db.get(Secret, secret_id) if secret_id is not None else None
    if secret is None or secret.user_id != user_id:
        secret = Secret(user_id=user_id, name=name, provider=provider)
        db.add(secret)
    secret.ciphertext = encrypt_secret_value(value)
    secret.is_active = True
    await db.flush()
    return secret


async def _secret_value(db: AsyncSession, secret_id: int | None, user_id: int) -> str:
    if secret_id is None:
        return ""
    secret = await db.get(Secret, secret_id)
    if secret is None or secret.user_id != user_id or not secret.is_active:
        return ""
    secret.last_used_at = datetime.now(timezone.utc)
    return decrypt_secret_value(secret.ciphertext)
