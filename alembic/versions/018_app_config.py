"""app config store for secrets and variables

Revision ID: 018_app_config
Revises: 017_client_score
Create Date: 2026-09-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018_app_config"
down_revision: Union[str, None] = "017_client_score"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_config",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("is_secret", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_config")
