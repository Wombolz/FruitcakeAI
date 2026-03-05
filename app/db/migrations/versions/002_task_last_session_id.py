"""Add Task.last_session_id for task-run debug endpoint

Revision ID: 002_task_last_session_id
Revises: 001_phase4
Create Date: 2026-03-04

Adds tasks.last_session_id FK → chat_sessions.id so the /admin/task-runs
endpoint can join AuditLog entries for each task run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector

revision = "002_task_last_session_id"
down_revision = "001_phase4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {c["name"] for c in inspector.get_columns("tasks")}

    if "last_session_id" not in existing:
        op.add_column(
            "tasks",
            sa.Column(
                "last_session_id",
                sa.Integer(),
                sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {c["name"] for c in inspector.get_columns("tasks")}
    if "last_session_id" in existing:
        op.drop_column("tasks", "last_session_id")
