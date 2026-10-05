"""add model profiles and user model preferences

Revision ID: 046_model_profiles
Revises: 045_user_settings_foundation
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "046_model_profiles"
down_revision = "045_user_settings_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("provider_family", sa.String(length=50), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_local", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("supports_text", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("supports_tools", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("supports_thinking", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("supports_native_streaming", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reasoning_efforts_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("default_reasoning_effort", sa.String(length=20), nullable=True),
        sa.Column("tool_mode", sa.String(length=20), nullable=False, server_default="enabled"),
        sa.Column("allowed_tools_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("blocked_tools_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("keep_alive", sa.String(length=30), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_profiles_id", "model_profiles", ["id"])
    op.create_index("ix_model_profiles_public_id", "model_profiles", ["public_id"], unique=True)
    op.create_index("ix_model_profiles_model_id", "model_profiles", ["model_id"], unique=True)

    op.add_column("user_assistant_preferences", sa.Column("preferred_model_profile_id", sa.Integer(), nullable=True))
    op.add_column("user_assistant_preferences", sa.Column("preferred_vision_model_profile_id", sa.Integer(), nullable=True))
    op.add_column("user_assistant_preferences", sa.Column("preferred_reasoning_effort", sa.String(length=20), nullable=True))
    op.create_foreign_key(
        "fk_user_preferences_model_profile",
        "user_assistant_preferences",
        "model_profiles",
        ["preferred_model_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_user_preferences_vision_model_profile",
        "user_assistant_preferences",
        "model_profiles",
        ["preferred_vision_model_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_user_preferences_vision_model_profile", "user_assistant_preferences", type_="foreignkey")
    op.drop_constraint("fk_user_preferences_model_profile", "user_assistant_preferences", type_="foreignkey")
    op.drop_column("user_assistant_preferences", "preferred_reasoning_effort")
    op.drop_column("user_assistant_preferences", "preferred_vision_model_profile_id")
    op.drop_column("user_assistant_preferences", "preferred_model_profile_id")
    op.drop_index("ix_model_profiles_model_id", table_name="model_profiles")
    op.drop_index("ix_model_profiles_public_id", table_name="model_profiles")
    op.drop_index("ix_model_profiles_id", table_name="model_profiles")
    op.drop_table("model_profiles")
