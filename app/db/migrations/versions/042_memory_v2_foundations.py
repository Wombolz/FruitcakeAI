"""memory v2: kind/subject_key/supersede/source/confidence on memories

Revision ID: 042_memory_v2_foundations
Revises: 041_incognito_sessions
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "042_memory_v2_foundations"
down_revision = "041_incognito_sessions"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_column(inspector, "memories", "kind"):
        op.add_column(
            "memories",
            sa.Column("kind", sa.String(length=20), nullable=False, server_default="fact"),
        )
        op.create_index("ix_memories_kind", "memories", ["kind"])
        # Backfill from the legacy cognitive taxonomy to retrieval-contract kinds.
        conn.execute(
            sa.text(
                "UPDATE memories SET kind = CASE memory_type "
                "WHEN 'procedural' THEN 'directive' "
                "WHEN 'episodic' THEN 'journal' "
                "ELSE 'fact' END"
            )
        )

    if not _has_column(inspector, "memories", "subject_key"):
        op.add_column("memories", sa.Column("subject_key", sa.String(length=200), nullable=True))
        op.create_index("ix_memories_subject_key", "memories", ["subject_key"])

    if not _has_column(inspector, "memories", "superseded_by_id"):
        op.add_column(
            "memories",
            sa.Column(
                "superseded_by_id",
                sa.Integer(),
                sa.ForeignKey("memories.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )

    if not _has_column(inspector, "memories", "source"):
        op.add_column(
            "memories",
            sa.Column("source", sa.String(length=30), nullable=False, server_default="migration"),
        )

    if not _has_column(inspector, "memories", "confidence"):
        op.add_column(
            "memories",
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.7"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for column, index in (
        ("confidence", None),
        ("source", None),
        ("superseded_by_id", None),
        ("subject_key", "ix_memories_subject_key"),
        ("kind", "ix_memories_kind"),
    ):
        if _has_column(inspector, "memories", column):
            if index:
                op.drop_index(index, table_name="memories")
            op.drop_column("memories", column)
