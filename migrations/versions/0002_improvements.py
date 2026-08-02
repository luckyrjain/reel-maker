"""Add pipeline robustness, asset pinning, licensing, observability, and encryption columns.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # §1b — job heartbeat + start time for stuck-job detection
    op.add_column("jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    # §7 — generation path metadata
    op.add_column("jobs", sa.Column("meta", sa.JSON(), nullable=True))

    # §2 — per-beat asset pinning on cut_assets
    # Existing rows have no beat_index data — clear them so the unique constraint
    # can be created cleanly. Assets are re-resolved on the next render.
    op.execute("DELETE FROM cut_assets")
    op.add_column("cut_assets", sa.Column("beat_index", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("cut_assets", sa.Column("order_in_beat", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("cut_assets", sa.Column("resolved_from", sa.String(16), nullable=True))
    op.create_unique_constraint(
        "uq_cut_beat_order", "cut_assets", ["cut_id", "beat_index", "order_in_beat"]
    )

    # §6 — licensing metadata on assets
    op.add_column("assets", sa.Column("license_url", sa.String(500), nullable=True))
    op.add_column("assets", sa.Column("attribution", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("safe_to_publish", sa.Boolean(), nullable=False, server_default="false"))

    # §8 — pipeline observability
    op.create_table(
        "stage_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reel_id", sa.Integer(), sa.ForeignKey("reels.id"), nullable=False),
        sa.Column("cut_id", sa.Integer(), sa.ForeignKey("cuts.id"), nullable=True),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("model_name", sa.String(255), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("stage_events")
    op.drop_column("assets", "safe_to_publish")
    op.drop_column("assets", "attribution")
    op.drop_column("assets", "license_url")
    op.drop_constraint("uq_cut_beat_order", "cut_assets", type_="unique")
    op.drop_column("cut_assets", "resolved_from")
    op.drop_column("cut_assets", "order_in_beat")
    op.drop_column("cut_assets", "beat_index")
    op.drop_column("jobs", "meta")
    op.drop_column("jobs", "heartbeat_at")
    op.drop_column("jobs", "started_at")
