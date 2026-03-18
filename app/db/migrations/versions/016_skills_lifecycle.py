"""Harden skills lifecycle and attribution support

Revision ID: 016_skills_lifecycle
Revises: 015_skills
Create Date: 2026-03-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Inspector


revision = "016_skills_lifecycle"
down_revision = "015_skills"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table_name: str, column_name: str) -> bool:
    return any(c["name"] == column_name for c in inspector.get_columns(table_name))


def _has_index(inspector: Inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def _has_unique(inspector: Inspector, table_name: str, constraint_name: str) -> bool:
    return any(u["name"] == constraint_name for u in inspector.get_unique_constraints(table_name))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_column(inspector, "skills", "supersedes_skill_id"):
        op.add_column(
            "skills",
            sa.Column("supersedes_skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="SET NULL"), nullable=True),
        )
    inspector = sa.inspect(conn)
    if not _has_index(inspector, "skills", "ix_skills_supersedes_skill_id"):
        op.create_index("ix_skills_supersedes_skill_id", "skills", ["supersedes_skill_id"])

    if _has_unique(inspector, "skills", "uq_skills_personal_user_slug"):
        op.drop_constraint("uq_skills_personal_user_slug", "skills", type_="unique")

    inspector = sa.inspect(conn)
    if _has_index(inspector, "skills", "uq_skills_shared_slug"):
        op.drop_index("uq_skills_shared_slug", table_name="skills")

    inspector = sa.inspect(conn)
    if not _has_index(inspector, "skills", "uq_skills_active_shared_slug"):
        op.create_index(
            "uq_skills_active_shared_slug",
            "skills",
            ["slug"],
            unique=True,
            sqlite_where=sa.text("personal_user_id IS NULL AND is_active = 1"),
            postgresql_where=sa.text("personal_user_id IS NULL AND is_active = true"),
        )
    inspector = sa.inspect(conn)
    if not _has_index(inspector, "skills", "uq_skills_active_personal_user_slug"):
        op.create_index(
            "uq_skills_active_personal_user_slug",
            "skills",
            ["personal_user_id", "slug"],
            unique=True,
            sqlite_where=sa.text("personal_user_id IS NOT NULL AND is_active = 1"),
            postgresql_where=sa.text("personal_user_id IS NOT NULL AND is_active = true"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_index(inspector, "skills", "uq_skills_active_personal_user_slug"):
        op.drop_index("uq_skills_active_personal_user_slug", table_name="skills")
    inspector = sa.inspect(conn)
    if _has_index(inspector, "skills", "uq_skills_active_shared_slug"):
        op.drop_index("uq_skills_active_shared_slug", table_name="skills")

    inspector = sa.inspect(conn)
    if not _has_unique(inspector, "skills", "uq_skills_personal_user_slug"):
        op.create_unique_constraint("uq_skills_personal_user_slug", "skills", ["personal_user_id", "slug"])
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

    inspector = sa.inspect(conn)
    if _has_index(inspector, "skills", "ix_skills_supersedes_skill_id"):
        op.drop_index("ix_skills_supersedes_skill_id", table_name="skills")
    inspector = sa.inspect(conn)
    if _has_column(inspector, "skills", "supersedes_skill_id"):
        op.drop_column("skills", "supersedes_skill_id")
