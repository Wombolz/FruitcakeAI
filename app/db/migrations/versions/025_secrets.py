"""user-owned encrypted secrets

Revision ID: 025_secrets
Revises: 024_user_chat_routing_preference
Create Date: 2026-03-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "025_secrets"
down_revision = "024_user_chat_routing_preference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "secrets" in tables:
        return

    op.create_table(
        "secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "name", name="uq_secrets_user_name"),
    )
    op.create_index("ix_secrets_id", "secrets", ["id"])
    op.create_index("ix_secrets_user_id", "secrets", ["user_id"])
    op.create_index("ix_secrets_name", "secrets", ["name"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "secrets" not in tables:
        return
    op.drop_index("ix_secrets_name", table_name="secrets")
    op.drop_index("ix_secrets_user_id", table_name="secrets")
    op.drop_index("ix_secrets_id", table_name="secrets")
    op.drop_table("secrets")
