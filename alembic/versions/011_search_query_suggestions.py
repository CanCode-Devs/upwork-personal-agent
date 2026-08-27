"""pending and dismissed search query suggestions

Revision ID: 011_search_query_suggestions
Revises: 010_suggested_intents
Create Date: 2026-08-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011_search_query_suggestions"
down_revision: Union[str, None] = "010_suggested_intents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("pending_search_queries", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("dismissed_search_queries", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("dismissed_search_queries")
        batch.drop_column("pending_search_queries")
