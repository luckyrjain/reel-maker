import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Float, ForeignKey, Integer, JSON, String, Text,
    DateTime, Enum as SAEnum, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship

from api.crypto import Encrypted


def _now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ReelStatus(str, enum.Enum):
    draft = "draft"
    enriching = "enriching"
    generating = "generating"
    guide_ready = "guide_ready"
    failed = "failed"


class CutPlatform(str, enum.Enum):
    youtube_shorts = "youtube_shorts"
    instagram_reels = "instagram_reels"


class CutStatus(str, enum.Enum):
    draft = "draft"
    rendering = "rendering"
    in_review = "in_review"
    approved = "approved"
    scheduled = "scheduled"
    publishing = "publishing"
    published = "published"
    failed = "failed"


class JobType(str, enum.Enum):
    enrich = "enrich"
    generate = "generate"
    render = "render"
    publish = "publish"


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class Reel(Base):
    __tablename__ = "reels"

    id = Column(Integer, primary_key=True)
    context = Column(Text, nullable=False)
    enriched_context = Column(Text, nullable=True)
    niche = Column(String(255))
    voiceover_mode = Column(String(50), default="voiceover")
    status = Column(SAEnum(ReelStatus), default=ReelStatus.draft, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    cuts = relationship("Cut", back_populates="reel", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="reel", cascade="all, delete-orphan")


class Cut(Base):
    __tablename__ = "cuts"

    id = Column(Integer, primary_key=True)
    reel_id = Column(Integer, ForeignKey("reels.id"), nullable=False)
    platform = Column(SAEnum(CutPlatform), nullable=False)
    target_length_s = Column(Float)
    guide = Column(JSON)
    caption = Column(Text)
    hashtags = Column(JSON)
    video_path = Column(String(500))
    thumbnail_path = Column(String(500))
    duration_s = Column(Float)
    status = Column(SAEnum(CutStatus), default=CutStatus.draft, nullable=False)
    published_at = Column(DateTime(timezone=True))
    platform_post_id = Column(String(255))

    reel = relationship("Reel", back_populates="cuts")
    cut_assets = relationship("CutAsset", back_populates="cut", cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True)
    type = Column(String(50))
    source = Column(String(100))
    source_ref = Column(String(255))
    local_path = Column(String(500))
    license = Column(String(255))
    license_url = Column(String(500))
    attribution = Column(Text)
    safe_to_publish = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=_now)

    cut_assets = relationship("CutAsset", back_populates="asset")


class CutAsset(Base):
    __tablename__ = "cut_assets"
    __table_args__ = (
        UniqueConstraint("cut_id", "beat_index", "order_in_beat", name="uq_cut_beat_order"),
    )

    id = Column(Integer, primary_key=True)
    cut_id = Column(Integer, ForeignKey("cuts.id"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    role = Column(String(100))
    beat_index = Column(Integer, nullable=False, default=0)
    order_in_beat = Column(Integer, nullable=False, default=0)
    resolved_from = Column(String(16))  # sha256(visual_direction)[:16]
    start_s = Column(Float)
    end_s = Column(Float)

    cut = relationship("Cut", back_populates="cut_assets")
    asset = relationship("Asset", back_populates="cut_assets")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    type = Column(SAEnum(JobType), nullable=False)
    reel_id = Column(Integer, ForeignKey("reels.id"))
    cut_id = Column(Integer, ForeignKey("cuts.id"))
    status = Column(SAEnum(JobStatus), default=JobStatus.pending, nullable=False)
    progress = Column(Integer, default=0)
    error = Column(Text)
    attempts = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True))
    heartbeat_at = Column(DateTime(timezone=True))
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    reel = relationship("Reel", back_populates="jobs")


class StageEvent(Base):
    """Instrumentation record for one stage of the generation or render pipeline."""
    __tablename__ = "stage_events"

    id = Column(Integer, primary_key=True)
    reel_id = Column(Integer, ForeignKey("reels.id"), nullable=False)
    cut_id = Column(Integer, ForeignKey("cuts.id"))
    stage = Column(String(50), nullable=False)
    provider = Column(String(100))
    model_name = Column(String(255))
    latency_ms = Column(Integer)
    tokens_in = Column(Integer)
    tokens_out = Column(Integer)
    cost_usd = Column(Float)
    attempt = Column(Integer)
    score = Column(Integer)
    ok = Column(Boolean, default=True)
    detail = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_now)


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True)
    provider = Column(String(100), nullable=False)
    account_label = Column(String(255))
    token_blob = Column(Encrypted)
    scopes = Column(JSON)
    expires_at = Column(DateTime(timezone=True))
