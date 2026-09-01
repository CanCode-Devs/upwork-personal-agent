"""structured job skip filters on app settings

Revision ID: 015_search_filters
Revises: 014_user_roles
Create Date: 2026-08-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015_search_filters"
down_revision: Union[str, None] = "014_user_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("skip_entry_level", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("job_type_filter", sa.String(length=16), nullable=False, server_default="any"))
        batch.add_column(sa.Column("engagement_filter", sa.String(length=16), nullable=False, server_default="any"))
        batch.add_column(sa.Column("blocked_client_countries", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("min_client_spend", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("max_connects_cost", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("max_connects_cost")
        batch.drop_column("min_client_spend")
        batch.drop_column("blocked_client_countries")
        batch.drop_column("engagement_filter")
        batch.drop_column("job_type_filter")
        batch.drop_column("skip_entry_level")
