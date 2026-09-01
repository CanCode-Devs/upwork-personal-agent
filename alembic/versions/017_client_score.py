"""separate client quality score

Revision ID: 017_client_score
Revises: 016_critique_and_humanize
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "017_client_score"
down_revision: Union[str, None] = "016_critique_and_humanize"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("client_score", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("client_score_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("client_score_breakdown", sa.Text(), nullable=True))
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("min_client_score", sa.Integer(), nullable=False, server_default="50"))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("min_client_score")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("client_score_breakdown")
        batch.drop_column("client_score_reason")
        batch.drop_column("client_score")
