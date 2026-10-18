"""Allow coding approvals to wait without an expiry."""
from alembic import op
import sqlalchemy as sa

revision = "057_coding_approval_wait"
down_revision = "056_coding_workspace_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("coding_approvals") as batch:
        batch.alter_column("expires_at", existing_type=sa.DateTime(timezone=True), nullable=True)


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM coding_approvals WHERE expires_at IS NULL LIMIT 1")).first():
        raise RuntimeError("Non-expiring approval history cannot be represented by the older schema")
    with op.batch_alter_table("coding_approvals") as batch:
        batch.alter_column("expires_at", existing_type=sa.DateTime(timezone=True), nullable=False)
