"""critique rounds, critique json, and clear the AI opening hook

Revision ID: 016_critique_and_humanize
Revises: 015_search_filters
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "016_critique_and_humanize"
down_revision: Union[str, None] = "015_search_filters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(sa.Column("critique_json", sa.Text(), nullable=False, server_default="{}"))
    with op.batch_alter_table("proposal_settings") as batch:
        batch.add_column(sa.Column("critique_rounds", sa.Integer(), nullable=False, server_default="1"))
    op.execute(
        """
        UPDATE proposal_settings
        SET opening_hook = '',
            enforce_opening_hook = 0,
            target_words = COALESCE(target_words, 150)
        WHERE lower(opening_hook) LIKE '%ai-generated proposal%'
           OR lower(opening_hook) LIKE '%ai generated proposal%'
           OR opening_hook LIKE '%you''ll probably get a lot of AI%'
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("proposal_settings") as batch:
        batch.drop_column("critique_rounds")
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("critique_json")
