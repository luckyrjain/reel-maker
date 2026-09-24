"""Tests for the reel list/detail HTML routes in api/routers/reels.py."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.db import get_db
from api.main import app


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        c._session_factory = TestingSessionLocal
        yield c
    app.dependency_overrides.clear()


def _make_reel(session_factory, **overrides):
    db = session_factory()
    try:
        reel = models.Reel(
            context=overrides.get("context", "A test reel about something"),
            niche=overrides.get("niche", "football"),
            status=overrides.get("status", models.ReelStatus.guide_ready),
        )
        db.add(reel)
        db.flush()
        cut = models.Cut(
            reel_id=reel.id,
            platform=models.CutPlatform.youtube_shorts,
            target_length_s=45.0,
            status=overrides.get("cut_status", models.CutStatus.draft),
            video_path=overrides.get("video_path"),
        )
        db.add(cut)
        db.commit()
        db.refresh(reel)
        return reel.id
    finally:
        db.close()


def test_list_reels_empty_state(client):
    resp = client.get("/api/reels")
    assert resp.status_code == 200
    assert "No reels yet" in resp.text


def test_list_reels_shows_created_reels(client):
    _make_reel(client._session_factory, context="First reel about tactics")
    _make_reel(client._session_factory, context="Second reel about transfers", niche="finance")

    resp = client.get("/api/reels")
    assert resp.status_code == 200
    assert "First reel about tactics" in resp.text
    assert "Second reel about transfers" in resp.text
    assert "finance" in resp.text


def test_list_reels_pagination_flags(client):
    for i in range(3):
        _make_reel(client._session_factory, context=f"Reel number {i}")

    resp = client.get("/api/reels?page=1")
    assert resp.status_code == 200
    # 3 reels fit on one page (page size 50) — no "Older" link
    assert "Older" not in resp.text


def test_list_reels_shows_quality_and_views_columns(client):
    reel_id = _make_reel(client._session_factory, context="Reel with metrics")

    db = client._session_factory()
    try:
        job = models.Job(
            type=models.JobType.generate,
            reel_id=reel_id,
            status=models.JobStatus.done,
            progress=100,
            meta={"quality_score": 87},
        )
        db.add(job)
        cut = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).first()
        cut.views = 4200
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/reels")
    assert resp.status_code == 200
    assert "87" in resp.text
    assert "4,200" in resp.text


def test_list_reels_shows_dash_when_no_metrics_yet(client):
    _make_reel(client._session_factory, context="Reel without metrics")

    resp = client.get("/api/reels")
    assert resp.status_code == 200
    # Both the Quality and Views cells render a placeholder dash.
    assert resp.text.count(">—<") >= 2


def test_reel_detail_links_back_to_list(client):
    reel_id = _make_reel(client._session_factory)
    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200


def test_failed_cut_card_has_no_duplicate_ids_and_valid_hx_targets(client):
    """Regression test: a "Retry publish" button once targeted #cut-card-{id}
    with outerHTML while the endpoint actually returns the small publish_status
    fragment — silently destroying the card on retry. Every hx-target here must
    resolve to an id that actually exists in the same render, and no id may be
    duplicated (htmx swaps get undefined/wrong-element behavior otherwise).
    """
    import re

    reel_id = _make_reel(
        client._session_factory,
        cut_status=models.CutStatus.failed,
        video_path="/data/videos/1/youtube_shorts.mp4",
    )
    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    html = resp.text

    ids = re.findall(r'id="([^"]+)"', html)
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate DOM ids: {duplicates}"

    targets = re.findall(r'hx-target="#([^"]+)"', html)
    assert "publish-section-" in "".join(targets)  # sanity: the retry-publish button is present
    for target in targets:
        assert target in ids, f"hx-target=#{target} has no matching id=\"{target}\" in the page"


def test_published_cut_card_shows_engagement_stats(client):
    reel_id = _make_reel(client._session_factory, cut_status=models.CutStatus.published)

    db = client._session_factory()
    try:
        cut = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).first()
        cut.views = 1234
        cut.likes = 56
        cut.comments = 7
        cut.metrics_updated_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        db.commit()
    finally:
        db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert "1,234" in resp.text
    assert "views" in resp.text
    assert "56" in resp.text
    assert "as of 2026-01-01 12:00 UTC" in resp.text


def test_published_cut_card_shows_not_pulled_yet_before_first_metrics_pull(client):
    reel_id = _make_reel(client._session_factory, cut_status=models.CutStatus.published)

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert "not pulled yet" in resp.text


def test_reel_detail_404_for_missing_reel(client):
    resp = client.get("/api/reels/999999")
    assert resp.status_code == 404


def test_reel_detail_surfaces_pipeline_cost_and_quality(client):
    db = client._session_factory()
    reel = models.Reel(context="A reel with pipeline data", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()

    job = models.Job(
        type=models.JobType.generate,
        reel_id=reel.id,
        status=models.JobStatus.done,
        meta={"quality_score": 82},
    )
    db.add(job)

    db.add(models.StageEvent(
        reel_id=reel.id, stage="generate", provider="nvidia",
        latency_ms=1200, tokens_in=500, tokens_out=200, cost_usd=0.0021, ok=True,
    ))
    db.add(models.StageEvent(
        reel_id=reel.id, stage="judge", provider="nvidia",
        latency_ms=800, tokens_in=100, tokens_out=50, cost_usd=0.0004, ok=True,
    ))
    db.add(models.StageEvent(
        reel_id=reel.id, stage="judge", provider="nvidia",
        latency_ms=900, ok=False, detail={"error": "boom"},
    ))
    db.commit()
    reel_id = reel.id
    db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert "82" in resp.text  # quality score
    assert "$0.0025" in resp.text  # total cost (0.0021 + 0.0004)
    assert "generate" in resp.text
    assert "judge" in resp.text


def test_pipeline_summary_aggregates_stages_and_failures(client):
    from api.routers.reels import _pipeline_summary

    db = client._session_factory()
    reel = models.Reel(context="Aggregation test", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    db.add(models.StageEvent(reel_id=reel.id, stage="enrich", cost_usd=0.001, latency_ms=500, ok=True))
    db.add(models.StageEvent(reel_id=reel.id, stage="enrich", cost_usd=0.002, latency_ms=700, ok=False))
    db.commit()

    summary = _pipeline_summary(db, reel.id)
    assert summary["stage_summary"]["enrich"]["count"] == 2
    assert summary["stage_summary"]["enrich"]["failures"] == 1
    assert round(summary["total_cost"], 3) == 0.003
    assert summary["total_latency_ms"] == 1200
    db.close()


def test_estimate_endpoint_returns_fragment_for_unstructured_context(client):
    resp = client.post(
        "/api/reels/estimate",
        data={"context": "A loose paragraph about a topic.", "generation_path": "auto"},
    )
    assert resp.status_code == 200
    assert "standard path" in resp.text
    assert "paid LLM calls" in resp.text


def test_create_reel_defaults_to_youtube_and_instagram(client):
    with patch("api.routers.reels.enrich_context") as mock_enrich:
        resp = client.post(
            "/api/reels",
            data={"context": "A" * 60, "generation_path": "structured"},
        )
    assert resp.status_code == 200
    mock_enrich.delay.assert_called_once()
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    platforms = {c.platform.value for c in reel.cuts}
    assert platforms == {"youtube_shorts", "instagram_reels"}
    db.close()


def test_create_reel_that_cannot_be_enqueued_fails_fast_instead_of_polling_forever(client):
    with patch("api.routers.reels.enrich_context") as mock_enrich:
        mock_enrich.delay.side_effect = ConnectionError("broker down")
        resp = client.post("/api/reels", data={"context": "A" * 60})
    assert resp.status_code == 503
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.status == models.ReelStatus.failed
    job = db.query(models.Job).filter(models.Job.reel_id == reel.id).one()
    assert job.status == models.JobStatus.failed and "could not enqueue" in job.error
    db.close()


def test_the_enrich_job_is_committed_before_it_is_enqueued(client):
    """Same shape as the cuts router's equivalent test: no transaction of ours may be open across
    .delay() (a slow broker failure would otherwise outlive an idle-in-transaction timeout), and the
    worker must already be able to see the job row it's handed."""
    from api.db import get_db
    from api.main import app

    sessions = []
    real_override = app.dependency_overrides[get_db]

    def tracking_override():
        gen = real_override()
        db = next(gen)
        sessions.append(db)
        try:
            yield db
        finally:
            try:
                next(gen)
            except StopIteration:
                pass

    seen = {}

    def delay(job_id):
        assert isinstance(job_id, int)
        seen["in_transaction_during_delay"] = sessions[-1].in_transaction()
        other = client._session_factory()
        job = other.get(models.Job, job_id)
        seen["visible"] = job is not None and job.status == models.JobStatus.pending
        other.close()

    app.dependency_overrides[get_db] = tracking_override
    try:
        with patch("api.routers.reels.enrich_context") as mock_enrich:
            mock_enrich.delay.side_effect = delay
            resp = client.post("/api/reels", data={"context": "A" * 60})
    finally:
        app.dependency_overrides[get_db] = real_override
    assert resp.status_code == 200, resp.text
    assert seen["visible"] is True
    assert seen["in_transaction_during_delay"] is False


def test_create_reel_honors_explicit_platform_selection(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={
                "context": "A" * 60,
                "generation_path": "structured",
                "platforms": ["youtube_shorts", "tiktok"],
            },
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    platforms = {c.platform.value for c in reel.cuts}
    assert platforms == {"youtube_shorts", "tiktok"}
    db.close()


def test_create_reel_ignores_unknown_platform_values(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={
                "context": "A" * 60,
                "generation_path": "structured",
                "platforms": ["not_a_real_platform"],
            },
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    # Falls back to the default pair since nothing valid was submitted.
    platforms = {c.platform.value for c in reel.cuts}
    assert platforms == {"youtube_shorts", "instagram_reels"}
    db.close()


def test_create_reel_honors_explicit_tts_voice_selection(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={"context": "A" * 60, "generation_path": "structured", "tts_voice": "en-US-JennyNeural"},
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.tts_voice == "en-US-JennyNeural"
    db.close()


def test_create_reel_ignores_unknown_tts_voice_value(client):
    """Same 'drop, don't 422' policy as unknown platform values — falls back to None
    (the provider default) rather than failing the whole submission."""
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={"context": "A" * 60, "generation_path": "structured", "tts_voice": "not-a-real-voice"},
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.tts_voice is None
    db.close()


def test_create_reel_without_tts_voice_defaults_to_none(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post("/api/reels", data={"context": "A" * 60, "generation_path": "structured"})
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.tts_voice is None
    db.close()


def test_estimate_endpoint_detects_structured_script(client):
    structured_ctx = (
        "GOALKEEPER\nMartinez saves penalties.\n"
        "DEFENSE\nRomero leads the line.\n"
        "MIDFIELD\nDe Paul is the engine."
    )
    resp = client.post(
        "/api/reels/estimate",
        data={"context": structured_ctx, "generation_path": "auto"},
    )
    assert resp.status_code == 200
    assert "structured path" in resp.text
