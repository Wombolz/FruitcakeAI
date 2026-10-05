"""add per-user model access and admin policy audit

Revision ID: 048_user_model_access
Revises: 047_user_integrations
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "048_user_model_access"
down_revision = "047_user_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_model_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("model_profile_id", sa.Integer(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_profile_id"], ["model_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "model_profile_id", name="uq_user_model_access_profile"),
    )
    op.create_index("ix_user_model_access_id", "user_model_access", ["id"])
    op.create_index("ix_user_model_access_user_id", "user_model_access", ["user_id"])
    op.create_index("ix_user_model_access_model_profile_id", "user_model_access", ["model_profile_id"])
    op.create_table(
        "admin_policy_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("target_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_policy_events_id", "admin_policy_events", ["id"])
    op.create_index("ix_admin_policy_events_actor_user_id", "admin_policy_events", ["actor_user_id"])
    op.create_index("ix_admin_policy_events_target_user_id", "admin_policy_events", ["target_user_id"])
    op.create_index("ix_admin_policy_events_action", "admin_policy_events", ["action"])
    op.create_index("ix_admin_policy_events_created_at", "admin_policy_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_policy_events_created_at", table_name="admin_policy_events")
    op.drop_index("ix_admin_policy_events_action", table_name="admin_policy_events")
    op.drop_index("ix_admin_policy_events_target_user_id", table_name="admin_policy_events")
    op.drop_index("ix_admin_policy_events_actor_user_id", table_name="admin_policy_events")
    op.drop_index("ix_admin_policy_events_id", table_name="admin_policy_events")
    op.drop_table("admin_policy_events")
    op.drop_index("ix_user_model_access_model_profile_id", table_name="user_model_access")
    op.drop_index("ix_user_model_access_user_id", table_name="user_model_access")
    op.drop_index("ix_user_model_access_id", table_name="user_model_access")
    op.drop_table("user_model_access")
