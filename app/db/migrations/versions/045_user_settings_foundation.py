"""add stable public user identity and assistant preferences

Revision ID: 045_user_settings_foundation
Revises: 044_chat_run_traces
Create Date: 2026-10-04
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op


revision = "045_user_settings_foundation"
down_revision = "044_chat_run_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("public_id", sa.String(length=36), nullable=True))

    users = sa.table(
        "users",
        sa.column("id", sa.Integer()),
        sa.column("public_id", sa.String(length=36)),
    )
    connection = op.get_bind()
    for user_id in connection.execute(sa.select(users.c.id)).scalars():
        connection.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(public_id=str(uuid.uuid4()))
        )

    op.alter_column("users", "public_id", existing_type=sa.String(length=36), nullable=False)
    op.create_index("ix_users_public_id", "users", ["public_id"], unique=True)

    op.create_table(
        "user_assistant_preferences",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("delivery_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("appearance", sa.String(length=20), nullable=False, server_default="system"),
        sa.Column("reduce_motion", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_assistant_preferences")
    op.drop_index("ix_users_public_id", table_name="users")
    op.drop_column("users", "public_id")
