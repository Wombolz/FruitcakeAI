"""Phase 4: tasks, memories, device_tokens tables; user active_hours; session is_task_session

Revision ID: 001_phase4
Revises:
Create Date: 2026-03-03

New tables: memories, tasks, device_tokens
New columns:
  users.active_hours_start, active_hours_end, active_hours_tz
  chat_sessions.is_task_session

Idempotent: safe to run even if tables already exist (create_all may have
created them before the first Alembic run).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector

# revision identifiers
revision = "001_phase4"
down_revision = None
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # ── memories ──────────────────────────────────────────────────────────────
    if "memories" not in existing_tables:
        op.create_table(
            "memories",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("memory_type", sa.String(20), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tags", sa.Text(), server_default="[]"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.text("now()")),
        )

    # Indexes — skip if already present
    if not _has_index(inspector, "memories", "ix_memories_user_id"):
        op.create_index("ix_memories_user_id", "memories", ["user_id"])
    if not _has_index(inspector, "memories", "ix_memories_memory_type"):
        op.create_index("ix_memories_memory_type", "memories", ["memory_type"])
    if not _has_index(inspector, "memories", "ix_memories_is_active"):
        op.create_index("ix_memories_is_active", "memories", ["is_active"])

    # pgvector embedding column (PostgreSQL only; ALTER TABLE IF NOT EXISTS handles
    # the case where create_all already added it via the Vector() column definition)
    if conn.dialect.name == "postgresql":
        try:
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(
                sa.text(
                    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS "
                    "embedding vector(384)"
                )
            )
        except Exception:
            pass  # pgvector not installed — embedding stays NULL until available

    # ── tasks ─────────────────────────────────────────────────────────────────
    if "tasks" not in existing_tables:
        op.create_table(
            "tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("task_type", sa.String(20), nullable=False, server_default="one_shot"),
            sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("schedule", sa.String(100), nullable=True),
            sa.Column("deliver", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("result", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("active_hours_start", sa.String(5), nullable=True),
            sa.Column("active_hours_end", sa.String(5), nullable=True),
            sa.Column("active_hours_tz", sa.String(50), nullable=True),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_index(inspector, "tasks", "ix_tasks_user_id"):
        op.create_index("ix_tasks_user_id", "tasks", ["user_id"])
    if not _has_index(inspector, "tasks", "ix_tasks_status"):
        op.create_index("ix_tasks_status", "tasks", ["status"])
    if not _has_index(inspector, "tasks", "ix_tasks_next_run_at"):
        op.create_index("ix_tasks_next_run_at", "tasks", ["next_run_at"])

    # ── device_tokens ─────────────────────────────────────────────────────────
    if "device_tokens" not in existing_tables:
        op.create_table(
            "device_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("token", sa.String(255), nullable=False, unique=True),
            sa.Column("environment", sa.String(20), nullable=False, server_default="sandbox"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )

    if not _has_index(inspector, "device_tokens", "ix_device_tokens_user_id"):
        op.create_index("ix_device_tokens_user_id", "device_tokens", ["user_id"])
    if not _has_index(inspector, "device_tokens", "ix_device_tokens_token"):
        op.create_index("ix_device_tokens_token", "device_tokens", ["token"], unique=True)

    # ── users: active hours columns ───────────────────────────────────────────
    if not _has_column(inspector, "users", "active_hours_start"):
        op.add_column("users", sa.Column("active_hours_start", sa.String(5), nullable=True))
    if not _has_column(inspector, "users", "active_hours_end"):
        op.add_column("users", sa.Column("active_hours_end", sa.String(5), nullable=True))
    if not _has_column(inspector, "users", "active_hours_tz"):
        op.add_column("users", sa.Column("active_hours_tz", sa.String(50), nullable=True))

    # ── chat_sessions: task session flag ──────────────────────────────────────
    if not _has_column(inspector, "chat_sessions", "is_task_session"):
        op.add_column(
            "chat_sessions",
            sa.Column("is_task_session", sa.Boolean(), nullable=False, server_default="false"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if _has_column(inspector, "chat_sessions", "is_task_session"):
        op.drop_column("chat_sessions", "is_task_session")
    if _has_column(inspector, "users", "active_hours_tz"):
        op.drop_column("users", "active_hours_tz")
    if _has_column(inspector, "users", "active_hours_end"):
        op.drop_column("users", "active_hours_end")
    if _has_column(inspector, "users", "active_hours_start"):
        op.drop_column("users", "active_hours_start")
    if "device_tokens" in existing_tables:
        op.drop_table("device_tokens")
    if "tasks" in existing_tables:
        op.drop_table("tasks")
    if "memories" in existing_tables:
        op.drop_table("memories")
