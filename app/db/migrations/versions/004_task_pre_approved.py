"""Add pre_approved column to tasks

Revision ID: 004_task_pre_approved
Revises: 003_phase5_webhook_configs
Create Date: 2026-03-05

Adds tasks.pre_approved (Boolean, default False) used by the approval
flow to skip the approval gate when a task is manually re-queued after
the user approves it via PATCH /tasks/{id}.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "004_task_pre_approved"
down_revision = "003_phase5_webhook_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_cols = {c["name"] for c in inspector.get_columns("tasks")}

    if "pre_approved" not in existing_cols:
        op.add_column(
            "tasks",
            sa.Column(
                "pre_approved",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_cols = {c["name"] for c in inspector.get_columns("tasks")}

    if "pre_approved" in existing_cols:
        op.drop_column("tasks", "pre_approved")
