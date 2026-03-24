"""llm usage events

Revision ID: 021_llm_usage_events
Revises: 020_graph_obs_active
Create Date: 2026-03-23 20:10:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "021_llm_usage_events"
down_revision = "020_graph_obs_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("llm_usage_events"):
        op.create_table(
            "llm_usage_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("task_run_id", sa.Integer(), nullable=True),
            sa.Column("source", sa.String(length=50), nullable=False),
            sa.Column("stage", sa.String(length=80), nullable=True),
            sa.Column("model", sa.String(length=120), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = {index["name"] for index in inspector.get_indexes("llm_usage_events")}
    desired_indexes = [
        (op.f("ix_llm_usage_events_id"), ["id"]),
        (op.f("ix_llm_usage_events_user_id"), ["user_id"]),
        (op.f("ix_llm_usage_events_session_id"), ["session_id"]),
        (op.f("ix_llm_usage_events_task_id"), ["task_id"]),
        (op.f("ix_llm_usage_events_task_run_id"), ["task_run_id"]),
        (op.f("ix_llm_usage_events_source"), ["source"]),
        (op.f("ix_llm_usage_events_stage"), ["stage"]),
        (op.f("ix_llm_usage_events_created_at"), ["created_at"]),
    ]
    for index_name, columns in desired_indexes:
        if index_name not in existing_indexes:
            op.create_index(index_name, "llm_usage_events", columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("llm_usage_events"):
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("llm_usage_events")}
    for index_name in (
        op.f("ix_llm_usage_events_created_at"),
        op.f("ix_llm_usage_events_stage"),
        op.f("ix_llm_usage_events_source"),
        op.f("ix_llm_usage_events_task_run_id"),
        op.f("ix_llm_usage_events_task_id"),
        op.f("ix_llm_usage_events_session_id"),
        op.f("ix_llm_usage_events_user_id"),
        op.f("ix_llm_usage_events_id"),
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="llm_usage_events")
    op.drop_table("llm_usage_events")
