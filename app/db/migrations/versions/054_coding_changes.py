"""Add opt-in host-applied coding changes and durable review evidence."""
from alembic import op
import sqlalchemy as sa

revision = "054_coding_changes"
down_revision = "053_coding_checkouts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("coding_sessions", sa.Column("edit_mode", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "coding_changes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("coding_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("approval_id", sa.String(64), sa.ForeignKey("coding_approvals.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("checkout_id", sa.String(64), sa.ForeignKey("coding_checkouts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False), sa.Column("tool", sa.String(40), nullable=False),
        sa.Column("before_text", sa.Text()), sa.Column("after_text", sa.Text(), nullable=False),
        sa.Column("before_hash", sa.String(64)), sa.Column("after_hash", sa.String(64), nullable=False),
        sa.Column("diff", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_coding_changes_run_id", "coding_changes", ["run_id"])


def downgrade():
    op.drop_table("coding_changes")
    op.drop_column("coding_sessions", "edit_mode")
