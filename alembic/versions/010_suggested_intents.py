"""store suggested reply intents on drafts

Revision ID: 010_suggested_intents
Revises: 009_messages
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_suggested_intents"
down_revision: Union[str, None] = "009_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("message_drafts") as batch:
        batch.add_column(sa.Column("suggested_intents", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("message_drafts") as batch:
        batch.drop_column("suggested_intents")
