"""proposal writer settings and examples

Revision ID: 007_proposal_settings
Revises: 006_proposal_apply
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_proposal_settings"
down_revision: Union[str, None] = "006_proposal_apply"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "proposal_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opening_hook", sa.Text(), nullable=False, server_default=""),
        sa.Column("enforce_opening_hook", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("tone", sa.String(length=32), nullable=False, server_default="consultative"),
        sa.Column("letter_structure", sa.Text(), nullable=False, server_default=""),
        sa.Column("must_include", sa.Text(), nullable=False, server_default=""),
        sa.Column("never_say", sa.Text(), nullable=False, server_default=""),
        sa.Column("extra_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("target_words", sa.Integer(), nullable=True),
        sa.Column("milestone_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("milestone_stages", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("milestone_min", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("milestone_max", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("screening_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("apply_questions_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("example_count", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "proposal_examples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("job_post", sa.Text(), nullable=False, server_default=""),
        sa.Column("cover_letter", sa.Text(), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("proposal_examples")
    op.drop_table("proposal_settings")
