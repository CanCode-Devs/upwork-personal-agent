"""skip US work-auth, W-2, and on-site jobs from scoring

Revision ID: 013_eligibility_hard_blocks
Revises: 012_application_proposal_id
Create Date: 2026-08-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013_eligibility_hard_blocks"
down_revision: Union[str, None] = "012_application_proposal_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("skip_us_work_auth", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("skip_w2_only", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("skip_onsite", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("skip_onsite")
        batch.drop_column("skip_w2_only")
        batch.drop_column("skip_us_work_auth")
