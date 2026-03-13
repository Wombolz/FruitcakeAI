"""Add rss_user_state table for per-user recent-list cursor

Revision ID: 010_rss_user_state
Revises: 009_rss_items_cache
Create Date: 2026-03-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "010_rss_user_state"
down_revision = "009_rss_items_cache"
branch_labels = None
depends_on = None


def _has_table(inspector: Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "rss_user_state"):
        op.create_table(
            "rss_user_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("last_list_recent_cursor_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.UniqueConstraint("user_id", name="uq_rss_user_state_user_id"),
        )

    inspector = sa.inspect(conn)
    if not _has_index(inspector, "rss_user_state", "ix_rss_user_state_user_id"):
        op.create_index("ix_rss_user_state_user_id", "rss_user_state", ["user_id"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "rss_user_state"):
        op.drop_table("rss_user_state")

