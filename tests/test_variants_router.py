"""Tests for the Cut variant-picker routes — choosing among already-rendered options.

POST /api/cuts/{id}/hook-variant, POST /api/cuts/{id}/thumbnail.

The 3 GET file-streaming routes (video/subtitles/thumbnail) and their shared
_resolve_within_video_store() guard moved to tests/test_cut_media.py, mirroring the
api/routers/cuts.py -> cut_media.py production split (candidate 2 of the
improve-codebase-architecture review) — see
docs/specs/2026-09-cut-media-router-split-module-design.md. These two routes stay here:
both call cuts.py's shared _cut_card() template helper and are gated on cut.status ==
"in_review", the same state-machine-shaped precondition as the render/approve/publish/
update routes in test_cuts_publish_router.py, not the streaming routes' "serve raw bytes"
shape.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from api import models
from api.db import get_db
from api.main import app

_GUIDE = {
    "platform": "youtube_shorts",
    "target_length_s": 30,
    "caption": "cap",
    "hashtags": list("abcde"),
    "beats": [
        {"index": 0, "type": "hook", "duration_s": 3, "visual_direction": "v",
         "on_screen_text": ["Old hook"], "vo_script": "The old hook line."},
        {"index": 1, "type": "body", "duration_s": 5, "visual_direction": "v",
         "on_screen_text": ["Body"], "vo_script": "Body text."},
        {"index": 2, "type": "cta", "duration_s": 3, "visual_direction": "v",
         "on_screen_text": ["CTA"], "vo_script": "Follow for more."},
    ],
}


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


def _make_cut(session_factory, status, *, guide=None, hook_variants=None, thumbnail_candidates=None,
              thumbnail_path=None):
    db = session_factory()
    try:
        reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        cut = models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=status,
            guide=guide, hook_variants=hook_variants,
            thumbnail_candidates=thumbnail_candidates, thumbnail_path=thumbnail_path,
        )
        db.add(cut)
        db.commit()
        return cut.id
    finally:
        db.close()


# ── POST /cuts/{id}/hook-variant ─────────────────────────────────────────────

def test_choose_hook_variant_swaps_beat_zero_and_rederives_on_screen_text(client):
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review,
        guide=_GUIDE, hook_variants=["You won't believe this football stat."],
    )
    resp = client.post(f"/api/cuts/{cut_id}/hook-variant", data={"index": "0"})
    assert resp.status_code == 200

    db = client._session_factory()
    cut = db.get(models.Cut, cut_id)
    assert cut.guide["beats"][0]["vo_script"] == "You won't believe this football stat."
    assert cut.guide["beats"][0]["on_screen_text"] != ["Old hook"]
    # untouched beats stay untouched
    assert cut.guide["beats"][1]["vo_script"] == "Body text."
    db.close()


def test_choose_hook_variant_rejects_wrong_status(client):
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.draft,
        guide=_GUIDE, hook_variants=["alt hook"],
    )
    resp = client.post(f"/api/cuts/{cut_id}/hook-variant", data={"index": "0"})
    assert resp.status_code == 409


def test_choose_hook_variant_rejects_out_of_range_index(client):
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review,
        guide=_GUIDE, hook_variants=["alt hook"],
    )
    resp = client.post(f"/api/cuts/{cut_id}/hook-variant", data={"index": "5"})
    assert resp.status_code == 422


def test_choose_hook_variant_rejects_when_no_variants_exist(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.in_review, guide=_GUIDE, hook_variants=None)
    resp = client.post(f"/api/cuts/{cut_id}/hook-variant", data={"index": "0"})
    assert resp.status_code == 422


# ── POST /cuts/{id}/thumbnail ────────────────────────────────────────────────

def test_choose_thumbnail_sets_the_chosen_candidate(client):
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review,
        thumbnail_candidates=["/data/videos/1/thumb.jpg", "/data/videos/1/thumb_1.jpg"],
        thumbnail_path="/data/videos/1/thumb.jpg",
    )
    resp = client.post(f"/api/cuts/{cut_id}/thumbnail", data={"index": "1"})
    assert resp.status_code == 200

    db = client._session_factory()
    cut = db.get(models.Cut, cut_id)
    assert cut.thumbnail_path == "/data/videos/1/thumb_1.jpg"
    db.close()


def test_choose_thumbnail_rejects_wrong_status(client):
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.draft,
        thumbnail_candidates=["/data/videos/1/thumb.jpg"],
    )
    resp = client.post(f"/api/cuts/{cut_id}/thumbnail", data={"index": "0"})
    assert resp.status_code == 409


def test_choose_thumbnail_rejects_out_of_range_index(client):
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review,
        thumbnail_candidates=["/data/videos/1/thumb.jpg"],
    )
    resp = client.post(f"/api/cuts/{cut_id}/thumbnail", data={"index": "3"})
    assert resp.status_code == 422


def test_choose_thumbnail_rejects_when_no_candidates_exist(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.in_review, thumbnail_candidates=None)
    resp = client.post(f"/api/cuts/{cut_id}/thumbnail", data={"index": "0"})
    assert resp.status_code == 422


