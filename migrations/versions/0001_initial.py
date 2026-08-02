"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("niche", sa.String(255)),
        sa.Column("voiceover_mode", sa.String(50), server_default="voiceover"),
        sa.Column(
            "status",
            sa.Enum("draft", "generating", "guide_ready", "failed", name="reelstatus"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "cuts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reel_id", sa.Integer(), sa.ForeignKey("reels.id"), nullable=False),
        sa.Column(
            "platform",
            sa.Enum("youtube_shorts", "instagram_reels", name="cutplatform"),
            nullable=False,
        ),
        sa.Column("target_length_s", sa.Float()),
        sa.Column("guide", sa.JSON()),
        sa.Column("caption", sa.Text()),
        sa.Column("hashtags", sa.JSON()),
        sa.Column("video_path", sa.String(500)),
        sa.Column("thumbnail_path", sa.String(500)),
        sa.Column("duration_s", sa.Float()),
        sa.Column(
            "status",
            sa.Enum(
                "draft", "rendering", "in_review", "approved",
                "scheduled", "publishing", "published", "failed",
                name="cutstatus",
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("platform_post_id", sa.String(255)),
    )

    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(50)),
        sa.Column("source", sa.String(100)),
        sa.Column("source_ref", sa.String(255)),
        sa.Column("local_path", sa.String(500)),
        sa.Column("license", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "cut_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cut_id", sa.Integer(), sa.ForeignKey("cuts.id"), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("role", sa.String(100)),
        sa.Column("start_s", sa.Float()),
        sa.Column("end_s", sa.Float()),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "type",
            sa.Enum("generate", "render", "publish", name="jobtype"),
            nullable=False,
        ),
        sa.Column("reel_id", sa.Integer(), sa.ForeignKey("reels.id")),
        sa.Column("cut_id", sa.Integer(), sa.ForeignKey("cuts.id")),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "done", "failed", name="jobstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("progress", sa.Integer(), server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("attempts", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("account_label", sa.String(255)),
        sa.Column("token_blob", sa.Text()),
        sa.Column("scopes", sa.JSON()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("credentials")
    op.drop_table("jobs")
    op.drop_table("cut_assets")
    op.drop_table("assets")
    op.drop_table("cuts")
    op.drop_table("reels")
    op.execute("DROP TYPE IF EXISTS jobstatus")
    op.execute("DROP TYPE IF EXISTS jobtype")
    op.execute("DROP TYPE IF EXISTS cutstatus")
    op.execute("DROP TYPE IF EXISTS cutplatform")
    op.execute("DROP TYPE IF EXISTS reelstatus")
