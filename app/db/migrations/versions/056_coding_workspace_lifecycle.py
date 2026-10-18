"""Reusable checkouts, conversation lifecycle, and per-run model identity."""
from alembic import op
import sqlalchemy as sa

revision = "056_coding_workspace_lifecycle"
down_revision = "055_coding_command_mode"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("coding_sessions", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.add_column("coding_sessions", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    with op.batch_alter_table("coding_checkouts") as batch:
        batch.add_column(sa.Column("workspace_id", sa.String(64)))
        batch.add_column(sa.Column("head_commit", sa.String(64)))
        batch.add_column(sa.Column("branch_name", sa.String(100)))
        batch.create_foreign_key("fk_checkout_workspace", "coding_workspaces", ["workspace_id"], ["id"], ondelete="RESTRICT")
    with op.batch_alter_table("coding_runs") as batch:
        batch.add_column(sa.Column("model_profile_id", sa.Integer()))
        batch.add_column(sa.Column("model_id", sa.String(255)))
        batch.create_foreign_key("fk_run_model", "model_profiles", ["model_profile_id"], ["id"], ondelete="SET NULL")
    op.create_table("coding_checkout_attachments",
                    sa.Column("session_id", sa.String(64), sa.ForeignKey("coding_sessions.id", ondelete="CASCADE"), primary_key=True),
                    sa.Column("checkout_id", sa.String(64), sa.ForeignKey("coding_checkouts.id", ondelete="RESTRICT"), nullable=False))
    op.create_index("ix_coding_checkout_attachments_checkout_id", "coding_checkout_attachments", ["checkout_id"])
    op.execute("UPDATE coding_checkouts SET workspace_id=(SELECT workspace_id FROM coding_sessions WHERE coding_sessions.id=coding_checkouts.session_id), head_commit=baseline_commit")
    op.execute("INSERT INTO coding_checkout_attachments (session_id,checkout_id) SELECT session_id,id FROM coding_checkouts")
    # Prior runs predate model switching; the session's model is their best-known identity.
    op.execute("UPDATE coding_runs SET model_profile_id=(SELECT model_profile_id FROM coding_sessions WHERE coding_sessions.id=coding_runs.session_id)")
    op.execute("UPDATE coding_runs SET model_id=(SELECT model_id FROM model_profiles WHERE model_profiles.id=coding_runs.model_profile_id)")


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM coding_sessions WHERE archived_at IS NOT NULL OR deleted_at IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Reconcile archived/deleted conversation retention before downgrading")
    if connection.execute(sa.text("SELECT 1 FROM coding_checkout_attachments a JOIN coding_checkouts c ON c.id=a.checkout_id WHERE a.session_id<>c.session_id LIMIT 1")).first():
        raise RuntimeError("Detach additional conversations before downgrading reusable checkouts")
    if connection.execute(sa.text("SELECT 1 FROM coding_checkouts WHERE head_commit<>baseline_commit OR branch_name IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Committed checkouts cannot be represented by the older baseline-only schema")
    op.drop_table("coding_checkout_attachments")
    with op.batch_alter_table("coding_runs") as batch:
        batch.drop_constraint("fk_run_model", type_="foreignkey")
        batch.drop_column("model_profile_id")
        batch.drop_column("model_id")
    with op.batch_alter_table("coding_checkouts") as batch:
        batch.drop_constraint("fk_checkout_workspace", type_="foreignkey")
        for name in ("workspace_id", "head_commit", "branch_name"):
            batch.drop_column(name)
    op.drop_column("coding_sessions", "deleted_at")
    op.drop_column("coding_sessions", "archived_at")
