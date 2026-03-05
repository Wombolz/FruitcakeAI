"""Add task_steps and task planning columns

Revision ID: 005_task_steps
Revises: 004_task_pre_approved
Create Date: 2026-03-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "005_task_steps"
down_revision = "004_task_pre_approved"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if "task_steps" not in existing_tables:
        op.create_table(
            "task_steps",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("step_index", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("tool_allowlist", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("tool_blocklist", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("output_summary", sa.Text(), nullable=True),
            sa.Column("result", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("waiting_approval_tool", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.text("now()")),
            sa.UniqueConstraint("task_id", "step_index", name="uq_task_steps_task_id_step_index"),
        )

    if not _has_index(inspector, "task_steps", "ix_task_steps_task_id"):
        op.create_index("ix_task_steps_task_id", "task_steps", ["task_id"])
    if not _has_index(inspector, "task_steps", "ix_task_steps_step_index"):
        op.create_index("ix_task_steps_step_index", "task_steps", ["step_index"])
    if not _has_index(inspector, "task_steps", "ix_task_steps_status"):
        op.create_index("ix_task_steps_status", "task_steps", ["status"])

    if not _has_column(inspector, "tasks", "current_step_index"):
        op.add_column("tasks", sa.Column("current_step_index", sa.Integer(), nullable=True))
    if not _has_column(inspector, "tasks", "has_plan"):
        op.add_column(
            "tasks",
            sa.Column("has_plan", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if not _has_column(inspector, "tasks", "plan_version"):
        op.add_column(
            "tasks",
            sa.Column("plan_version", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if _has_column(inspector, "tasks", "plan_version"):
        op.drop_column("tasks", "plan_version")
    if _has_column(inspector, "tasks", "has_plan"):
        op.drop_column("tasks", "has_plan")
    if _has_column(inspector, "tasks", "current_step_index"):
        op.drop_column("tasks", "current_step_index")

    if "task_steps" in existing_tables:
        op.drop_table("task_steps")
