"""Tests for the hook-variant and thumbnail-candidate routes.

POST /api/cuts/{id}/hook-variant, POST /api/cuts/{id}/thumbnail,
GET /api/cuts/{id}/thumbnail/{index}.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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


# ── GET /cuts/{id}/thumbnail/{index} ─────────────────────────────────────────

def test_stream_thumbnail_serves_the_candidate_file(client, tmp_path):
    thumb = tmp_path / "1" / "youtube_shorts_thumb.jpg"
    thumb.parent.mkdir(parents=True)
    thumb.write_bytes(b"fake-jpeg-bytes")
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review,
        thumbnail_candidates=[str(thumb)], thumbnail_path=str(thumb),
    )
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resp = client.get(f"/api/cuts/{cut_id}/thumbnail/0")
    assert resp.status_code == 200
    assert resp.content == b"fake-jpeg-bytes"


def test_stream_thumbnail_404s_on_out_of_range_index(client, tmp_path):
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review,
        thumbnail_candidates=["/data/videos/1/thumb.jpg"],
    )
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resp = client.get(f"/api/cuts/{cut_id}/thumbnail/9")
    assert resp.status_code == 404


def test_stream_thumbnail_rejects_a_path_outside_the_video_store(client, tmp_path):
    """Same path-guard as stream_video — a candidate path can never point outside VIDEO_STORE_DIR."""
    outside = tmp_path.parent / "outside_thumb.jpg"
    outside.write_bytes(b"nope")
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review,
        thumbnail_candidates=[str(outside)],
    )
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    with patch("api.routers.cuts.settings.video_store_dir", str(store_dir)):
        resp = client.get(f"/api/cuts/{cut_id}/thumbnail/0")
    assert resp.status_code == 403
