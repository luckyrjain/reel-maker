"""Add black_frame_beat_indices column to cuts.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cuts", sa.Column("black_frame_beat_indices", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("cuts", "black_frame_beat_indices")
