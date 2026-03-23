"""graph memory observation active flag

Revision ID: 020_graph_obs_active
Revises: 019_graph_memory_foundation
Create Date: 2026-03-23
"""

from alembic import op
import sqlalchemy as sa


revision = "020_graph_obs_active"
down_revision = "019_graph_memory_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("memory_observations"):
        return
    columns = {col["name"] for col in inspector.get_columns("memory_observations")}
    if "is_active" not in columns:
        op.add_column(
            "memory_observations",
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("memory_observations"):
        return
    columns = {col["name"] for col in inspector.get_columns("memory_observations")}
    if "is_active" in columns:
        op.drop_column("memory_observations", "is_active")
