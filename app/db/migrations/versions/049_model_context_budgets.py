"""add model-aware context budget fields

Revision ID: 049_model_context_budgets
Revises: 048_user_model_access
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "049_model_context_budgets"
down_revision = "048_user_model_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_profiles",
        sa.Column("context_window_tokens", sa.Integer(), nullable=False, server_default="65536"),
    )
    op.add_column(
        "model_profiles",
        sa.Column("output_reserve_tokens", sa.Integer(), nullable=False, server_default="8192"),
    )
    op.add_column(
        "model_profiles",
        sa.Column("reasoning_reserve_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "model_profiles",
        sa.Column("context_safety_margin_tokens", sa.Integer(), nullable=False, server_default="2048"),
    )


def downgrade() -> None:
    op.drop_column("model_profiles", "context_safety_margin_tokens")
    op.drop_column("model_profiles", "reasoning_reserve_tokens")
    op.drop_column("model_profiles", "output_reserve_tokens")
    op.drop_column("model_profiles", "context_window_tokens")
