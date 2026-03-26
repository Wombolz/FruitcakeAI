"""task llm model override

Revision ID: 027_task_llm_model_override
Revises: 026_task_api_state
Create Date: 2026-03-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "027_task_llm_model_override"
down_revision = "026_task_api_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "llm_model_override" not in columns:
        op.add_column("tasks", sa.Column("llm_model_override", sa.String(length=200), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "llm_model_override" in columns:
        op.drop_column("tasks", "llm_model_override")
