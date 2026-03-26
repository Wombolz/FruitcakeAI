from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Secret


def _derived_master_key() -> str:
    source = settings.secrets_master_key or settings.jwt_secret_key
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def _fernet() -> Fernet:
    return Fernet(_derived_master_key().encode("utf-8"))


def encrypt_secret_value(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret_value(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


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


async def resolve_secret_value(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
    mark_used: bool = True,
) -> Optional[str]:
    secret = await get_secret_by_name(db, user_id=user_id, name=name, mark_used=mark_used)
    if secret is None:
        return None
    return decrypt_secret_value(secret.ciphertext)
