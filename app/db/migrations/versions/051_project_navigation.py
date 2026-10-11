"""add user projects and chat organization

Revision ID: 051_project_navigation
Revises: 050_session_memory_extracted
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "051_project_navigation"
down_revision = "050_session_memory_extracted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Early testers ran this migration before it was resequenced behind the
    # memory-extraction migration. Reconcile that preview schema instead of
    # failing when its table or columns already exist.
    if "user_projects" not in inspector.get_table_names():
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

    inspector = sa.inspect(bind)
    project_indexes = {index["name"] for index in inspector.get_indexes("user_projects")}
    expected_project_indexes = (
        ("ix_user_projects_id", ["id"], False),
        ("ix_user_projects_public_id", ["public_id"], True),
        ("ix_user_projects_user_id", ["user_id"], False),
        ("ix_user_projects_sort_index", ["sort_index"], False),
        ("ix_user_projects_user_sort", ["user_id", "sort_index", "id"], False),
    )
    for name, columns, unique in expected_project_indexes:
        if name not in project_indexes:
            op.create_index(name, "user_projects", columns, unique=unique)

    session_columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "project_id" not in session_columns:
        op.add_column("chat_sessions", sa.Column("project_id", sa.Integer(), nullable=True))
    if "is_pinned" not in session_columns:
        op.add_column(
            "chat_sessions",
            sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )

    inspector = sa.inspect(bind)
    foreign_keys = {foreign_key.get("name") for foreign_key in inspector.get_foreign_keys("chat_sessions")}
    if "fk_chat_sessions_project_id_user_projects" not in foreign_keys:
        op.create_foreign_key(
            "fk_chat_sessions_project_id_user_projects",
            "chat_sessions",
            "user_projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )
    session_indexes = {index["name"] for index in inspector.get_indexes("chat_sessions")}
    if "ix_chat_sessions_project_id" not in session_indexes:
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
