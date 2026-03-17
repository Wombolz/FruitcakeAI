"""Add last_accessed_at to memories

Revision ID: 013_memory_last_accessed
Revises: 012_task_profile
Create Date: 2026-03-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "013_memory_last_accessed"
down_revision = "012_task_profile"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not _has_column(inspector, "memories", "last_accessed_at"):
        op.add_column("memories", sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_column(inspector, "memories", "last_accessed_at"):
        op.drop_column("memories", "last_accessed_at")
