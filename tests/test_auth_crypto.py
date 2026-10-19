"""Authentication compatibility across the dependency refresh."""

from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import HTTPException
import jwt
import pytest

from app.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.config import settings


def test_new_passwords_use_argon2id():
    hashed = hash_password("test-password")
    assert hashed.startswith("$argon2id$")
    assert verify_password("test-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_new_passwords_do_not_truncate_at_72_bytes():
    prefix = "a" * 72
    hashed = hash_password(prefix + "first")
    assert verify_password(prefix + "first", hashed)
    assert not verify_password(prefix + "second", hashed)


@pytest.mark.parametrize("password", ["legacy-password", "a" * 90, "a" * 71 + "\N{EURO SIGN}"])
def test_legacy_passlib_bcrypt_passwords_still_verify(password):
    hashed = bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(rounds=4)).decode("ascii")
    assert verify_password(password, hashed)
    assert not verify_password("wrong-password", hashed)


@pytest.mark.parametrize("hashed", ["", "not-a-hash", "$2b$invalid", "$argon2id$invalid"])
def test_invalid_password_hash_fails_closed(hashed):
    assert not verify_password("password", hashed)


def test_access_and_refresh_tokens_remain_separate():
    access = create_access_token(42, "alice", "parent")
    refresh = create_refresh_token(42)
    assert decode_token(access)["sub"] == "42"
    assert decode_token(refresh, expected_type="refresh")["sub"] == "42"
    with pytest.raises(HTTPException) as exc:
        decode_token(refresh)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("kind", ["expired", "wrong-key", "wrong-algorithm", "unsigned"])
def test_bad_jwt_is_rejected(kind):
    payload = {"sub": "42", "type": "access", "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    key, algorithm = settings.jwt_secret_key, settings.jwt_algorithm
    if kind == "expired":
        payload["exp"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    elif kind == "wrong-key":
        key = "not-the-server-secret" * 3
    elif kind == "wrong-algorithm":
        algorithm = "HS512" if settings.jwt_algorithm != "HS512" else "HS256"
    else:
        key, algorithm = "", "none"
    token = jwt.encode(payload, key, algorithm=algorithm)
    with pytest.raises(HTTPException) as exc:
        decode_token(token)
    assert exc.value.status_code == 401


def test_old_hs256_token_wire_format_is_accepted(monkeypatch):
    # Fixed token produced by the old python-jose implementation, not by PyJWT.
    monkeypatch.setattr(settings, "jwt_secret_key", "legacy-migration-test-secret-32-bytes")
    monkeypatch.setattr(settings, "jwt_algorithm", "HS256")
    token = LEGACY_TOKEN
    assert decode_token(token)["sub"] == "42"


LEGACY_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiI0MiIsInR5cGUiOiJhY2Nlc3MiLCJleHAiOjQxMDI0NDQ4MDB9."
    "a5ECEAY7CwARitM66_L6etd6V0kPi_ixQuGAI4IYVQY"
)
