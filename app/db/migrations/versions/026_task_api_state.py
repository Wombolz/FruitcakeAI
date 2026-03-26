"""task api state

Revision ID: 026_task_api_state
Revises: 025_secrets
Create Date: 2026-03-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "026_task_api_state"
down_revision = "025_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "task_api_states" in tables:
        return

    op.create_table(
        "task_api_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state_key", sa.String(length=100), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "state_key", name="uq_task_api_states_task_key"),
    )
    op.create_index("ix_task_api_states_id", "task_api_states", ["id"])
    op.create_index("ix_task_api_states_task_id", "task_api_states", ["task_id"])
    op.create_index("ix_task_api_states_state_key", "task_api_states", ["state_key"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "task_api_states" not in tables:
        return
    op.drop_index("ix_task_api_states_state_key", table_name="task_api_states")
    op.drop_index("ix_task_api_states_task_id", table_name="task_api_states")
    op.drop_index("ix_task_api_states_id", table_name="task_api_states")
    op.drop_table("task_api_states")
