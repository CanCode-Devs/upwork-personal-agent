"""proposal screening and apply payload

Revision ID: 006_proposal_apply
Revises: 005_proposal_milestones
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_proposal_apply"
down_revision: Union[str, None] = "005_proposal_milestones"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(sa.Column("screening_json", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("apply_json", sa.Text(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("apply_json")
        batch.drop_column("screening_json")
