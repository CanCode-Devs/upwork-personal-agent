"""dashboard user roles and event actor

Revision ID: 014_user_roles
Revises: 013_eligibility_hard_blocks
Create Date: 2026-08-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_user_roles"
down_revision: Union[str, None] = "013_eligibility_hard_blocks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("role", sa.String(length=32), nullable=False, server_default="admin"))
        batch.add_column(sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch.create_index("ix_events_user_id", ["user_id"])
        batch.create_foreign_key("fk_events_user_id_users", "users", ["user_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint("fk_events_user_id_users", type_="foreignkey")
        batch.drop_index("ix_events_user_id")
        batch.drop_column("user_id")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_active")
        batch.drop_column("role")
