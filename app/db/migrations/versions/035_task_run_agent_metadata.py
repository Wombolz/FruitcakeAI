"""add agent metadata to task runs

Revision ID: 035_task_run_agent_metadata
Revises: 034_task_recipe_metadata
Create Date: 2026-04-07
"""

from alembic import op
import sqlalchemy as sa


revision = "035_task_run_agent_metadata"
down_revision = "034_task_recipe_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column("run_kind", sa.String(length=30), nullable=False, server_default="task"),
    )
    op.add_column("task_runs", sa.Column("agent_role", sa.String(length=100), nullable=True))
    op.add_column("task_runs", sa.Column("trigger_source", sa.String(length=100), nullable=True))
    op.add_column("task_runs", sa.Column("source_context_json", sa.Text(), nullable=True))
    op.create_index("ix_task_runs_run_kind", "task_runs", ["run_kind"])
    op.create_index("ix_task_runs_agent_role", "task_runs", ["agent_role"])
    op.alter_column("task_runs", "run_kind", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_task_runs_agent_role", table_name="task_runs")
    op.drop_index("ix_task_runs_run_kind", table_name="task_runs")
    op.drop_column("task_runs", "source_context_json")
    op.drop_column("task_runs", "trigger_source")
    op.drop_column("task_runs", "agent_role")
    op.drop_column("task_runs", "run_kind")
