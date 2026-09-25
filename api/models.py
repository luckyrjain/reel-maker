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
    tiktok = "tiktok"


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
    # Edge-tts voice name from engine/render/tts.py::CURATED_EDGE_VOICES, or None to use
    # EdgeTTSProvider.DEFAULT_VOICE. Only applies when TTS_PROVIDER=edge (the default) — see
    # get_tts_provider()'s docstring for why Kokoro doesn't get a per-reel voice choice.
    tts_voice = Column(String(100), nullable=True)
    # On-screen text color from engine/render/compositor.py::CURATED_TEXT_COLORS, or None
    # for DEFAULT_TEXT_COLOR ("white"). Not free text/hex — see CURATED_TEXT_COLORS'
    # docstring: an unvalidated value here is an ffmpeg drawtext filter-graph injection
    # point, not just a rendering-quality one.
    text_color = Column(String(20), nullable=True)
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
    # All candidate frames written by the last render; thumbnail_path is whichever one is
    # currently chosen (candidates[0] by default — see engine/render/compositor.py).
    thumbnail_candidates = Column(JSON)
    # Alternate opening-line text for the hook beat, generated once per guide
    # (worker/tasks/generate.py) so the operator can swap it in without a full regeneration.
    hook_variants = Column(JSON)
    # 0-indexed beat indices where resolve_beat_assets()'s whole Wikipedia -> Pexels ->
    # HF Video -> HF Image chain came up empty and the compositor rendered a black frame
    # for that beat's full duration. None/empty when every beat got real footage.
    # Written by render_cut; a re-render replaces it wholesale. See
    # engine/render/asset_sourcer.py's fallback chain and docs/roadmap.md's Phase 7
    # asset_sourcer visibility item — this is operator-visible so a black-frame reel
    # doesn't silently report `done`.
    black_frame_beat_indices = Column(JSON)
    duration_s = Column(Float)
    status = Column(SAEnum(CutStatus), default=CutStatus.draft, nullable=False)
    published_at = Column(DateTime(timezone=True))
    platform_post_id = Column(String(255))

    # Latest known engagement snapshot — not a time series (see docs/roadmap.md
    # Phase 5 for that). Populated by worker/tasks/metrics.py::pull_publish_metrics,
    # a periodic beat task; None until that task has run at least once for this cut.
    views = Column(Integer)
    likes = Column(Integer)
    comments = Column(Integer)
    metrics_updated_at = Column(DateTime(timezone=True))

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


class PerformanceNote(Base):
    """Operator-written, plain-English note synthesizing past reel performance
    (e.g. "Hooks phrased as a direct question outperform statement hooks — lean
    into that"). Every *active* note is seeded into generate_guide's
    prior_feedback on the standard LLM path — see worker/tasks/generate.py and
    docs/specs/2026-09-phase5-quality-engagement-feedback.md §3. Deliberately
    plain text, human-curated: no raw past-reel content is ever auto-injected,
    only what the operator chose to write after reviewing the /api/insights
    top/bottom performer report."""
    __tablename__ = "performance_notes"

    id = Column(Integer, primary_key=True)
    text = Column(Text, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True)
    provider = Column(String(100), nullable=False)
    account_label = Column(String(255))
    token_blob = Column(Encrypted)
    refresh_token_blob = Column(Encrypted)
    # Provider-specific ID discovered during OAuth that isn't a scope — e.g. the
    # Instagram Business Account ID behind a connected Facebook Page, or a
    # YouTube channel ID. Publishers read this to know what to post to.
    provider_account_id = Column(String(255))
    scopes = Column(JSON)
    expires_at = Column(DateTime(timezone=True))
