"""add per-user integrations

Revision ID: 047_user_integrations
Revises: 046_model_profiles
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "047_user_integrations"
down_revision = "046_model_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_integrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("service", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="connected"),
        sa.Column("account_identifier", sa.String(length=255), nullable=True),
        sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("access_token_secret_id", sa.Integer(), nullable=True),
        sa.Column("refresh_token_secret_id", sa.Integer(), nullable=True),
        sa.Column("credential_secret_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_class", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["access_token_secret_id"], ["secrets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["refresh_token_secret_id"], ["secrets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["credential_secret_id"], ["secrets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", "service", name="uq_user_integration_provider_service"),
    )
    op.create_index("ix_user_integrations_id", "user_integrations", ["id"])
    op.create_index("ix_user_integrations_public_id", "user_integrations", ["public_id"], unique=True)
    op.create_index("ix_user_integrations_user_id", "user_integrations", ["user_id"])
    op.create_index(
        "ix_user_integrations_user_service_status",
        "user_integrations",
        ["user_id", "service", "status"],
    )
    op.create_table(
        "integration_oauth_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("service", sa.String(length=50), nullable=False),
        sa.Column("redirect_uri", sa.String(length=500), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_integration_oauth_states_id", "integration_oauth_states", ["id"])
    op.create_index("ix_integration_oauth_states_nonce", "integration_oauth_states", ["nonce"], unique=True)
    op.create_index("ix_integration_oauth_states_user_id", "integration_oauth_states", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_integration_oauth_states_user_id", table_name="integration_oauth_states")
    op.drop_index("ix_integration_oauth_states_nonce", table_name="integration_oauth_states")
    op.drop_index("ix_integration_oauth_states_id", table_name="integration_oauth_states")
    op.drop_table("integration_oauth_states")
    op.drop_index("ix_user_integrations_user_service_status", table_name="user_integrations")
    op.drop_index("ix_user_integrations_user_id", table_name="user_integrations")
    op.drop_index("ix_user_integrations_public_id", table_name="user_integrations")
    op.drop_index("ix_user_integrations_id", table_name="user_integrations")
    op.drop_table("user_integrations")
