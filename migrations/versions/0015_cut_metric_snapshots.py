"""Add cut_metric_snapshots table for a metrics time series.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cut_metric_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cut_id", sa.Integer(), sa.ForeignKey("cuts.id"), nullable=False),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("comments", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_cut_metric_snapshots_cut_id_recorded_at",
        "cut_metric_snapshots", ["cut_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cut_metric_snapshots_cut_id_recorded_at", table_name="cut_metric_snapshots")
    op.drop_table("cut_metric_snapshots")
