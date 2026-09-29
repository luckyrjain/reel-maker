"""Add rendered_guide_fingerprint column to cuts.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cuts", sa.Column("rendered_guide_fingerprint", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("cuts", "rendered_guide_fingerprint")
