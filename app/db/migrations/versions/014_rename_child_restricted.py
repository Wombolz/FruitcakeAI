"""Rename legacy child/kids persona values to restricted naming

Revision ID: 014_rename_child_restricted
Revises: 013_memory_last_accessed
Create Date: 2026-03-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection, Inspector


revision = "014_rename_child_restricted"
down_revision = "013_memory_last_accessed"
branch_labels = None
depends_on = None


def _has_table(inspector: Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _apply_updates(conn: Connection, role_from: str, role_to: str, persona_from: str, persona_to: str) -> None:
    inspector = sa.inspect(conn)

    if _has_table(inspector, "users"):
        conn.execute(
            sa.text("UPDATE users SET role = :role_to WHERE role = :role_from"),
            {"role_from": role_from, "role_to": role_to},
        )
        conn.execute(
            sa.text("UPDATE users SET persona = :persona_to WHERE persona = :persona_from"),
            {"persona_from": persona_from, "persona_to": persona_to},
        )

    if _has_table(inspector, "tasks"):
        conn.execute(
            sa.text("UPDATE tasks SET persona = :persona_to WHERE persona = :persona_from"),
            {"persona_from": persona_from, "persona_to": persona_to},
        )

    if _has_table(inspector, "chat_sessions"):
        conn.execute(
            sa.text("UPDATE chat_sessions SET persona = :persona_to WHERE persona = :persona_from"),
            {"persona_from": persona_from, "persona_to": persona_to},
        )


def upgrade() -> None:
    conn = op.get_bind()
    _apply_updates(
        conn,
        role_from="child",
        role_to="restricted",
        persona_from="kids_assistant",
        persona_to="restricted_assistant",
    )


def downgrade() -> None:
    conn = op.get_bind()
    _apply_updates(
        conn,
        role_from="restricted",
        role_to="child",
        persona_from="restricted_assistant",
        persona_to="kids_assistant",
    )
