"""add host root approvals and task approval payloads

Revision ID: 039_host_root_approvals
Revises: 038_agent_model_override
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "039_host_root_approvals"
down_revision = "038_agent_model_override"
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

    if "approved_host_roots" not in existing_tables:
        op.create_table(
            "approved_host_roots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("canonical_path", sa.Text(), nullable=False),
            sa.Column("access_mode", sa.String(length=30), nullable=False, server_default="read_only"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("approval_source", sa.String(length=50), nullable=False, server_default="task_approval"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "user_id",
                "canonical_path",
                "access_mode",
                name="uq_approved_host_roots_user_path_mode",
            ),
        )

    if not _has_index(inspector, "approved_host_roots", "ix_approved_host_roots_user_id"):
        op.create_index("ix_approved_host_roots_user_id", "approved_host_roots", ["user_id"])
    if not _has_index(inspector, "approved_host_roots", "ix_approved_host_roots_created_by_user_id"):
        op.create_index(
            "ix_approved_host_roots_created_by_user_id",
            "approved_host_roots",
            ["created_by_user_id"],
        )

    if not _has_column(inspector, "task_steps", "waiting_approval_kind"):
        op.add_column("task_steps", sa.Column("waiting_approval_kind", sa.String(length=50), nullable=True))
    if not _has_column(inspector, "task_steps", "waiting_approval_payload_json"):
        op.add_column("task_steps", sa.Column("waiting_approval_payload_json", sa.Text(), nullable=True))
    if not _has_column(inspector, "task_runs", "waiting_approval_kind"):
        op.add_column("task_runs", sa.Column("waiting_approval_kind", sa.String(length=50), nullable=True))
    if not _has_column(inspector, "task_runs", "waiting_approval_payload_json"):
        op.add_column("task_runs", sa.Column("waiting_approval_payload_json", sa.Text(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if _has_column(inspector, "task_steps", "waiting_approval_payload_json"):
        op.drop_column("task_steps", "waiting_approval_payload_json")
    if _has_column(inspector, "task_steps", "waiting_approval_kind"):
        op.drop_column("task_steps", "waiting_approval_kind")
    if _has_column(inspector, "task_runs", "waiting_approval_payload_json"):
        op.drop_column("task_runs", "waiting_approval_payload_json")
    if _has_column(inspector, "task_runs", "waiting_approval_kind"):
        op.drop_column("task_runs", "waiting_approval_kind")

    if "approved_host_roots" in existing_tables:
        op.drop_table("approved_host_roots")
