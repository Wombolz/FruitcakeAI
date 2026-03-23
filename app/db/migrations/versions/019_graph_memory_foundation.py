"""graph memory foundation

Revision ID: 019_graph_memory_foundation
Revises: 018_document_ingest_jobs
Create Date: 2026-03-23
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "019_graph_memory_foundation"
down_revision = "018_document_ingest_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("memory_entities"):
        op.create_table(
            "memory_entities",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("normalized_name", sa.String(length=255), nullable=False),
            sa.Column("entity_type", sa.String(length=100), nullable=False, server_default="unknown"),
            sa.Column("aliases", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not inspector.has_table("memory_relations"):
        op.create_table(
            "memory_relations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("from_entity_id", sa.Integer(), nullable=False),
            sa.Column("to_entity_id", sa.Integer(), nullable=False),
            sa.Column("relation_type", sa.String(length=100), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("source_memory_id", sa.Integer(), nullable=True),
            sa.Column("source_session_id", sa.Integer(), nullable=True),
            sa.Column("source_task_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["from_entity_id"], ["memory_entities.id"]),
            sa.ForeignKeyConstraint(["to_entity_id"], ["memory_entities.id"]),
            sa.ForeignKeyConstraint(["source_memory_id"], ["memories.id"]),
            sa.ForeignKeyConstraint(["source_session_id"], ["chat_sessions.id"]),
            sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not inspector.has_table("memory_observations"):
        op.create_table(
            "memory_observations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("entity_id", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("source_memory_id", sa.Integer(), nullable=True),
            sa.Column("source_session_id", sa.Integer(), nullable=True),
            sa.Column("source_task_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["entity_id"], ["memory_entities.id"]),
            sa.ForeignKeyConstraint(["source_memory_id"], ["memories.id"]),
            sa.ForeignKeyConstraint(["source_session_id"], ["chat_sessions.id"]),
            sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if inspector.has_table("memory_entities"):
        existing = {idx["name"] for idx in inspector.get_indexes("memory_entities")}
        for name, cols in [
            ("ix_memory_entities_user_id", ["user_id"]),
            ("ix_memory_entities_normalized_name", ["normalized_name"]),
        ]:
            if name not in existing:
                op.create_index(name, "memory_entities", cols, unique=False)

    if inspector.has_table("memory_relations"):
        existing = {idx["name"] for idx in inspector.get_indexes("memory_relations")}
        for name, cols in [
            ("ix_memory_relations_user_id", ["user_id"]),
            ("ix_memory_relations_from_entity_id", ["from_entity_id"]),
            ("ix_memory_relations_to_entity_id", ["to_entity_id"]),
            ("ix_memory_relations_source_memory_id", ["source_memory_id"]),
            ("ix_memory_relations_source_session_id", ["source_session_id"]),
            ("ix_memory_relations_source_task_id", ["source_task_id"]),
        ]:
            if name not in existing:
                op.create_index(name, "memory_relations", cols, unique=False)

    if inspector.has_table("memory_observations"):
        existing = {idx["name"] for idx in inspector.get_indexes("memory_observations")}
        for name, cols in [
            ("ix_memory_observations_user_id", ["user_id"]),
            ("ix_memory_observations_entity_id", ["entity_id"]),
            ("ix_memory_observations_source_memory_id", ["source_memory_id"]),
            ("ix_memory_observations_source_session_id", ["source_session_id"]),
            ("ix_memory_observations_source_task_id", ["source_task_id"]),
        ]:
            if name not in existing:
                op.create_index(name, "memory_observations", cols, unique=False)


def downgrade() -> None:
    for index_name in [
        "ix_memory_observations_source_task_id",
        "ix_memory_observations_source_session_id",
        "ix_memory_observations_source_memory_id",
        "ix_memory_observations_entity_id",
        "ix_memory_observations_user_id",
    ]:
        op.drop_index(index_name, table_name="memory_observations")
    for index_name in [
        "ix_memory_relations_source_task_id",
        "ix_memory_relations_source_session_id",
        "ix_memory_relations_source_memory_id",
        "ix_memory_relations_to_entity_id",
        "ix_memory_relations_from_entity_id",
        "ix_memory_relations_user_id",
    ]:
        op.drop_index(index_name, table_name="memory_relations")
    for index_name in [
        "ix_memory_entities_normalized_name",
        "ix_memory_entities_user_id",
    ]:
        op.drop_index(index_name, table_name="memory_entities")
    op.drop_table("memory_observations")
    op.drop_table("memory_relations")
    op.drop_table("memory_entities")
