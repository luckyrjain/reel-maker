"""Add engagement-metrics snapshot columns to cuts.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cuts", sa.Column("views", sa.Integer(), nullable=True))
    op.add_column("cuts", sa.Column("likes", sa.Integer(), nullable=True))
    op.add_column("cuts", sa.Column("comments", sa.Integer(), nullable=True))
    op.add_column("cuts", sa.Column("metrics_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("cuts", "metrics_updated_at")
    op.drop_column("cuts", "comments")
    op.drop_column("cuts", "likes")
    op.drop_column("cuts", "views")
