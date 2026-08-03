"""Tests for POST /api/cuts/{id}/publish and GET /api/cuts/{id}/publish-status."""
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


def _make_cut(session_factory, status, video_path="/data/videos/1/youtube_shorts.mp4"):
    db = session_factory()
    try:
        reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        cut = models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
            status=status, video_path=video_path,
        )
        db.add(cut)
        db.commit()
        return cut.id
    finally:
        db.close()


def test_publish_requires_a_rendered_video(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.approved, video_path=None)
    resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 422


def test_publish_rejects_wrong_status(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.in_review)
    resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 409


def test_publish_rejects_already_publishing(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.publishing)
    resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 409


def test_publish_from_approved_enqueues_job(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.approved)
    with patch("api.routers.cuts.publish_cut") as mock_task:
        resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 200
    mock_task.delay.assert_called_once()

    db = client._session_factory()
    cut = db.get(models.Cut, cut_id)
    assert cut.status.value == "publishing"
    db.close()


def test_publish_retries_from_failed_via_approved(client):
    """A publish failure retries straight from 'approved', no re-render needed."""
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    with patch("api.routers.cuts.publish_cut") as mock_task:
        resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 200
    mock_task.delay.assert_called_once()

    db = client._session_factory()
    cut = db.get(models.Cut, cut_id)
    assert cut.status.value == "publishing"
    db.close()


def test_publish_status_fragment_404_for_missing_job(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.publishing)
    resp = client.get(f"/api/cuts/{cut_id}/publish-status", params={"job_id": 999999})
    assert resp.status_code == 404


def _make_job(session_factory, cut_id, *, status, error=None, progress=0):
    db = session_factory()
    try:
        cut = db.get(models.Cut, cut_id)
        job = models.Job(
            type=models.JobType.publish, reel_id=cut.reel_id, cut_id=cut_id,
            status=status, error=error, progress=progress,
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def test_publish_status_mid_retry_backoff_has_no_broken_retry_button(client):
    """A transient failure resets job.status to "pending" (not "failed") while it
    retries automatically. The fragment must not offer a "Retry publish" button in
    that window — cut.status is still "publishing", so clicking it would 409 and,
    since htmx doesn't swap on a non-2xx response, silently do nothing.
    """
    cut_id = _make_cut(client._session_factory, models.CutStatus.publishing)
    job_id = _make_job(
        client._session_factory, cut_id,
        status=models.JobStatus.pending,
        error="transient failure, retry 1: network down",
    )
    resp = client.get(f"/api/cuts/{cut_id}/publish-status", params={"job_id": job_id})
    assert resp.status_code == 200
    assert "Retry publish" not in resp.text
    assert "retrying automatically" in resp.text.lower()


def test_publish_status_terminal_failure_shows_retry_button(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.publishing)
    job_id = _make_job(
        client._session_factory, cut_id,
        status=models.JobStatus.failed,
        error="No connected youtube account",
    )
    resp = client.get(f"/api/cuts/{cut_id}/publish-status", params={"job_id": job_id})
    assert resp.status_code == 200
    assert "Retry publish" in resp.text
    assert "No connected youtube account" in resp.text
