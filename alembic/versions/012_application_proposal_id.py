"""store Upwork proposal id on applications

Revision ID: 012_application_proposal_id
Revises: 011_search_query_suggestions
Create Date: 2026-08-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012_application_proposal_id"
down_revision: Union[str, None] = "011_search_query_suggestions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("upwork_applications") as batch:
        batch.add_column(sa.Column("proposal_id", sa.String(length=128), nullable=True))
        batch.create_index("ix_upwork_applications_proposal_id", ["proposal_id"])


def downgrade() -> None:
    with op.batch_alter_table("upwork_applications") as batch:
        batch.drop_index("ix_upwork_applications_proposal_id")
        batch.drop_column("proposal_id")
