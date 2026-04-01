"""add task executor config json

Revision ID: 032_task_executor_config
Revises: 031_chat_session_sort_order
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "032_task_executor_config"
down_revision = "031_chat_session_sort_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("executor_config_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.alter_column("tasks", "executor_config_json", server_default=None)


def downgrade() -> None:
    op.drop_column("tasks", "executor_config_json")
