"""message rooms and chat drafts

Revision ID: 009_messages
Revises: 008_role_letter_structure
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009_messages"
down_revision: Union[str, None] = "008_role_letter_structure"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_rooms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("room_id", sa.String(length=128), nullable=False),
        sa.Column("context_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("context_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("counterpart", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("snippet", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unread", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("send_status", sa.String(length=32), nullable=False, server_default="idle"),
        sa.Column("send_error", sa.Text(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_message_rooms_room_id", "message_rooms", ["room_id"], unique=True)
    op.create_index("ix_message_rooms_last_message_at", "message_rooms", ["last_message_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("room_pk", sa.Integer(), sa.ForeignKey("message_rooms.id"), nullable=False),
        sa.Column("upwork_message_id", sa.String(length=128), nullable=False),
        sa.Column("sender", sa.String(length=32), nullable=False, server_default="client"),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_chat_messages_room_pk", "chat_messages", ["room_pk"])
    op.create_index("ix_chat_messages_upwork_message_id", "chat_messages", ["upwork_message_id"], unique=True)

    op.create_table(
        "message_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("room_pk", sa.Integer(), sa.ForeignKey("message_rooms.id"), nullable=False),
        sa.Column("suggested_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_message_drafts_room_pk", "message_drafts", ["room_pk"], unique=True)


def downgrade() -> None:
    op.drop_table("message_drafts")
    op.drop_table("chat_messages")
    op.drop_table("message_rooms")
