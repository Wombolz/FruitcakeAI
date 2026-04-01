"""secret access audit events

Revision ID: 033_secret_access_events
Revises: 032_task_executor_config
Create Date: 2026-04-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "033_secret_access_events"
down_revision = "032_task_executor_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "secret_access_events" in tables:
        return
    op.create_table(
        "secret_access_events",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("secret_id", sa.Integer(), sa.ForeignKey("secrets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("secret_name", sa.String(length=100), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_class", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_secret_access_events_id", "secret_access_events", ["id"])
    op.create_index("ix_secret_access_events_secret_id", "secret_access_events", ["secret_id"])
    op.create_index("ix_secret_access_events_user_id", "secret_access_events", ["user_id"])
    op.create_index("ix_secret_access_events_task_id", "secret_access_events", ["task_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "secret_access_events" not in tables:
        return
    op.drop_index("ix_secret_access_events_task_id", table_name="secret_access_events")
    op.drop_index("ix_secret_access_events_user_id", table_name="secret_access_events")
    op.drop_index("ix_secret_access_events_secret_id", table_name="secret_access_events")
    op.drop_index("ix_secret_access_events_id", table_name="secret_access_events")
    op.drop_table("secret_access_events")
