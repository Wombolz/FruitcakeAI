"""track per-session memory extraction high-water mark

Existing sessions are marked as already extracted so idle extraction only
considers activity from now on (nightly extraction still covers the last
24h through its own window).

Revision ID: 050_session_memory_extracted
Revises: 049_model_context_budgets
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "050_session_memory_extracted"
down_revision = "049_model_context_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("memory_extracted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE chat_sessions SET memory_extracted_at = now() "
        "WHERE memory_extracted_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "memory_extracted_at")
