"""Freeze imported skill instructions and supporting text files together."""
from alembic import op
import sqlalchemy as sa

revision = "062_skill_bundles"
down_revision = "061_persona_visual_assets"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("skills", sa.Column("bundle_files_json", sa.Text(), nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM skills WHERE bundle_files_json IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Installed skill bundles exist; refusing to discard their reference files")
    op.drop_column("skills", "bundle_files_json")
