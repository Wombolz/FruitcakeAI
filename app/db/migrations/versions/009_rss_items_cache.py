"""Add rss_items cache table for headline/history recall

Revision ID: 009_rss_items_cache
Revises: 008_rss_sources
Create Date: 2026-03-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "009_rss_items_cache"
down_revision = "008_rss_sources"
branch_labels = None
depends_on = None


def _has_table(inspector: Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "rss_items"):
        op.create_table(
            "rss_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source_id", sa.Integer(), sa.ForeignKey("rss_sources.id", ondelete="CASCADE"), nullable=False),
            sa.Column("item_uid", sa.String(length=128), nullable=False),
            sa.Column("title", sa.String(length=1000), nullable=False),
            sa.Column("link", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("source_id", "item_uid", name="uq_rss_items_source_uid"),
        )

    inspector = sa.inspect(conn)
    if not _has_index(inspector, "rss_items", "ix_rss_items_source_id"):
        op.create_index("ix_rss_items_source_id", "rss_items", ["source_id"])
    if not _has_index(inspector, "rss_items", "ix_rss_items_item_uid"):
        op.create_index("ix_rss_items_item_uid", "rss_items", ["item_uid"])
    if not _has_index(inspector, "rss_items", "ix_rss_items_published_at"):
        op.create_index("ix_rss_items_published_at", "rss_items", ["published_at"])
    if not _has_index(inspector, "rss_items", "ix_rss_items_fetched_at"):
        op.create_index("ix_rss_items_fetched_at", "rss_items", ["fetched_at"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "rss_items"):
        op.drop_table("rss_items")
