"""job dashboard fields, applications, scoring prefs

Revision ID: 004_job_dashboard
Revises: 003_work_memory
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_job_dashboard"
down_revision: Union[str, None] = "003_work_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("price_label", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("timezone", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("job_type", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("client_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("applied_on_upwork", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_jobs_applied_on_upwork", ["applied_on_upwork"])

    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("require_verified_payment", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("min_client_rating", sa.Float(), nullable=True))
        batch.add_column(sa.Column("min_client_hires", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("max_proposal_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("prefer_timezones", sa.Text(), nullable=False, server_default=""))

    op.create_table(
        "upwork_applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("posting_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("rate", sa.String(length=64), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_upwork_applications_posting_id", "upwork_applications", ["posting_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_upwork_applications_posting_id", table_name="upwork_applications")
    op.drop_table("upwork_applications")
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("prefer_timezones")
        batch.drop_column("max_proposal_count")
        batch.drop_column("min_client_hires")
        batch.drop_column("min_client_rating")
        batch.drop_column("require_verified_payment")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_applied_on_upwork")
        batch.drop_column("applied_on_upwork")
        batch.drop_column("client_json")
        batch.drop_column("job_type")
        batch.drop_column("timezone")
        batch.drop_column("price_label")
