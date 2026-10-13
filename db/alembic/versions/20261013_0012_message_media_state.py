"""track why a message has no media file (discarded by policy / removed later)

Revision ID: 0012_message_media_state
Revises: 0011_usage_cache_tokens
Create Date: 2026-10-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012_message_media_state"
down_revision: Union[str, Sequence[str], None] = "0011_usage_cache_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # NULL = nothing special (the file is there, or the message has no file).
    # 'discarded' = not kept because of the auto-download policy.
    # 'removed'   = deleted later from the file manager.
    # Nullable on purpose: no backfill over a table that can hold millions of rows.
    op.add_column("messages", sa.Column("media_state", sa.Text, nullable=True))
    # The file manager joins files back to messages by media_path.
    op.create_index("idx_msg_media_path", "messages", ["media_path"])

def downgrade() -> None:
    op.drop_index("idx_msg_media_path", table_name="messages")
    op.drop_column("messages", "media_state")
