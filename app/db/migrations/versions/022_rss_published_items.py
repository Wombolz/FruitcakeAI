"""rss published items

Revision ID: 022_rss_pub_items
Revises: 021_llm_usage_events
Create Date: 2026-03-24 12:20:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "022_rss_pub_items"
down_revision = "021_llm_usage_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("rss_published_items"):
        op.create_table(
            "rss_published_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.Column("task_run_id", sa.Integer(), nullable=False),
            sa.Column("rss_item_id", sa.Integer(), nullable=False),
            sa.Column("url_canonical", sa.Text(), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["rss_item_id"], ["rss_items.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_run_id", "rss_item_id", name="uq_rss_published_items_run_item"),
        )

    existing_indexes = {index["name"] for index in inspector.get_indexes("rss_published_items")}
    desired_indexes = [
        (op.f("ix_rss_published_items_id"), ["id"]),
        (op.f("ix_rss_published_items_task_id"), ["task_id"]),
        (op.f("ix_rss_published_items_task_run_id"), ["task_run_id"]),
        (op.f("ix_rss_published_items_rss_item_id"), ["rss_item_id"]),
        (op.f("ix_rss_published_items_url_canonical"), ["url_canonical"]),
        (op.f("ix_rss_published_items_published_at"), ["published_at"]),
    ]
    for index_name, columns in desired_indexes:
        if index_name not in existing_indexes:
            op.create_index(index_name, "rss_published_items", columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("rss_published_items"):
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("rss_published_items")}
    for index_name in (
        op.f("ix_rss_published_items_published_at"),
        op.f("ix_rss_published_items_url_canonical"),
        op.f("ix_rss_published_items_rss_item_id"),
        op.f("ix_rss_published_items_task_run_id"),
        op.f("ix_rss_published_items_task_id"),
        op.f("ix_rss_published_items_id"),
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="rss_published_items")
    op.drop_table("rss_published_items")
