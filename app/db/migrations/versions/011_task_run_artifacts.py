"""Add task_run_artifacts table for magazine/task run structured artifacts

Revision ID: 011_task_run_artifacts
Revises: 010_rss_user_state
Create Date: 2026-03-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "011_task_run_artifacts"
down_revision = "010_rss_user_state"
branch_labels = None
depends_on = None


def _has_table(inspector: Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "task_run_artifacts"):
        op.create_table(
            "task_run_artifacts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_run_id", sa.Integer(), sa.ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("artifact_type", sa.String(length=50), nullable=False),
            sa.Column("content_json", sa.Text(), nullable=True),
            sa.Column("content_text", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    inspector = sa.inspect(conn)
    if not _has_index(inspector, "task_run_artifacts", "ix_task_run_artifacts_task_run_id"):
        op.create_index("ix_task_run_artifacts_task_run_id", "task_run_artifacts", ["task_run_id"])
    if not _has_index(inspector, "task_run_artifacts", "ix_task_run_artifacts_artifact_type"):
        op.create_index("ix_task_run_artifacts_artifact_type", "task_run_artifacts", ["artifact_type"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "task_run_artifacts"):
        op.drop_table("task_run_artifacts")
