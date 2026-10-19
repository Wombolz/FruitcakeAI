"""Frozen review captures and fenced checkout admission (no AI review execution)."""
from alembic import op
import sqlalchemy as sa

revision = "058_coding_review_captures"
down_revision = "057_coding_approval_wait"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("coding_checkout_reservations",
        sa.Column("checkout_id", sa.String(64), sa.ForeignKey("coding_checkouts.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("holder_kind", sa.String(20), nullable=False),
        sa.Column("holder_id", sa.String(64), nullable=False),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column("service_epoch", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("coding_review_captures",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("coding_sessions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("checkout_id", sa.String(64), sa.ForeignKey("coding_checkouts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("coding_workspaces.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("client_request_id", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("manifest_hash", sa.String(64)),
        sa.Column("stored_bytes", sa.Integer(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(50)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "client_request_id", name="uq_coding_capture_request"))
    op.create_index("ix_coding_review_captures_user_id", "coding_review_captures", ["user_id"])
    op.create_index("ix_coding_review_captures_checkout_id", "coding_review_captures", ["checkout_id"])


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM coding_review_captures LIMIT 1")).first() or bind.execute(
        sa.text("SELECT 1 FROM coding_checkout_reservations LIMIT 1")
    ).first():
        raise RuntimeError("Retained review evidence or active reservations require operator inspection; disable the feature instead.")
    op.drop_table("coding_review_captures")
    op.drop_table("coding_checkout_reservations")
