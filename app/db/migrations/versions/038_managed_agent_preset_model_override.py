"""add llm model override to managed agent presets

Revision ID: 038_agent_model_override
Revises: 037_managed_agent_instances
Create Date: 2026-04-12
"""

from alembic import op
import sqlalchemy as sa


revision = "038_agent_model_override"
down_revision = "037_managed_agent_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "managed_agent_presets",
        sa.Column("llm_model_override", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("managed_agent_presets", "llm_model_override")
