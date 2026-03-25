"""memory proposals review queue

Revision ID: 023_memory_proposals
Revises: 022_rss_pub_items
Create Date: 2026-03-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "023_memory_proposals"
down_revision = "022_rss_pub_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("memory_proposals"):
        op.create_table(
            "memory_proposals",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("proposal_key", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("proposal_type", sa.String(length=50), nullable=False),
            sa.Column("source_type", sa.String(length=50), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
            sa.Column("task_run_id", sa.Integer(), sa.ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("proposal_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("approved_memory_id", sa.Integer(), sa.ForeignKey("memories.id", ondelete="SET NULL"), nullable=True),
            sa.Column("resolved_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        inspector = inspect(bind)

    existing_indexes = {index["name"] for index in inspector.get_indexes("memory_proposals")}
    for index_name, columns, unique in [
        ("ix_memory_proposals_proposal_key", ["proposal_key"], True),
        ("ix_memory_proposals_user_id", ["user_id"], False),
        ("ix_memory_proposals_proposal_type", ["proposal_type"], False),
        ("ix_memory_proposals_source_type", ["source_type"], False),
        ("ix_memory_proposals_status", ["status"], False),
        ("ix_memory_proposals_task_id", ["task_id"], False),
        ("ix_memory_proposals_task_run_id", ["task_run_id"], False),
        ("ix_memory_proposals_approved_memory_id", ["approved_memory_id"], False),
        ("ix_memory_proposals_resolved_by_user_id", ["resolved_by_user_id"], False),
    ]:
        if index_name not in existing_indexes:
            op.create_index(index_name, "memory_proposals", columns, unique=unique)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("memory_proposals"):
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("memory_proposals")}
    for index_name in [
        "ix_memory_proposals_resolved_by_user_id",
        "ix_memory_proposals_approved_memory_id",
        "ix_memory_proposals_task_run_id",
        "ix_memory_proposals_task_id",
        "ix_memory_proposals_status",
        "ix_memory_proposals_source_type",
        "ix_memory_proposals_proposal_type",
        "ix_memory_proposals_user_id",
        "ix_memory_proposals_proposal_key",
    ]:
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="memory_proposals")

    op.drop_table("memory_proposals")
