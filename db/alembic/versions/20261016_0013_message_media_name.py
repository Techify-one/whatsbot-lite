"""keep the name the sender gave a media file

Revision ID: 0013_message_media_name
Revises: 0012_message_media_state
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013_message_media_name"
down_revision: Union[str, Sequence[str], None] = "0012_message_media_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # The file manager shows this instead of the on-disk (uuid) name, and the
    # download uses it. NULL = no name known. Nullable on purpose and no
    # backfill: old documents carry their name in `content`, which the reader
    # falls back to, so nothing rewrites a table that can hold millions of rows.
    op.add_column("messages", sa.Column("media_name", sa.Text, nullable=True))

def downgrade() -> None:
    op.drop_column("messages", "media_name")
