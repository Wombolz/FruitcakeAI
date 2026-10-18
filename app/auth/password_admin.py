"""Local operator helpers; generated credentials never go to application logs."""

import json
import os
from pathlib import Path
import secrets

from sqlalchemy import select

DEFAULT_PASSWORD = "changeme123"


class PasswordAdminError(ValueError):
    """An operator-safe error containing no credential or connection details."""


def generated_password() -> str:
    return secrets.token_urlsafe(24)


def write_private_credentials(path: Path, credentials: dict[str, str]) -> None:
    """Refuse overwrites and symlinks; create an owner-readable recovery file."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(credentials, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


async def reset_password(db, username: str, password: str) -> None:
    from app.auth.jwt import hash_password, verify_password
    from app.db.models import User

    if len(password) < 16:
        raise PasswordAdminError("Use at least 16 characters for the new password.")
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None:
        raise PasswordAdminError("No account with that username.")
    if verify_password(password, user.hashed_password):
        raise PasswordAdminError("The new password must differ from the existing password.")
    user.hashed_password = hash_password(password)
    await db.flush()


async def rotate_default_passwords(db, output: Path) -> list[str]:
    from app.auth.jwt import hash_password, verify_password
    from app.db.models import User

    users = (await db.execute(select(User).order_by(User.username))).scalars().all()
    changed = [user for user in users if verify_password(DEFAULT_PASSWORD, user.hashed_password)]
    if not changed:
        return []
    passwords = {user.username: generated_password() for user in changed}
    # Save recovery material before committing any credential changes.
    write_private_credentials(output, passwords)
    for user in changed:
        user.hashed_password = hash_password(passwords[user.username])
    await db.flush()
    return list(passwords)
