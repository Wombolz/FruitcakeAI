"""Add RSS source catalog and discovery candidate tables

Revision ID: 008_rss_sources
Revises: 007_task_persona
Create Date: 2026-03-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "008_rss_sources"
down_revision = "007_task_persona"
branch_labels = None
depends_on = None


def _has_table(inspector: Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "rss_sources"):
        op.create_table(
            "rss_sources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("url_canonical", sa.Text(), nullable=False),
            sa.Column("category", sa.String(length=100), nullable=False, server_default="news"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("trust_level", sa.String(length=30), nullable=False, server_default="manual"),
            sa.Column("update_interval_minutes", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "url_canonical", name="uq_rss_sources_user_url"),
        )

    if not _has_table(inspector, "rss_source_candidates"):
        op.create_table(
            "rss_source_candidates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("seed_url", sa.Text(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("url_canonical", sa.Text(), nullable=False),
            sa.Column("title_hint", sa.String(length=255), nullable=True),
            sa.Column("domain", sa.String(length=255), nullable=False),
            sa.Column("discovered_via", sa.String(length=100), nullable=False, server_default="discover_rss_sources"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    inspector = sa.inspect(conn)
    if not _has_index(inspector, "rss_sources", "ix_rss_sources_user_id"):
        op.create_index("ix_rss_sources_user_id", "rss_sources", ["user_id"])
    if not _has_index(inspector, "rss_sources", "ix_rss_sources_url_canonical"):
        op.create_index("ix_rss_sources_url_canonical", "rss_sources", ["url_canonical"])
    if not _has_index(inspector, "rss_sources", "ix_rss_sources_active"):
        op.create_index("ix_rss_sources_active", "rss_sources", ["active"])

    if not _has_index(inspector, "rss_source_candidates", "ix_rss_source_candidates_user_id"):
        op.create_index("ix_rss_source_candidates_user_id", "rss_source_candidates", ["user_id"])
    if not _has_index(inspector, "rss_source_candidates", "ix_rss_source_candidates_status"):
        op.create_index("ix_rss_source_candidates_status", "rss_source_candidates", ["status"])
    if not _has_index(inspector, "rss_source_candidates", "ix_rss_source_candidates_domain"):
        op.create_index("ix_rss_source_candidates_domain", "rss_source_candidates", ["domain"])
    if not _has_index(inspector, "rss_source_candidates", "ix_rss_source_candidates_url_canonical"):
        op.create_index("ix_rss_source_candidates_url_canonical", "rss_source_candidates", ["url_canonical"])

    # Global-source uniqueness for rows where user_id IS NULL.
    dialect = conn.dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_rss_sources_global_url
            ON rss_sources (url_canonical)
            WHERE user_id IS NULL
            """
        )
    elif dialect == "sqlite":
        # SQLite supports partial indexes in modern versions.
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_rss_sources_global_url
            ON rss_sources (url_canonical)
            WHERE user_id IS NULL
            """
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    op.execute("DROP INDEX IF EXISTS uq_rss_sources_global_url")

    if _has_table(inspector, "rss_source_candidates"):
        op.drop_table("rss_source_candidates")
    if _has_table(inspector, "rss_sources"):
        op.drop_table("rss_sources")
