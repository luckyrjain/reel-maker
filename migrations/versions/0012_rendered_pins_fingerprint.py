"""Add rendered_pins_fingerprint column to cuts.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cuts", sa.Column("rendered_pins_fingerprint", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("cuts", "rendered_pins_fingerprint")
