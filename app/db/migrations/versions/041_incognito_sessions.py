"""add chat_sessions.is_incognito for admin incognito sessions

Revision ID: 041_incognito_sessions
Revises: 040_task_presentation
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "041_incognito_sessions"
down_revision = "040_task_presentation"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not _has_column(inspector, "chat_sessions", "is_incognito"):
        op.add_column(
            "chat_sessions",
            sa.Column("is_incognito", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_column(inspector, "chat_sessions", "is_incognito"):
        op.drop_column("chat_sessions", "is_incognito")
