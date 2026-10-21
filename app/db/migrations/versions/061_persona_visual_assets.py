"""Private immutable persona image assets and explicit fresh-test options."""
from alembic import op
import sqlalchemy as sa

revision = "061_persona_visual_assets"
down_revision = "060_persona_revisions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("persona_visual_assets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_persona_visual_assets_user_id", "persona_visual_assets", ["user_id"])
    op.create_table("persona_test_options",
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("memory_recall_enabled", sa.Boolean(), nullable=False))


def downgrade():
    for table in ("persona_visual_assets", "persona_test_options"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Retained persona assets/tests require operator inspection; refusing data loss.")
    op.drop_table("persona_test_options")
    op.drop_table("persona_visual_assets")
