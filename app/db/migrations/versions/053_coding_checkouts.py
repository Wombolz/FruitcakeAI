"""Add durable managed coding worktree identities.

Revision ID: 053_coding_checkouts
Revises: 052_coding_sessions
"""
from alembic import op
import sqlalchemy as sa

revision = "053_coding_checkouts"
down_revision = "052_coding_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "coding_checkouts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("coding_sessions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False, unique=True),
        sa.Column("baseline_commit", sa.String(64)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("session_id", name="uq_coding_checkout_session"),
    )
    op.create_index("ix_coding_checkouts_user_id", "coding_checkouts", ["user_id"])


def downgrade():
    # Downgrading forgets ownership, but deliberately never deletes files/Git
    # registrations. Operators must retain/migrate the data before downgrading.
    op.drop_table("coding_checkouts")
