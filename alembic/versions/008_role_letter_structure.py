"""role hire letter structure

Revision ID: 008_role_letter_structure
Revises: 007_proposal_settings
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_role_letter_structure"
down_revision: Union[str, None] = "007_proposal_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("proposal_settings") as batch:
        batch.add_column(sa.Column("role_letter_structure", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("proposal_settings") as batch:
        batch.drop_column("role_letter_structure")
