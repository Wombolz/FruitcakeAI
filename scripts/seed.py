#!/usr/bin/env python3
"""
FruitcakeAI v5 — Database seed script
Creates default users from config/users.yaml.

Usage (from project root with venv active):
    python scripts/seed.py
"""

import asyncio
import sys
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# Make sure the project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.session import AsyncSessionLocal
from app.db.models import User
from app.auth.jwt import hash_password


async def seed():
    config_path = Path(__file__).parent.parent / "config" / "users.yaml"
    users_config = yaml.safe_load(config_path.read_text())

    async with AsyncSessionLocal() as db:
        created = 0
        skipped = 0

        for u in users_config["users"]:
            result = await db.execute(select(User).where(User.username == u["username"]))
            existing = result.scalar_one_or_none()

            if existing:
                print(f"  skip  {u['username']} (already exists)")
                skipped += 1
                continue

            user = User(
                username=u["username"],
                email=u["email"],
                hashed_password=hash_password(u["password"]),
                full_name=u.get("full_name"),
                role=u["role"],
                persona=u.get("persona", "family_assistant"),
            )
            user.library_scopes = u.get("library_scopes", ["family_docs"])
            db.add(user)
            print(f"  create {u['username']} ({u['role']})")
            created += 1

        await db.commit()
        print(f"\nDone — {created} created, {skipped} skipped.")


if __name__ == "__main__":
    asyncio.run(seed())
