"""Tests for the reel list/detail HTML routes in api/routers/reels.py."""
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


def test_reel_detail_links_back_to_list(client):
    reel_id = _make_reel(client._session_factory)
    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200


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
