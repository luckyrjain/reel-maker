"""Add hook_variants and thumbnail_candidates columns to cuts.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cuts", sa.Column("thumbnail_candidates", sa.JSON(), nullable=True))
    op.add_column("cuts", sa.Column("hook_variants", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("cuts", "hook_variants")
    op.drop_column("cuts", "thumbnail_candidates")
