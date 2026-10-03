"""add chat run traces and usage correlation

Revision ID: 044_chat_run_traces
Revises: 043_chat_runs
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "044_chat_run_traces"
down_revision = "043_chat_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_run_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("phase", sa.String(length=50), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["chat_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_chat_run_events_run_sequence"),
    )
    op.create_index("ix_chat_run_events_id", "chat_run_events", ["id"])
    op.create_index("ix_chat_run_events_run_id", "chat_run_events", ["run_id"])
    op.create_index("ix_chat_run_events_event_type", "chat_run_events", ["event_type"])

    op.add_column("llm_usage_events", sa.Column("chat_run_id", sa.String(length=64), nullable=True))
    op.add_column(
        "llm_usage_events",
        sa.Column("cached_prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("llm_usage_events", sa.Column("total_duration_ms", sa.Float(), nullable=True))
    op.add_column("llm_usage_events", sa.Column("load_duration_ms", sa.Float(), nullable=True))
    op.add_column("llm_usage_events", sa.Column("prompt_eval_duration_ms", sa.Float(), nullable=True))
    op.add_column("llm_usage_events", sa.Column("eval_duration_ms", sa.Float(), nullable=True))
    op.create_foreign_key(
        "fk_llm_usage_events_chat_run_id",
        "llm_usage_events",
        "chat_runs",
        ["chat_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_llm_usage_events_chat_run_id", "llm_usage_events", ["chat_run_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_events_chat_run_id", table_name="llm_usage_events")
    op.drop_constraint("fk_llm_usage_events_chat_run_id", "llm_usage_events", type_="foreignkey")
    op.drop_column("llm_usage_events", "eval_duration_ms")
    op.drop_column("llm_usage_events", "prompt_eval_duration_ms")
    op.drop_column("llm_usage_events", "load_duration_ms")
    op.drop_column("llm_usage_events", "total_duration_ms")
    op.drop_column("llm_usage_events", "cached_prompt_tokens")
    op.drop_column("llm_usage_events", "chat_run_id")

    op.drop_index("ix_chat_run_events_event_type", table_name="chat_run_events")
    op.drop_index("ix_chat_run_events_run_id", table_name="chat_run_events")
    op.drop_index("ix_chat_run_events_id", table_name="chat_run_events")
    op.drop_table("chat_run_events")
