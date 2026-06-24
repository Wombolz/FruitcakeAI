"""add task presentation metadata

Revision ID: 040_task_presentation
Revises: 039_host_root_approvals
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "040_task_presentation"
down_revision = "039_host_root_approvals"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not _has_column(inspector, "tasks", "presentation_json"):
        op.add_column(
            "tasks",
            sa.Column("presentation_json", sa.Text(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_column(inspector, "tasks", "presentation_json"):
        op.drop_column("tasks", "presentation_json")
