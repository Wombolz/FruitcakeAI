"""Phase 5: add webhook_configs table

Revision ID: 003_phase5_webhook_configs
Revises: 002_task_last_session_id
Create Date: 2026-03-05

New table: webhook_configs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector

# revision identifiers
revision = "003_phase5_webhook_configs"
down_revision = "002_task_last_session_id"
branch_labels = None
depends_on = None


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if "webhook_configs" not in existing_tables:
        op.create_table(
            "webhook_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("webhook_key", sa.String(255), nullable=False, unique=True),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )

    if not _has_index(inspector, "webhook_configs", "ix_webhook_configs_user_id"):
        op.create_index("ix_webhook_configs_user_id", "webhook_configs", ["user_id"])
    if not _has_index(inspector, "webhook_configs", "ix_webhook_configs_webhook_key"):
        op.create_index(
            "ix_webhook_configs_webhook_key",
            "webhook_configs",
            ["webhook_key"],
            unique=True,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())
    if "webhook_configs" in existing_tables:
        op.drop_table("webhook_configs")
