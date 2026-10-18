"""Add coding workspace grants, sessions, runs, events, and approvals.

Revision ID: 052_coding_sessions
Revises: 051_project_navigation
"""
from alembic import op
import sqlalchemy as sa

revision = "052_coding_sessions"
down_revision = "051_project_navigation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "coding_workspaces",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False),
        sa.Column("approved_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "canonical_path", name="uq_coding_workspace_owner_path"),
    )
    op.create_index("ix_coding_workspaces_user_id", "coding_workspaces", ["user_id"])
    op.create_table(
        "coding_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("coding_workspaces.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("user_projects.id", ondelete="SET NULL")),
        sa.Column("model_profile_id", sa.Integer(), sa.ForeignKey("model_profiles.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("persona", sa.String(100), nullable=False),
        sa.Column("runtime_kind", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("context_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_coding_sessions_user_id", "coding_sessions", ["user_id"])
    op.create_table(
        "coding_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("coding_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_request_id", sa.String(100)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("context_generation", sa.Integer(), nullable=False),
        sa.Column("last_event_sequence", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("session_id", "client_request_id", name="uq_coding_run_client_request"),
    )
    for column in ("session_id", "user_id"):
        op.create_index(f"ix_coding_runs_{column}", "coding_runs", [column])
    active = sa.text("status IN ('starting','running','waiting_approval','stopping')")
    op.create_index("uq_coding_run_active_session", "coding_runs", ["session_id"], unique=True,
                    postgresql_where=active, sqlite_where=active)
    op.create_table(
        "coding_run_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("coding_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "sequence", name="uq_coding_event_sequence"),
    )
    op.create_index("ix_coding_run_events_run_id", "coding_run_events", ["run_id"])
    op.create_table(
        "coding_approvals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("coding_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("runtime_request_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("resolved_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "runtime_request_id", name="uq_coding_approval_request"),
    )
    op.create_index("ix_coding_approvals_run_id", "coding_approvals", ["run_id"])


def downgrade():
    for table in ("coding_approvals", "coding_run_events", "coding_runs", "coding_sessions", "coding_workspaces"):
        op.drop_table(table)
