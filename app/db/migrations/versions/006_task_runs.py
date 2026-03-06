"""Add task_runs table

Revision ID: 006_task_runs
Revises: 005_task_steps
Create Date: 2026-03-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "006_task_runs"
down_revision = "005_task_steps"
branch_labels = None
depends_on = None


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())

    if "task_runs" not in tables:
        op.create_table(
            "task_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="running"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
        )

    if not _has_index(inspector, "task_runs", "ix_task_runs_task_id"):
        op.create_index("ix_task_runs_task_id", "task_runs", ["task_id"])
    if not _has_index(inspector, "task_runs", "ix_task_runs_status"):
        op.create_index("ix_task_runs_status", "task_runs", ["status"])
    if not _has_index(inspector, "task_runs", "ix_task_runs_started_at"):
        op.create_index("ix_task_runs_started_at", "task_runs", ["started_at"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    if "task_runs" in tables:
        op.drop_table("task_runs")
