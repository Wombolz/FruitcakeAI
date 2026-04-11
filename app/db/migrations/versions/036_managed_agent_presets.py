"""add managed agent presets

Revision ID: 036_managed_agent_presets
Revises: 035_task_run_agent_metadata
Create Date: 2026-04-11
"""

from alembic import op
import sqlalchemy as sa


revision = "036_managed_agent_presets"
down_revision = "035_task_run_agent_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_agent_presets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("preset_id", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_maintain_task", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("schedule", sa.String(length=100), nullable=True),
        sa.Column("active_hours_start", sa.String(length=5), nullable=True),
        sa.Column("active_hours_end", sa.String(length=5), nullable=True),
        sa.Column("active_hours_tz", sa.String(length=50), nullable=True),
        sa.Column("context_paths_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("params_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("linked_task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "preset_id", name="uq_managed_agent_presets_user_preset"),
    )
    op.create_index("ix_managed_agent_presets_user_id", "managed_agent_presets", ["user_id"])
    op.create_index("ix_managed_agent_presets_preset_id", "managed_agent_presets", ["preset_id"])
    op.create_index("ix_managed_agent_presets_linked_task_id", "managed_agent_presets", ["linked_task_id"])
    op.alter_column("managed_agent_presets", "enabled", server_default=None)
    op.alter_column("managed_agent_presets", "auto_maintain_task", server_default=None)
    op.alter_column("managed_agent_presets", "context_paths_json", server_default=None)
    op.alter_column("managed_agent_presets", "params_json", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_managed_agent_presets_linked_task_id", table_name="managed_agent_presets")
    op.drop_index("ix_managed_agent_presets_preset_id", table_name="managed_agent_presets")
    op.drop_index("ix_managed_agent_presets_user_id", table_name="managed_agent_presets")
    op.drop_table("managed_agent_presets")
