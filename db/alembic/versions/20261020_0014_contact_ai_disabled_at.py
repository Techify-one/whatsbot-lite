"""remember when the AI was switched off for a contact

Revision ID: 0014_contact_ai_disabled_at
Revises: 0013_message_media_name
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014_contact_ai_disabled_at"
down_revision: Union[str, Sequence[str], None] = "0013_message_media_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # The automatic re-enabling waits from the later of the last message and this
    # moment. Nullable and no backfill: rows already switched off fall back to
    # their last message, so nothing rewrites the table.
    op.add_column("contacts", sa.Column("ai_disabled_at", sa.Float, nullable=True))

def downgrade() -> None:
    op.drop_column("contacts", "ai_disabled_at")
