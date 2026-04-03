"""add task recipe metadata

Revision ID: 034_task_recipe_metadata
Revises: 033_secret_access_events
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa


revision = "034_task_recipe_metadata"
down_revision = "033_secret_access_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("task_recipe_json", sa.Text(), nullable=False, server_default="{}"))
    op.alter_column("tasks", "task_recipe_json", server_default=None)


def downgrade() -> None:
    op.drop_column("tasks", "task_recipe_json")
