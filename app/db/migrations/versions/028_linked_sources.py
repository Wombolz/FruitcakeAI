"""linked sources

Revision ID: 028_linked_sources
Revises: 027_task_llm_model_override
Create Date: 2026-03-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "028_linked_sources"
down_revision = "027_task_llm_model_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "linked_sources" not in tables:
        op.create_table(
            "linked_sources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("source_type", sa.String(length=20), nullable=False),
            sa.Column("root_path", sa.Text(), nullable=False),
            sa.Column("scope", sa.String(length=50), nullable=False, server_default="personal"),
            sa.Column("sync_status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_linked_sources_owner_id", "linked_sources", ["owner_id"])

    document_columns = {column["name"] for column in inspector.get_columns("documents")}
    if "linked_source_id" not in document_columns:
        op.add_column("documents", sa.Column("linked_source_id", sa.Integer(), nullable=True))
        op.create_index("ix_documents_linked_source_id", "documents", ["linked_source_id"])
        op.create_foreign_key(
            "fk_documents_linked_source_id",
            "documents",
            "linked_sources",
            ["linked_source_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "source_mode" not in document_columns:
        op.add_column("documents", sa.Column("source_mode", sa.String(length=20), nullable=False, server_default="imported"))
    if "source_sync_status" not in document_columns:
        op.add_column("documents", sa.Column("source_sync_status", sa.String(length=30), nullable=False, server_default="synced"))
    if "source_modified_at" not in document_columns:
        op.add_column("documents", sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True))
    if "source_last_seen_at" not in document_columns:
        op.add_column("documents", sa.Column("source_last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    document_columns = {column["name"] for column in inspector.get_columns("documents")}

    if "source_last_seen_at" in document_columns:
        op.drop_column("documents", "source_last_seen_at")
    if "source_modified_at" in document_columns:
        op.drop_column("documents", "source_modified_at")
    if "source_sync_status" in document_columns:
        op.drop_column("documents", "source_sync_status")
    if "source_mode" in document_columns:
        op.drop_column("documents", "source_mode")
    if "linked_source_id" in document_columns:
        fk_names = {fk["name"] for fk in inspector.get_foreign_keys("documents") if fk.get("name")}
        if "fk_documents_linked_source_id" in fk_names:
            op.drop_constraint("fk_documents_linked_source_id", "documents", type_="foreignkey")
        indexes = {idx["name"] for idx in inspector.get_indexes("documents")}
        if "ix_documents_linked_source_id" in indexes:
            op.drop_index("ix_documents_linked_source_id", table_name="documents")
        op.drop_column("documents", "linked_source_id")

    tables = set(inspector.get_table_names())
    if "linked_sources" in tables:
        indexes = {idx["name"] for idx in inspector.get_indexes("linked_sources")}
        if "ix_linked_sources_owner_id" in indexes:
            op.drop_index("ix_linked_sources_owner_id", table_name="linked_sources")
        op.drop_table("linked_sources")
