"""Add text_color column to reels.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("reels", sa.Column("text_color", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("reels", "text_color")
