"""Add document processing metadata columns

Revision ID: 017_document_processing_columns
Revises: 016_skills_lifecycle
Create Date: 2026-03-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "017_document_processing_columns"
down_revision = "016_skills_lifecycle"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table_name: str, column_name: str) -> bool:
    return any(c["name"] == column_name for c in inspector.get_columns(table_name))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = (
        ("content_type", sa.String(length=50)),
        ("extraction_method", sa.String(length=50)),
        ("extracted_text_length", sa.Integer()),
        ("chunk_count", sa.Integer()),
        ("processing_started_at", sa.DateTime(timezone=True)),
        ("processing_completed_at", sa.DateTime(timezone=True)),
    )
    for name, coltype in columns:
        if not _has_column(inspector, "documents", name):
            op.add_column("documents", sa.Column(name, coltype, nullable=True))
            inspector = sa.inspect(conn)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for name in (
        "processing_completed_at",
        "processing_started_at",
        "chunk_count",
        "extracted_text_length",
        "extraction_method",
        "content_type",
    ):
        if _has_column(inspector, "documents", name):
            op.drop_column("documents", name)
            inspector = sa.inspect(conn)
