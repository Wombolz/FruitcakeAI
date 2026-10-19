"""
FruitcakeAI v5 — JWT helpers
Ported from v4, adapted for v5 role model (admin | parent | restricted | guest).
"""

from datetime import datetime, timedelta
from typing import Any, Dict

import bcrypt
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, status

from app.config import settings

password_hasher = PasswordHasher()


def create_access_token(user_id: int, username: str, role: str) -> str:
    payload: Dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "type": "access",
        "exp": datetime.utcnow() + timedelta(hours=settings.jwt_expiration_hours),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int) -> str:
    payload: Dict[str, Any] = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": datetime.utcnow() + timedelta(days=settings.jwt_refresh_expiration_days),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: str = "access") -> Dict[str, Any]:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != expected_type:
            raise credentials_exc
        return payload
    except jwt.InvalidTokenError:
        raise credentials_exc


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        if hashed.startswith(("$2a$", "$2b$", "$2y$")):
            # Passlib's legacy bcrypt truncated UTF-8 bytes at 72, even mid-codepoint.
            # Only legacy verification retains that behavior; new hashes use Argon2id.
            return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("ascii"))
        return password_hasher.verify(hashed, plain)
    except (InvalidHashError, VerificationError, ValueError, UnicodeError):
        return False
