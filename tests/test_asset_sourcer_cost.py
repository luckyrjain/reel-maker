"""Tests for HuggingFace asset-generation cost tracking in asset_sourcer.py.

Covers two things: HuggingFace*Source.last_call_was_generated correctly
distinguishes a real API call from a cache hit, and resolve_beat_assets only
charges StageEvent.cost_usd when a real call happened.
"""
from unittest.mock import MagicMock, patch


from api import models
from engine.render.asset_sourcer import (
    HuggingFaceImageSource,
    HuggingFaceVideoSource,
    resolve_beat_assets,
)


# ── last_call_was_generated ──────────────────────────────────────────────────

def test_hf_image_marks_generated_on_real_api_call(tmp_path):
    source = HuggingFaceImageSource(api_key="key", model="m", store_dir=tmp_path)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": "image/png"}
    resp.content = b"fake-png-bytes"
    with patch("engine.render.asset_sourcer.httpx.post", return_value=resp):
        result = source.generate("a prompt")
    assert result is not None
    assert source.last_call_was_generated is True


def test_hf_image_does_not_mark_generated_on_cache_hit(tmp_path):
    source = HuggingFaceImageSource(api_key="key", model="m", store_dir=tmp_path)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": "image/png"}
    resp.content = b"fake-png-bytes"
    with patch("engine.render.asset_sourcer.httpx.post", return_value=resp) as mock_post:
        source.generate("a prompt")  # first call — real API hit, caches the file
        assert source.last_call_was_generated is True
        source.generate("a prompt")  # second call — same fingerprint, cache hit
    assert source.last_call_was_generated is False
    assert mock_post.call_count == 1


def test_hf_image_no_api_key_never_marks_generated(tmp_path):
    source = HuggingFaceImageSource(api_key="", model="m", store_dir=tmp_path)
    assert source.generate("a prompt") is None
    assert source.last_call_was_generated is False


def test_hf_video_marks_generated_on_real_api_call(tmp_path):
    source = HuggingFaceVideoSource(api_key="key", model="m", store_dir=tmp_path)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": "video/mp4"}
    resp.content = b"fake-mp4-bytes"
    with patch("engine.render.asset_sourcer.httpx.post", return_value=resp):
        result = source.generate("a prompt")
    assert result is not None
    assert source.last_call_was_generated is True


def test_hf_video_does_not_mark_generated_on_cache_hit(tmp_path):
    source = HuggingFaceVideoSource(api_key="key", model="m", store_dir=tmp_path)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": "video/mp4"}
    resp.content = b"fake-mp4-bytes"
    with patch("engine.render.asset_sourcer.httpx.post", return_value=resp) as mock_post:
        source.generate("a prompt")
        assert source.last_call_was_generated is True
        source.generate("a prompt")
    assert source.last_call_was_generated is False
    assert mock_post.call_count == 1


# ── resolve_beat_assets cost recording ───────────────────────────────────────

class _NoneSourcer:
    def search(self, query, min_duration_s):
        return None


class _FakeHFSource:
    """Stands in for HuggingFace*Source with a scriptable last_call_was_generated."""

    def __init__(self, result, was_generated, api_key="key"):
        self._result = result
        self.last_call_was_generated = was_generated
        self.api_key = api_key

    def generate(self, prompt):
        return self._result


def _asset_result(source="huggingface", duration_s=0.0):
    from engine.render.asset_sourcer import SourcedAsset
    return SourcedAsset(
        source=source, source_ref="fp123", local_path="/tmp/x.png",
        license_str="generated", safe_to_publish=True, duration_s=duration_s,
    )


def test_real_hf_image_call_records_cost(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db.add(reel)
    db.commit()

    hf = _FakeHFSource(_asset_result(), was_generated=True)
    with patch("engine.render.asset_sourcer.settings.huggingface_price_per_image", 0.003):
        resolve_beat_assets(
            db, "a query", 5.0, _NoneSourcer(), wiki=None, hf_video=None, hf=hf, reel_id=reel.id,
        )

    events = db.query(models.StageEvent).filter(models.StageEvent.reel_id == reel.id).all()
    assert len(events) == 1
    assert events[0].stage == "asset_hf_image"
    assert events[0].cost_usd == 0.003
    assert events[0].detail["cache_hit"] is False


def test_cached_hf_image_call_records_zero_cost(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db.add(reel)
    db.commit()

    hf = _FakeHFSource(_asset_result(), was_generated=False)
    with patch("engine.render.asset_sourcer.settings.huggingface_price_per_image", 0.003):
        resolve_beat_assets(
            db, "a query", 5.0, _NoneSourcer(), wiki=None, hf_video=None, hf=hf, reel_id=reel.id,
        )

    events = db.query(models.StageEvent).filter(models.StageEvent.reel_id == reel.id).all()
    assert len(events) == 1
    assert events[0].cost_usd in (None, 0.0), "a cache hit must not be billed again"
    assert events[0].detail["cache_hit"] is True


def test_real_hf_video_call_records_cost_scaled_by_duration(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db.add(reel)
    db.commit()

    hf_video = _FakeHFSource(_asset_result(source="huggingface_video", duration_s=4.0), was_generated=True)
    with patch("engine.render.asset_sourcer.settings.huggingface_price_per_video_second", 0.01):
        resolve_beat_assets(
            db, "a query", 5.0, _NoneSourcer(), wiki=None, hf_video=hf_video, hf=None, reel_id=reel.id,
        )

    events = db.query(models.StageEvent).filter(models.StageEvent.reel_id == reel.id).all()
    assert len(events) == 1
    assert events[0].stage == "asset_hf_video"
    assert round(events[0].cost_usd, 4) == 0.04


def test_no_reel_id_skips_cost_tracking_entirely(db_session):
    db = db_session
    hf = _FakeHFSource(_asset_result(), was_generated=True)
    # No reel row is even created — reel_id=None must mean no StageEvent query/write happens.
    resolve_beat_assets(db, "a query", 5.0, _NoneSourcer(), wiki=None, hf_video=None, hf=hf, reel_id=None)
    assert db.query(models.StageEvent).count() == 0


def test_no_api_key_source_skips_stage_event_entirely(db_session):
    """hf_video/hf are always constructed by render_cut regardless of whether
    HUGGINGFACE_API_KEY is set — generate() is then a guaranteed no-op that
    never makes a network call. record_stage must not wrap that no-op, or
    every beat that falls through to this branch writes a meaningless
    StageEvent row (DB insert + commit for a call that was never attempted)."""
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db.add(reel)
    db.commit()

    hf = _FakeHFSource(None, was_generated=False, api_key="")
    hf_video = _FakeHFSource(None, was_generated=False, api_key="")
    resolve_beat_assets(
        db, "a query", 5.0, _NoneSourcer(), wiki=None, hf_video=hf_video, hf=hf, reel_id=reel.id,
    )

    assert db.query(models.StageEvent).filter(models.StageEvent.reel_id == reel.id).count() == 0
