"""user chat routing preference

Revision ID: 024_user_chat_routing_preference
Revises: 023_memory_proposals
Create Date: 2026-03-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "024_user_chat_routing_preference"
down_revision = "023_memory_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}

    if "chat_routing_preference" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "chat_routing_preference",
                sa.String(length=20),
                nullable=False,
                server_default="auto",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "chat_routing_preference" in columns:
        op.drop_column("users", "chat_routing_preference")
