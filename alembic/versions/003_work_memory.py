"""upwork vs agent work memory

Revision ID: 003_work_memory
Revises: 002_agent_tools
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_work_memory"
down_revision: Union[str, None] = "002_agent_tools"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("portfolio_items") as batch:
        batch.add_column(sa.Column("origin", sa.String(length=32), nullable=False, server_default="agent"))
        batch.add_column(sa.Column("kind", sa.String(length=32), nullable=False, server_default="project"))
        batch.add_column(sa.Column("description", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("external_id", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_portfolio_items_origin", ["origin"])
        batch.create_index("ix_portfolio_items_kind", ["kind"])
        batch.create_index("ix_portfolio_items_external_id", ["external_id"], unique=True)

    op.create_table(
        "upwork_profile",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("overview", sa.Text(), nullable=False),
        sa.Column("skills_json", sa.Text(), nullable=False),
        sa.Column("hourly_rate", sa.Integer(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("upwork_profile")
    with op.batch_alter_table("portfolio_items") as batch:
        batch.drop_index("ix_portfolio_items_external_id")
        batch.drop_index("ix_portfolio_items_kind")
        batch.drop_index("ix_portfolio_items_origin")
        batch.drop_column("synced_at")
        batch.drop_column("external_id")
        batch.drop_column("description")
        batch.drop_column("kind")
        batch.drop_column("origin")
