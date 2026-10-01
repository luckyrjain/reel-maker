"""Tests for the hook-variant and thumbnail-candidate routes.

POST /api/cuts/{id}/hook-variant, POST /api/cuts/{id}/thumbnail,
GET /api/cuts/{id}/thumbnail/{index}.
"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.db import get_db
from api.main import app
from api.routers.cuts import _resolve_within_video_store

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
              thumbnail_path=None, subtitle_path=None, video_path=None):
    db = session_factory()
    try:
        reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        cut = models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=status,
            guide=guide, hook_variants=hook_variants,
            thumbnail_candidates=thumbnail_candidates, thumbnail_path=thumbnail_path,
            subtitle_path=subtitle_path, video_path=video_path,
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


def test_stream_thumbnail_serves_the_requested_index_not_always_the_first(client, tmp_path):
    """Review-round regression: nothing else in this file exercises a multi-candidate list,
    so a bug that always served thumbnail_candidates[0] regardless of the requested index
    would have shipped silently."""
    thumb0 = tmp_path / "1" / "thumb_0.jpg"
    thumb1 = tmp_path / "1" / "thumb_1.jpg"
    thumb0.parent.mkdir(parents=True)
    thumb0.write_bytes(b"first-candidate-bytes")
    thumb1.write_bytes(b"second-candidate-bytes")
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review,
        thumbnail_candidates=[str(thumb0), str(thumb1)],
    )
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resp = client.get(f"/api/cuts/{cut_id}/thumbnail/1")
    assert resp.status_code == 200
    assert resp.content == b"second-candidate-bytes"


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


# ── GET /cuts/{id}/subtitles ──────────────────────────────────────────────────

def test_stream_subtitles_serves_the_srt_file(client, tmp_path):
    srt = tmp_path / "1" / "youtube_shorts.srt"
    srt.parent.mkdir(parents=True)
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review, subtitle_path=str(srt),
    )
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resp = client.get(f"/api/cuts/{cut_id}/subtitles")
    assert resp.status_code == 200
    assert resp.content == b"1\n00:00:00,000 --> 00:00:01,000\nHello.\n"
    assert resp.headers["content-type"].startswith("application/x-subrip")


def test_stream_subtitles_404s_when_subtitle_path_unset(client, tmp_path):
    cut_id = _make_cut(client._session_factory, models.CutStatus.in_review, subtitle_path=None)
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resp = client.get(f"/api/cuts/{cut_id}/subtitles")
    assert resp.status_code == 404


def test_stream_subtitles_404s_for_a_missing_cut(client, tmp_path):
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resp = client.get("/api/cuts/999999/subtitles")
    assert resp.status_code == 404


def test_stream_subtitles_rejects_a_path_outside_the_video_store(client, tmp_path):
    """Same path-guard as stream_video/stream_thumbnail — subtitle_path can never
    point outside VIDEO_STORE_DIR."""
    outside = tmp_path.parent / "outside_captions.srt"
    outside.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review, subtitle_path=str(outside),
    )
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    with patch("api.routers.cuts.settings.video_store_dir", str(store_dir)):
        resp = client.get(f"/api/cuts/{cut_id}/subtitles")
    assert resp.status_code == 403


# ── _resolve_within_video_store (CAR candidate 1, improve-codebase-architecture review) ──
#
# Direct tests of the shared path-traversal guard extracted from stream_video/stream_
# subtitles/stream_thumbnail's three previously-independent copies — see
# api/routers/cuts.py and docs/specs/2026-09-video-store-guard-module-design.md.

def test_resolve_within_video_store_returns_the_resolved_path_when_inside(tmp_path):
    inside = tmp_path / "1" / "file.mp4"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resolved = _resolve_within_video_store(str(inside))
    assert resolved == inside.resolve()


def test_resolve_within_video_store_rejects_a_path_outside(tmp_path):
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"x")
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    with patch("api.routers.cuts.settings.video_store_dir", str(store_dir)):
        with pytest.raises(HTTPException) as exc_info:
            _resolve_within_video_store(str(outside))
    assert exc_info.value.status_code == 403


# ── GET /cuts/{id}/video ──────────────────────────────────────────────────────
#
# Previously had zero direct test coverage at all (independent-review-caught gap,
# closed alongside the guard extraction above).

def test_stream_video_serves_the_mp4_file(client, tmp_path):
    video = tmp_path / "1" / "youtube_shorts.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake-mp4-bytes")
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review, video_path=str(video),
    )
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resp = client.get(f"/api/cuts/{cut_id}/video")
    assert resp.status_code == 200
    assert resp.content == b"fake-mp4-bytes"
    assert resp.headers["content-type"].startswith("video/mp4")


def test_stream_video_404s_when_video_path_unset(client, tmp_path):
    cut_id = _make_cut(client._session_factory, models.CutStatus.in_review, video_path=None)
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resp = client.get(f"/api/cuts/{cut_id}/video")
    assert resp.status_code == 404


def test_stream_video_404s_for_a_missing_cut(client, tmp_path):
    with patch("api.routers.cuts.settings.video_store_dir", str(tmp_path)):
        resp = client.get("/api/cuts/999999/video")
    assert resp.status_code == 404


def test_stream_video_rejects_a_path_outside_the_video_store(client, tmp_path):
    """Same path-guard as stream_subtitles/stream_thumbnail — video_path can never
    point outside VIDEO_STORE_DIR."""
    outside = tmp_path.parent / "outside_video.mp4"
    outside.write_bytes(b"nope")
    cut_id = _make_cut(
        client._session_factory, models.CutStatus.in_review, video_path=str(outside),
    )
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    with patch("api.routers.cuts.settings.video_store_dir", str(store_dir)):
        resp = client.get(f"/api/cuts/{cut_id}/video")
    assert resp.status_code == 403
