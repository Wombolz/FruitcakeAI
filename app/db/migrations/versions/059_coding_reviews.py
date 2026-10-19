"""Independent read-only reviewer records, findings, events and approvals."""
from alembic import op
import sqlalchemy as sa

revision = "059_coding_reviews"
down_revision = "058_coding_review_captures"
branch_labels = None
depends_on = None


def fk(name, target, *, nullable=False, primary_key=False, unique=False):
    kind = sa.Integer() if target in {"users.id", "model_profiles.id"} else sa.String(64)
    return sa.Column(name, kind, sa.ForeignKey(target, ondelete="SET NULL" if nullable else "RESTRICT"),
                     nullable=nullable, primary_key=primary_key, unique=unique)


def created():
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade():
    op.create_table("coding_reviews",
        sa.Column("id", sa.String(64), primary_key=True), fk("user_id", "users.id"),
        fk("session_id", "coding_sessions.id"), fk("checkout_id", "coding_checkouts.id"),
        fk("capture_id", "coding_review_captures.id", unique=True),
        sa.Column("client_request_id", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False), fk("model_profile_id", "model_profiles.id", nullable=True),
        *[sa.Column(name, sa.Text(), nullable=False) for name in
          ("model_json", "reasoning_json", "coverage_json", "limitations_json", "summary")],
        sa.Column("status", sa.String(25), nullable=False), sa.Column("error_code", sa.String(50)),
        sa.Column("last_event_sequence", sa.Integer(), nullable=False), created(),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("session_id", "client_request_id", name="uq_coding_review_request"))
    op.create_index("ix_coding_reviews_user_id", "coding_reviews", ["user_id"])
    op.create_index("ix_coding_reviews_session_id", "coding_reviews", ["session_id"])
    op.create_table("coding_review_findings", sa.Column("id", sa.String(64), primary_key=True),
        fk("review_id", "coding_reviews.id"), fk("capture_id", "coding_review_captures.id"),
        sa.Column("severity", sa.String(20), nullable=False), sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False), sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("side", sa.String(10), nullable=False), sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False), sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("disposition", sa.String(20), nullable=False), sa.Column("reason", sa.String(2000)),
        fk("updated_by_user_id", "users.id", nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_coding_review_findings_review_id", "coding_review_findings", ["review_id"])
    op.create_table("coding_review_events", fk("review_id", "coding_reviews.id", primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True), sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False), sa.Column("schema_version", sa.Integer(), nullable=False), created())
    op.create_table("coding_review_approvals", sa.Column("id", sa.String(64), primary_key=True),
        fk("review_id", "coding_reviews.id"), sa.Column("runtime_request_id", sa.String(100), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        created(), sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("review_id", "runtime_request_id", name="uq_coding_review_approval"))
    op.create_index("ix_coding_review_approvals_review_id", "coding_review_approvals", ["review_id"])


def downgrade():
    for table in ("coding_reviews", "coding_review_findings", "coding_review_events", "coding_review_approvals"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Retained review records require operator inspection; disable reviews instead.")
    for table in ("coding_review_approvals", "coding_review_events", "coding_review_findings", "coding_reviews"):
        op.drop_table(table)
