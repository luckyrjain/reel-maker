"""Tests for GET /api/jobs/{job_id}, the one JSON endpoint in the app.

Nothing in the UI calls it (the pages poll the HTML fragments in reels.py), so before this file
it had no test at all: a changed field name, a leaked column or a lost 404 would have shipped
silently to whatever external client reads it. See docs/specs/2026-11-jobs-endpoint-tests-design.md.
"""
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
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
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


def _make_job(session_factory, *, status=models.JobStatus.pending, progress=0, error=None,
              meta=None, reel_count=0, job_type=models.JobType.generate):
    """Insert a job (after `reel_count` filler reels, so reel ids and job ids differ) and return its id."""
    db = session_factory()
    try:
        for _ in range(reel_count):
            db.add(models.Reel(context="filler", status=models.ReelStatus.draft))
        db.flush()
        reel = models.Reel(context="x", status=models.ReelStatus.generating)
        db.add(reel)
        db.flush()
        job = models.Job(
            type=job_type, reel_id=reel.id, status=status, progress=progress,
            error=error, meta=meta if meta is not None else {},
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def test_returns_exactly_id_status_progress_and_error(client):
    job_id = _make_job(client._session_factory, status=models.JobStatus.running, progress=70)

    # No redirect is followed: a route moved to a trailing-slash path would answer the old URL with a 307.
    resp = client.get(f"/api/jobs/{job_id}", follow_redirects=False)

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {"id": job_id, "status": "running", "progress": 70, "error": None}
    # 70.0 == 70 in Python, so equality alone would let a float through; strict clients reject it.
    assert type(resp.json()["progress"]) is int and type(resp.json()["id"]) is int


@pytest.mark.parametrize("status", list(models.JobStatus))
def test_every_status_is_serialized_as_its_plain_string(client, status):
    job_id = _make_job(client._session_factory, status=status)

    assert client.get(f"/api/jobs/{job_id}").json()["status"] == status.value


def test_progress_zero_is_returned_not_dropped(client):
    job_id = _make_job(client._session_factory, progress=0)

    assert client.get(f"/api/jobs/{job_id}").json()["progress"] == 0


def test_a_failed_jobs_error_text_is_returned_verbatim(client):
    """The longest error `job_task` stores is 2000 characters; it must come back whole, not cut."""
    # Multi-line on purpose: `_error_text` keeps interior newlines, so they must survive the response too.
    message = ("LLM returned invalid JSON:\nExpecting ',' delimiter — résumé ✓ " * 40)[:2000]
    assert "\n" in message
    assert len(message) == 2000
    job_id = _make_job(client._session_factory, status=models.JobStatus.failed, error=message)

    body = client.get(f"/api/jobs/{job_id}").json()

    assert body["status"] == "failed"
    assert body["error"] == message


def test_no_internal_column_is_exposed(client):
    """meta can hold scores and note ids, and the row carries fencing/reaper counters; none is part of the shape."""
    job_id = _make_job(
        client._session_factory, status=models.JobStatus.done, progress=100,
        meta={"quality_score": 77, "performance_note_ids": [3], "secret_marker": "do-not-leak"},
    )

    resp = client.get(f"/api/jobs/{job_id}")

    assert set(resp.json()) == {"id", "status", "progress", "error"}
    assert "do-not-leak" not in resp.text and "quality_score" not in resp.text


def test_the_requested_job_is_the_one_returned(client):
    """Three jobs of different types and states, and ids that differ from their reels' ids.

    The running job carries an error on purpose: a retried job is reset to pending with a
    "transient failure, retry N" error and the next claim does not clear it, so a job that is not
    `failed` can still have one, and it has to be shown.
    """
    first = _make_job(client._session_factory, status=models.JobStatus.done, progress=100, reel_count=2,
                      job_type=models.JobType.enrich)
    second = _make_job(client._session_factory, status=models.JobStatus.running, progress=40, reel_count=3,
                       error="transient failure, retry 1: ReadTimeout", job_type=models.JobType.render)
    third = _make_job(client._session_factory, status=models.JobStatus.failed, progress=10, error="boom",
                      job_type=models.JobType.publish)
    assert len({first, second, third}) == 3

    assert client.get(f"/api/jobs/{first}").json() == {
        "id": first, "status": "done", "progress": 100, "error": None,
    }
    assert client.get(f"/api/jobs/{second}").json() == {
        "id": second, "status": "running", "progress": 40, "error": "transient failure, retry 1: ReadTimeout",
    }
    assert client.get(f"/api/jobs/{third}").json() == {
        "id": third, "status": "failed", "progress": 10, "error": "boom",
    }


def test_an_unknown_job_is_a_404_with_a_message(client):
    _make_job(client._session_factory)

    resp = client.get("/api/jobs/9999")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Job not found"}


def test_a_reel_id_is_not_mistaken_for_a_job_id(client):
    """Job lookup is by job id: with 3 filler reels the job's reel has id 4 but the job itself has id 1."""
    job_id = _make_job(client._session_factory, reel_count=3)

    assert job_id == 1
    assert client.get("/api/jobs/4").status_code == 404
    assert client.get(f"/api/jobs/{job_id}").status_code == 200


@pytest.mark.parametrize("bad_id", ["abc", "1.5", "1e3"])
def test_a_non_integer_id_is_a_422(client, bad_id):
    assert client.get(f"/api/jobs/{bad_id}").status_code == 422


@pytest.mark.parametrize("missing_id", ["0", "-1"])
def test_zero_and_negative_ids_are_a_404(client, missing_id):
    _make_job(client._session_factory)

    assert client.get(f"/api/jobs/{missing_id}").status_code == 404


def test_reading_a_job_changes_nothing(client):
    job_id = _make_job(client._session_factory, status=models.JobStatus.running, progress=55)
    db = client._session_factory()
    try:
        before = db.get(models.Job, job_id)
        snapshot = (before.status, before.progress, before.error, before.updated_at, before.claim_token)
    finally:
        db.close()

    client.get(f"/api/jobs/{job_id}")
    client.get(f"/api/jobs/{job_id}")

    db = client._session_factory()
    try:
        after = db.get(models.Job, job_id)
        assert (after.status, after.progress, after.error, after.updated_at, after.claim_token) == snapshot
    finally:
        db.close()


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_only_get_is_allowed(client, method):
    job_id = _make_job(client._session_factory)

    assert getattr(client, method)(f"/api/jobs/{job_id}").status_code == 405
