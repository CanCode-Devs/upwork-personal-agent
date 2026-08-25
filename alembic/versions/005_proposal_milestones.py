"""store proposal milestones for review and submit

Revision ID: 005_proposal_milestones
Revises: 004_job_dashboard
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_proposal_milestones"
down_revision: Union[str, None] = "004_job_dashboard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(sa.Column("milestones_json", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("milestones_json")
