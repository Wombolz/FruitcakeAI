"""Add explicit per-session container-command opt-in."""
from alembic import op
import sqlalchemy as sa

revision = "055_coding_command_mode"
down_revision = "054_coding_changes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("coding_sessions", sa.Column("command_mode", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("coding_sessions", "command_mode")
