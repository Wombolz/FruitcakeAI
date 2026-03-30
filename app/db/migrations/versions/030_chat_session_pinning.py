"""chat session ordering

Revision ID: 030_chat_session_pinning
Revises: 029_linked_source_exclusions
Create Date: 2026-03-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "030_chat_session_pinning"
down_revision = "029_linked_source_exclusions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "sort_order" not in columns:
        op.add_column(
            "chat_sessions",
            sa.Column("sort_order", sa.Integer(), nullable=True),
        )

    chat_sessions = sa.table(
        "chat_sessions",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("is_active", sa.Boolean),
        sa.column("is_task_session", sa.Boolean),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("sort_order", sa.Integer),
    )

    ordered_rows = bind.execute(
        sa.select(
            chat_sessions.c.id,
            chat_sessions.c.user_id,
        )
        .where(
            chat_sessions.c.is_active == sa.true(),
            chat_sessions.c.is_task_session == sa.false(),
        )
        .order_by(
            chat_sessions.c.user_id.asc(),
            sa.func.coalesce(chat_sessions.c.updated_at, chat_sessions.c.created_at).desc(),
            chat_sessions.c.id.desc(),
        )
    ).fetchall()

    current_user_id = None
    current_order = 0
    for row in ordered_rows:
        if row.user_id != current_user_id:
            current_user_id = row.user_id
            current_order = 0
        bind.execute(
            sa.update(chat_sessions)
            .where(chat_sessions.c.id == row.id)
            .values(sort_order=current_order)
        )
        current_order += 1


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "sort_order" in columns:
        op.drop_column("chat_sessions", "sort_order")
