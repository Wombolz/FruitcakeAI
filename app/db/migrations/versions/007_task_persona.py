"""Add tasks.persona for per-task execution persona

Revision ID: 007_task_persona
Revises: 006_task_runs
Create Date: 2026-03-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "007_task_persona"
down_revision = "006_task_runs"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column_name: str) -> bool:
    return any(c["name"] == column_name for c in inspector.get_columns(table))


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_column(inspector, "tasks", "persona"):
        op.add_column("tasks", sa.Column("persona", sa.String(length=100), nullable=True))

    # Refresh inspector after schema change.
    inspector = sa.inspect(conn)
    if not _has_index(inspector, "tasks", "ix_tasks_persona"):
        op.create_index("ix_tasks_persona", "tasks", ["persona"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_index(inspector, "tasks", "ix_tasks_persona"):
        op.drop_index("ix_tasks_persona", table_name="tasks")

    inspector = sa.inspect(conn)
    if _has_column(inspector, "tasks", "persona"):
        op.drop_column("tasks", "persona")

