"""Add tts_voice column to reels.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("reels", sa.Column("tts_voice", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("reels", "tts_voice")
