"""add document ingest jobs

Revision ID: 018_document_ingest_jobs
Revises: 017_document_processing_columns
Create Date: 2026-03-22
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "018_document_ingest_jobs"
down_revision = "017_document_processing_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("document_ingest_jobs"):
        op.create_table(
            "document_ingest_jobs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("document_id", name="uq_document_ingest_jobs_document_id"),
        )

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("document_ingest_jobs")}
    if "ix_document_ingest_jobs_document_id" not in existing_indexes:
        op.create_index(
            "ix_document_ingest_jobs_document_id",
            "document_ingest_jobs",
            ["document_id"],
            unique=False,
        )
    if "ix_document_ingest_jobs_status" not in existing_indexes:
        op.create_index(
            "ix_document_ingest_jobs_status",
            "document_ingest_jobs",
            ["status"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_document_ingest_jobs_status", table_name="document_ingest_jobs")
    op.drop_index("ix_document_ingest_jobs_document_id", table_name="document_ingest_jobs")
    op.drop_table("document_ingest_jobs")
