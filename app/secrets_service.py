from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Secret, SecretAccessEvent


class SecretConfigurationError(RuntimeError):
    pass


class SecretDecryptionError(RuntimeError):
    pass


@dataclass(slots=True)
class ResolvedSecret:
    secret: Secret
    value: str


def _derived_master_key() -> str:
    source = (settings.secrets_master_key or '').strip()
    if not source:
        raise SecretConfigurationError('SECRETS_MASTER_KEY is required for secrets operations.')
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def _fernet() -> Fernet:
    return Fernet(_derived_master_key().encode("utf-8"))


def encrypt_secret_value(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret_value(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptionError(
            "Secret decryption failed. Rotate this secret or verify SECRETS_MASTER_KEY."
        ) from exc


def mask_secret_value(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return "****"
    tail = normalized[-4:] if len(normalized) >= 4 else normalized
    return f"****{tail}"


def normalize_secret_name(name: str) -> str:
    return (name or "").strip().lower()


async def get_secret_by_name(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
    mark_used: bool = False,
) -> Optional[Secret]:
    normalized_name = normalize_secret_name(name)
    result = await db.execute(
        select(Secret).where(
            Secret.user_id == user_id,
            func.lower(Secret.name) == normalized_name,
            Secret.is_active.is_(True),
        )
    )
    secret = result.scalar_one_or_none()
    if secret is not None and mark_used:
        secret.last_used_at = datetime.now(timezone.utc)
        await db.flush()
    return secret


async def audit_secret_access(
    db: AsyncSession,
    *,
    user_id: int,
    secret_name: str,
    tool_name: str,
    success: bool,
    secret_id: int | None = None,
    task_id: int | None = None,
    error_class: str | None = None,
) -> None:
    event = SecretAccessEvent(
        secret_id=secret_id,
        user_id=user_id,
        secret_name=normalize_secret_name(secret_name),
        task_id=task_id,
        tool_name=str(tool_name or '').strip() or 'unknown',
        success=bool(success),
        error_class=(str(error_class).strip() or None) if error_class else None,
    )
    db.add(event)
    await db.flush()


async def resolve_secret(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
    mark_used: bool = True,
    tool_name: str | None = None,
    task_id: int | None = None,
    audit: bool = False,
) -> Optional[ResolvedSecret]:
    normalized_name = normalize_secret_name(name)
    secret: Secret | None = None
    try:
        secret = await get_secret_by_name(db, user_id=user_id, name=normalized_name, mark_used=mark_used)
        if secret is None:
            if audit and tool_name:
                await audit_secret_access(
                    db,
                    user_id=user_id,
                    secret_name=normalized_name,
                    tool_name=tool_name,
                    task_id=task_id,
                    success=False,
                    error_class='SecretNotFound',
                )
            return None
        value = decrypt_secret_value(secret.ciphertext)
        if audit and tool_name:
            await audit_secret_access(
                db,
                user_id=user_id,
                secret_name=normalized_name,
                tool_name=tool_name,
                task_id=task_id,
                secret_id=int(secret.id),
                success=True,
            )
        return ResolvedSecret(secret=secret, value=value)
    except SecretDecryptionError as exc:
        if audit and tool_name:
            await audit_secret_access(
                db,
                user_id=user_id,
                secret_name=normalized_name,
                tool_name=tool_name,
                task_id=task_id,
                secret_id=int(secret.id) if secret is not None and secret.id is not None else None,
                success=False,
                error_class=exc.__class__.__name__,
            )
        raise
    except Exception as exc:
        if audit and tool_name:
            await audit_secret_access(
                db,
                user_id=user_id,
                secret_name=normalized_name,
                tool_name=tool_name,
                task_id=task_id,
                secret_id=int(secret.id) if secret is not None and secret.id is not None else None,
                success=False,
                error_class=exc.__class__.__name__,
            )
        raise


async def resolve_secret_value(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
    mark_used: bool = True,
    tool_name: str | None = None,
    task_id: int | None = None,
    audit: bool = False,
) -> Optional[str]:
    resolved = await resolve_secret(
        db,
        user_id=user_id,
        name=name,
        mark_used=mark_used,
        tool_name=tool_name,
        task_id=task_id,
        audit=audit,
    )
    if resolved is None:
        return None
    return resolved.value


async def list_secret_access_events(
    db: AsyncSession,
    *,
    user_id: int,
    secret_id: int | None = None,
    limit: int = 50,
) -> list[SecretAccessEvent]:
    query = (
        select(SecretAccessEvent)
        .where(SecretAccessEvent.user_id == user_id)
        .order_by(SecretAccessEvent.created_at.desc(), SecretAccessEvent.id.desc())
        .limit(max(1, min(int(limit or 50), 200)))
    )
    if secret_id is not None:
        query = query.where(SecretAccessEvent.secret_id == secret_id)
    result = await db.execute(query)
    return list(result.scalars().all())
