"""evolve managed presets into agent instances

Revision ID: 037_managed_agent_instances
Revises: 036_managed_agent_presets
Create Date: 2026-04-11
"""

from alembic import op
import sqlalchemy as sa


revision = "037_managed_agent_instances"
down_revision = "036_managed_agent_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "managed_agent_presets",
        sa.Column("display_name", sa.String(length=255), nullable=True),
    )
    op.execute(
        "UPDATE managed_agent_presets SET display_name = COALESCE(display_name, preset_id)"
    )
    op.alter_column("managed_agent_presets", "display_name", nullable=False)
    op.drop_constraint("uq_managed_agent_presets_user_preset", "managed_agent_presets", type_="unique")
    op.create_unique_constraint(
        "uq_managed_agent_presets_user_display_name",
        "managed_agent_presets",
        ["user_id", "display_name"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_managed_agent_presets_user_display_name", "managed_agent_presets", type_="unique")
    op.create_unique_constraint(
        "uq_managed_agent_presets_user_preset",
        "managed_agent_presets",
        ["user_id", "preset_id"],
    )
    op.drop_column("managed_agent_presets", "display_name")
