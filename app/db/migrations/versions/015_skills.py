"""Add admin-managed skills table

Revision ID: 015_skills
Revises: 014_rename_child_restricted
Create Date: 2026-03-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "015_skills"
down_revision = "014_rename_child_restricted"
branch_labels = None
depends_on = None


def _has_table(inspector: Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_index(inspector: Inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "skills"):
        op.create_table(
            "skills",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slug", sa.String(length=100), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("system_prompt_addition", sa.Text(), nullable=False),
            sa.Column("allowed_tool_additions", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("scope", sa.String(length=20), nullable=False, server_default="shared"),
            sa.Column("personal_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
            sa.Column("installed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("description_embedding", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("personal_user_id", "slug", name="uq_skills_personal_user_slug"),
        )

    inspector = sa.inspect(conn)
    for index_name, columns in (
        ("ix_skills_slug", ["slug"]),
        ("ix_skills_scope", ["scope"]),
        ("ix_skills_is_active", ["is_active"]),
        ("ix_skills_is_pinned", ["is_pinned"]),
        ("ix_skills_personal_user_id", ["personal_user_id"]),
        ("ix_skills_installed_by", ["installed_by"]),
    ):
        if not _has_index(inspector, "skills", index_name):
            op.create_index(index_name, "skills", columns)

    inspector = sa.inspect(conn)
    if not _has_index(inspector, "skills", "uq_skills_shared_slug"):
        op.create_index(
            "uq_skills_shared_slug",
            "skills",
            ["slug"],
            unique=True,
            sqlite_where=sa.text("personal_user_id IS NULL"),
            postgresql_where=sa.text("personal_user_id IS NULL"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "skills"):
        for index_name in (
            "uq_skills_shared_slug",
            "ix_skills_installed_by",
            "ix_skills_personal_user_id",
            "ix_skills_is_pinned",
            "ix_skills_is_active",
            "ix_skills_scope",
            "ix_skills_slug",
        ):
            if _has_index(inspector, "skills", index_name):
                op.drop_index(index_name, table_name="skills")
                inspector = sa.inspect(conn)
        op.drop_table("skills")
