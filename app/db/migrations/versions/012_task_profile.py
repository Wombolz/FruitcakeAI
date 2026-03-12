"""Add tasks.profile column for profile-driven execution

Revision ID: 012_task_profile
Revises: 011_task_run_artifacts
Create Date: 2026-03-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "012_task_profile"
down_revision = "011_task_run_artifacts"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table_name: str, column_name: str) -> bool:
    return any(c["name"] == column_name for c in inspector.get_columns(table_name))


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_column(inspector, "tasks", "profile"):
        op.add_column("tasks", sa.Column("profile", sa.String(length=50), nullable=True))

    inspector = sa.inspect(conn)
    if not _has_index(inspector, "tasks", "ix_tasks_profile"):
        op.create_index("ix_tasks_profile", "tasks", ["profile"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_index(inspector, "tasks", "ix_tasks_profile"):
        op.drop_index("ix_tasks_profile", table_name="tasks")
    if _has_column(inspector, "tasks", "profile"):
        op.drop_column("tasks", "profile")
