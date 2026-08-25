"""agent tools tables

Revision ID: 002_agent_tools
Revises: 001_initial
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_agent_tools"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("score_breakdown", sa.Text(), nullable=True))
        batch.add_column(sa.Column("matched_context", sa.Text(), nullable=True))

    op.create_table(
        "preferences_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("enforcement_level", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_preferences_rules_category", "preferences_rules", ["category"])

    op.create_table(
        "portfolio_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_title", sa.String(length=256), nullable=False),
        sa.Column("tech_stack", sa.Text(), nullable=False),
        sa.Column("outcomes_achieved", sa.Text(), nullable=False),
        sa.Column("associated_keywords", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "feedback_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("upwork_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("client_notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_feedback_log_job_id", "feedback_log", ["job_id"])
    op.create_index("ix_feedback_log_upwork_id", "feedback_log", ["upwork_id"])
    op.create_index("ix_feedback_log_outcome", "feedback_log", ["outcome"])

    op.create_table(
        "embedding_index",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("vector_offset", sa.Integer(), nullable=False),
        sa.Column("text_preview", sa.Text(), nullable=False),
    )
    op.create_index("ix_embedding_index_source_type", "embedding_index", ["source_type"])
    op.create_index("ix_embedding_index_source_id", "embedding_index", ["source_id"])

    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("autonomy_mode", sa.String(length=32), nullable=False),
        sa.Column("auto_submit_threshold", sa.Integer(), nullable=False),
        sa.Column("min_score", sa.Integer(), nullable=False),
        sa.Column("min_hourly", sa.Integer(), nullable=True),
        sa.Column("min_fixed", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_table("embedding_index")
    op.drop_table("feedback_log")
    op.drop_table("portfolio_items")
    op.drop_table("preferences_rules")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("matched_context")
        batch.drop_column("score_breakdown")
