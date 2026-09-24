"""Tests for api/routers/insights.py — GET /api/insights and PerformanceNote CRUD."""
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


def _make_reel_with_metrics(session_factory, *, quality_score, views, niche="football"):
    db = session_factory()
    try:
        reel = models.Reel(context="A test reel", niche=niche, status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        db.add(models.Job(
            type=models.JobType.generate, reel_id=reel.id,
            status=models.JobStatus.done, meta={"quality_score": quality_score},
        ))
        db.add(models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
            status=models.CutStatus.published, views=views,
        ))
        db.commit()
        return reel.id
    finally:
        db.close()


# ── page rendering ───────────────────────────────────────────────────────────

def test_insights_page_renders_with_no_data(client):
    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert "Not enough published, measured reels yet" in resp.text


def test_insights_page_renders_with_some_but_insufficient_data(client):
    for i in range(3):
        _make_reel_with_metrics(client._session_factory, quality_score=50 + i, views=100 * (i + 1))

    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert "n = 3 of 5 needed" in resp.text


def test_insights_page_renders_with_enough_data(client):
    qualities = [10, 20, 30, 40, 50]
    views = [2, 4, 5, 4, 5]
    for q, v in zip(qualities, views):
        _make_reel_with_metrics(client._session_factory, quality_score=q, views=v)

    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert "correlation" in resp.text.lower()
    assert "(n = 5)" in resp.text


# ── PerformanceNote CRUD round-trip ──────────────────────────────────────────

def test_create_note_round_trips_through_db(client):
    resp = client.post("/api/insights/notes", data={"text": "Direct-question hooks win."})
    assert resp.status_code == 200
    assert "Direct-question hooks win." in resp.text

    db = client._session_factory()
    try:
        notes = db.query(models.PerformanceNote).all()
        assert len(notes) == 1
        assert notes[0].text == "Direct-question hooks win."
        assert notes[0].active is True
    finally:
        db.close()


def test_create_note_with_blank_text_is_not_saved(client):
    resp = client.post("/api/insights/notes", data={"text": "   "})
    assert resp.status_code == 200

    db = client._session_factory()
    try:
        assert db.query(models.PerformanceNote).count() == 0
    finally:
        db.close()


def test_toggle_note_flips_active(client):
    db = client._session_factory()
    try:
        note = models.PerformanceNote(text="Some note", active=True)
        db.add(note)
        db.commit()
        note_id = note.id
    finally:
        db.close()

    resp = client.post(f"/api/insights/notes/{note_id}/toggle")
    assert resp.status_code == 200

    db = client._session_factory()
    try:
        refreshed = db.get(models.PerformanceNote, note_id)
        assert refreshed.active is False
    finally:
        db.close()

    # Toggle back
    resp = client.post(f"/api/insights/notes/{note_id}/toggle")
    assert resp.status_code == 200
    db = client._session_factory()
    try:
        refreshed = db.get(models.PerformanceNote, note_id)
        assert refreshed.active is True
    finally:
        db.close()


def test_toggle_unknown_note_404s(client):
    resp = client.post("/api/insights/notes/999/toggle")
    assert resp.status_code == 404


def test_delete_note_removes_it_and_no_longer_appears_in_fresh_get(client):
    db = client._session_factory()
    try:
        note = models.PerformanceNote(text="Delete me", active=True)
        db.add(note)
        db.commit()
        note_id = note.id
    finally:
        db.close()

    resp = client.get("/api/insights")
    assert "Delete me" in resp.text

    resp = client.request("DELETE", f"/api/insights/notes/{note_id}")
    assert resp.status_code == 200
    assert "Delete me" not in resp.text

    db = client._session_factory()
    try:
        assert db.get(models.PerformanceNote, note_id) is None
    finally:
        db.close()

    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert "Delete me" not in resp.text


def test_delete_unknown_note_is_a_no_op_not_an_error(client):
    resp = client.request("DELETE", "/api/insights/notes/999")
    assert resp.status_code == 200
