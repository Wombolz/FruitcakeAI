"""add user projects and chat organization

Revision ID: 050_project_navigation
Revises: 049_model_context_budgets
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "050_project_navigation"
down_revision = "049_model_context_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("icon", sa.String(length=100), nullable=True),
        sa.Column("sort_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_projects_id", "user_projects", ["id"])
    op.create_index("ix_user_projects_public_id", "user_projects", ["public_id"], unique=True)
    op.create_index("ix_user_projects_user_id", "user_projects", ["user_id"])
    op.create_index("ix_user_projects_sort_index", "user_projects", ["sort_index"])
    op.create_index(
        "ix_user_projects_user_sort",
        "user_projects",
        ["user_id", "sort_index", "id"],
    )

    op.add_column("chat_sessions", sa.Column("project_id", sa.Integer(), nullable=True))
    op.add_column(
        "chat_sessions",
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_foreign_key(
        "fk_chat_sessions_project_id_user_projects",
        "chat_sessions",
        "user_projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_chat_sessions_project_id", "chat_sessions", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_project_id", table_name="chat_sessions")
    op.drop_constraint(
        "fk_chat_sessions_project_id_user_projects",
        "chat_sessions",
        type_="foreignkey",
    )
    op.drop_column("chat_sessions", "is_pinned")
    op.drop_column("chat_sessions", "project_id")
    op.drop_index("ix_user_projects_user_sort", table_name="user_projects")
    op.drop_index("ix_user_projects_sort_index", table_name="user_projects")
    op.drop_index("ix_user_projects_user_id", table_name="user_projects")
    op.drop_index("ix_user_projects_public_id", table_name="user_projects")
    op.drop_index("ix_user_projects_id", table_name="user_projects")
    op.drop_table("user_projects")
