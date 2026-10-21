"""Owner-scoped personality revisions and pinned chat/run selections."""
from alembic import op
import sqlalchemy as sa

revision = "060_persona_revisions"
down_revision = "059_coding_reviews"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("persona_definitions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("head_version", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_persona_definitions_user_id", "persona_definitions", ["user_id"])
    op.create_table("persona_revisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("persona_id", sa.String(64), sa.ForeignKey("persona_definitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("persona_id", "version", name="uq_persona_revision_version"))
    for table, key, kind, target in (
        ("persona_selections", "session_id", sa.Integer(), "chat_sessions.id"),
        ("user_persona_defaults", "user_id", sa.Integer(), "users.id"),
        ("chat_run_persona_snapshots", "run_id", sa.String(64), "chat_runs.id"),
    ):
        columns = [sa.Column(key, kind, sa.ForeignKey(target, ondelete="CASCADE"), primary_key=True),
                   sa.Column("snapshot_json", sa.Text(), nullable=False)]
        if table != "chat_run_persona_snapshots":
            columns.append(sa.Column("version", sa.Integer(), nullable=False))
        op.create_table(table, *columns)


def downgrade():
    tables = ("chat_run_persona_snapshots", "persona_selections", "user_persona_defaults",
              "persona_revisions", "persona_definitions")
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Retained persona history requires operator inspection; refusing data loss.")
    for table in tables:
        op.drop_table(table)
