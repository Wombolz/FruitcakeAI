"""linked source exclusions and skip counts

Revision ID: 029_linked_source_exclusions
Revises: 028_linked_sources
Create Date: 2026-03-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "029_linked_source_exclusions"
down_revision = "028_linked_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("linked_sources")}
    if "excluded_paths" not in columns:
        op.add_column(
            "linked_sources",
            sa.Column("excluded_paths", sa.Text(), nullable=False, server_default="[]"),
        )
    if "skipped_empty_count" not in columns:
        op.add_column(
            "linked_sources",
            sa.Column("skipped_empty_count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("linked_sources")}
    if "skipped_empty_count" in columns:
        op.drop_column("linked_sources", "skipped_empty_count")
    if "excluded_paths" in columns:
        op.drop_column("linked_sources", "excluded_paths")
