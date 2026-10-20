"""One wall-clock budget across a whole render's asset sourcing (engine/render/asset_sourcer.py).

Each search() has its own budget (900 s Pexels / 240 s Wikipedia), but a render resolves assets for every
beat, so those add up to hours. `asset_budget(seconds)` sets one deadline for everything inside it
(`render_cut` wraps each beat's `resolve_or_reuse()` in `asset_budget(RENDER_ASSET_BUDGET_S - asset
time spent so far)`, so TTS time is not charged): each search's budget is clipped to what is left, no further
download starts once it is spent, and the paid HuggingFace tiers are not called. A cached file is still
served. Outside the context nothing changes, and no extra clock reading is made.
"""
from unittest.mock import MagicMock, patch

import pytest

from engine.render import asset_sourcer as AS
from tests.test_sourcer_download_guards import PEXELS_OK, WIKI_OK, _Resp, _Stream, _pexels, _wiki
from tests.test_sourcer_selection import _summary, _video, _vf


def _clock(monkeypatch, now):
    box = [now]
    monkeypatch.setattr(AS, "_monotonic", lambda: box[0])
    return box


# ── the context manager ───────────────────────────────────────────────────────────────────────

def test_unset_by_default():
    assert AS._asset_deadline_at.get() is None and AS._asset_budget_spent() is False


def test_budget_sets_a_deadline_and_restores_it(monkeypatch):
    _clock(monkeypatch, 100.0)
    with AS.asset_budget(50.0):
        assert AS._asset_deadline_at.get() == 150.0
    assert AS._asset_deadline_at.get() is None


def test_budget_is_restored_after_an_exception(monkeypatch):
    _clock(monkeypatch, 100.0)
    with pytest.raises(RuntimeError):
        with AS.asset_budget(50.0):
            raise RuntimeError("boom")
    assert AS._asset_deadline_at.get() is None


def test_a_nested_budget_can_only_tighten(monkeypatch):
    _clock(monkeypatch, 0.0)
    with AS.asset_budget(100.0):
        with AS.asset_budget(500.0):
            assert AS._asset_deadline_at.get() == 100.0
        with AS.asset_budget(10.0):
            assert AS._asset_deadline_at.get() == 10.0
        assert AS._asset_deadline_at.get() == 100.0


def test_spent_is_true_at_and_after_the_deadline_only(monkeypatch):
    box = _clock(monkeypatch, 0.0)
    with AS.asset_budget(10.0):
        box[0] = 9.99
        assert AS._asset_budget_spent() is False
        box[0] = 10.0
        assert AS._asset_budget_spent() is True
        box[0] = 11.0
        assert AS._asset_budget_spent() is True


def test_clip_is_the_smaller_of_the_two_and_a_no_op_when_unset(monkeypatch):
    _clock(monkeypatch, 0.0)
    assert AS._clip_to_asset_budget(500.0) == 500.0
    with AS.asset_budget(100.0):
        assert AS._clip_to_asset_budget(500.0) == 100.0
        assert AS._clip_to_asset_budget(40.0) == 40.0


def test_the_render_budget_is_twenty_minutes():
    assert AS.RENDER_ASSET_BUDGET_S == 1200.0


def test_outside_a_budget_no_extra_clock_reading_is_made(monkeypatch):
    calls = []
    monkeypatch.setattr(AS, "_monotonic", lambda: calls.append(1) or 0.0)
    assert AS._clip_to_asset_budget(5.0) == 5.0 and AS._asset_budget_spent() is False
    assert calls == []


# ── Pexels ─────────────────────────────────────────────────────────────────────────────────────

def _hit(link=PEXELS_OK):
    return [_video(1, [_vf(1080, 1920, link=link)])]


def test_pexels_a_spent_budget_starts_no_download(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    stream = _Stream({PEXELS_OK: _Resp()})
    with AS.asset_budget(-1.0):
        assert _pexels(tmp_path, _hit(), stream) is None
    assert stream.requests == [] and list(tmp_path.iterdir()) == []


def test_pexels_a_spent_budget_still_serves_a_cached_file(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    (tmp_path / "pexels_1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8)
    stream = _Stream({})
    with AS.asset_budget(-1.0):
        result = _pexels(tmp_path, _hit(), stream)
    assert result is not None and stream.requests == []


def test_pexels_the_download_budget_is_what_is_left_of_the_render_budget(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    stream = _Stream({PEXELS_OK: _Resp()})
    with AS.asset_budget(5.0):
        assert _pexels(tmp_path, _hit(), stream) is not None
    assert stream.requests[0][1]["timeout"] == pytest.approx(5.0 + AS._HOP_TIMEOUT_GRACE_S)


def test_pexels_without_a_budget_the_search_budget_applies(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    stream = _Stream({PEXELS_OK: _Resp()})
    assert _pexels(tmp_path, _hit(), stream) is not None
    assert stream.requests[0][1]["timeout"] == 120.0           # the configured timeout, unclamped


# ── Wikipedia ──────────────────────────────────────────────────────────────────────────────────

def test_wikipedia_a_spent_budget_starts_no_download(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    stream = _Stream({WIKI_OK: _Resp()})
    with AS.asset_budget(-1.0):
        assert _wiki(tmp_path, _summary(thumbnail=None), stream) is None
    assert stream.requests == []


def test_wikipedia_a_spent_budget_still_serves_a_cached_image(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    (tmp_path / "wiki_123.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
    stream = _Stream({})
    with AS.asset_budget(-1.0):
        result = _wiki(tmp_path, _summary(thumbnail=None), stream)
    assert result is not None and stream.requests == []


def test_wikipedia_the_download_budget_is_what_is_left_of_the_render_budget(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    stream = _Stream({WIKI_OK: _Resp()})
    with AS.asset_budget(7.0):
        assert _wiki(tmp_path, _summary(thumbnail=None), stream) is not None
    assert stream.requests[0][1]["timeout"] == pytest.approx(7.0 + AS._HOP_TIMEOUT_GRACE_S)


# ── the paid HuggingFace tiers ─────────────────────────────────────────────────────────────────
# The skip lives in generate(), AFTER the on-disk cache lookup: a cached (already paid for) asset is
# still served once the budget is spent, only a call to the paid API is refused.

import hashlib

_HF_SUFFIX = ", portrait orientation, vertical format, cinematic, high quality"


def _no_result_sourcer():
    s = MagicMock()
    s.search.return_value = None
    return s


def _hf_image_cache_file(tmp_path, query="q"):
    return tmp_path / f"hf_{hashlib.sha256((query + _HF_SUFFIX).encode()).hexdigest()[:16]}.png"


def _hf_video_cache_file(tmp_path, query="q"):
    return tmp_path / f"hfvid_{hashlib.sha256((query + _HF_SUFFIX).encode()).hexdigest()[:16]}.mp4"


def _post_resp(content_type, content):
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.headers = {"content-type": content_type}
    r.content = content
    return r


def _png():
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (8, 8)).save(b, format="PNG")
    return b.getvalue()


def test_hf_image_not_generated_once_the_budget_is_spent(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    src = AS.HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{AS.__name__}.httpx.post") as post, AS.asset_budget(-1.0):
        assert src.generate("q") is None
    post.assert_not_called()
    assert src.last_call_was_generated is False and list(tmp_path.iterdir()) == []


def test_hf_video_not_generated_once_the_budget_is_spent(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    src = AS.HuggingFaceVideoSource("k", "org/m", tmp_path)
    with patch(f"{AS.__name__}.httpx.post") as post, AS.asset_budget(-1.0):
        assert src.generate("q") is None
    post.assert_not_called()


def test_hf_image_cached_asset_still_served_once_the_budget_is_spent(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    _hf_image_cache_file(tmp_path).write_bytes(_png())
    src = AS.HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{AS.__name__}.httpx.post") as post, AS.asset_budget(-1.0):
        result = src.generate("q")
    assert result is not None and result.local_path == _hf_image_cache_file(tmp_path)
    post.assert_not_called()
    assert src.last_call_was_generated is False                 # a cache hit is never billed


def test_hf_video_cached_asset_still_served_once_the_budget_is_spent(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    _hf_video_cache_file(tmp_path).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8)
    src = AS.HuggingFaceVideoSource("k", "org/m", tmp_path)
    with patch(f"{AS.__name__}.httpx.post") as post, AS.asset_budget(-1.0):
        assert src.generate("q") is not None
    post.assert_not_called()


def test_hf_image_is_generated_while_budget_remains(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    src = AS.HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{AS.__name__}.httpx.post", return_value=_post_resp("image/png", _png())) as post, \
            AS.asset_budget(100.0):
        assert src.generate("q") is not None
    post.assert_called_once()


def test_a_spent_budget_through_resolve_beat_assets_gives_a_black_frame_but_serves_a_cached_hf_image(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    hf = AS.HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{AS.__name__}.httpx.post") as post, AS.asset_budget(-1.0), \
            patch.object(AS, "_cache_asset", side_effect=lambda db, r, t: ("asset", r.local_path)):
        out = AS.resolve_beat_assets(MagicMock(), "q", 1.0, _no_result_sourcer(), None, None, hf, reel_id=None)
        assert out == [(None, None)]
        _hf_image_cache_file(tmp_path).write_bytes(_png())
        out2 = AS.resolve_beat_assets(MagicMock(), "q", 1.0, _no_result_sourcer(), None, None, hf, reel_id=None)
    post.assert_not_called()
    assert out2[0][1] == _hf_image_cache_file(tmp_path)


def test_the_skip_is_logged(tmp_path, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger=AS.__name__)
    _clock(monkeypatch, 0.0)
    with AS.asset_budget(-1.0):
        AS.HuggingFaceImageSource("k", "org/m", tmp_path).generate("q")
    assert "asset budget" in caplog.text


# ── round-2: a refused HF call is visible as such in its StageEvent and in the log ────────────────────

def test_the_video_skip_is_logged_too(tmp_path, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger=AS.__name__)
    _clock(monkeypatch, 0.0)
    with AS.asset_budget(-1.0):
        AS.HuggingFaceVideoSource("k", "org/m", tmp_path).generate("q")
    assert "Skipping HuggingFace video generation" in caplog.text


def test_last_call_skipped_is_set_only_by_a_refusal_and_reset_by_the_next_call(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    for src in (AS.HuggingFaceImageSource("k", "org/m", tmp_path), AS.HuggingFaceVideoSource("k", "org/m", tmp_path)):
        assert src.last_call_skipped is False
        with AS.asset_budget(-1.0):
            assert src.generate("q") is None
        assert src.last_call_skipped is True and src.last_call_was_generated is False
        with patch(f"{AS.__name__}.httpx.post", return_value=_post_resp("text/plain", b"")):
            src.generate("q")                                                 # budget not spent: a real attempt
        assert src.last_call_skipped is False


def test_a_cached_asset_served_after_the_budget_is_spent_is_not_marked_skipped(tmp_path, monkeypatch):
    _clock(monkeypatch, 0.0)
    _hf_image_cache_file(tmp_path).write_bytes(_png())
    src = AS.HuggingFaceImageSource("k", "org/m", tmp_path)
    with AS.asset_budget(-1.0):
        assert src.generate("q") is not None
    assert src.last_call_skipped is False


def test_a_skipped_gated_call_records_why_in_its_stage_event(db_session, tmp_path, monkeypatch):
    from api import models
    _clock(monkeypatch, 0.0)
    reel = models.Reel(context="c", niche="n")
    db_session.add(reel)
    db_session.flush()
    src = AS.HuggingFaceImageSource("k", "org/m", tmp_path)
    with AS.asset_budget(-1.0):
        assert AS._generate_gated_hf_asset(db_session, reel.id, "asset_hf_image", src, "q", lambda r: 1.0) is None
    ev = db_session.query(models.StageEvent).filter_by(stage="asset_hf_image").one()
    assert ev.detail == {"cache_hit": False, "skipped": "asset_budget_spent"} and not ev.cost_usd


def test_a_normal_gated_call_has_no_skipped_marker(db_session, tmp_path, monkeypatch):
    from api import models
    _clock(monkeypatch, 0.0)
    reel = models.Reel(context="c", niche="n")
    db_session.add(reel)
    db_session.flush()
    _hf_image_cache_file(tmp_path).write_bytes(_png())
    src = AS.HuggingFaceImageSource("k", "org/m", tmp_path)
    AS._generate_gated_hf_asset(db_session, reel.id, "asset_hf_image", src, "q", lambda r: 1.0)
    ev = db_session.query(models.StageEvent).filter_by(stage="asset_hf_image").one()
    assert "skipped" not in ev.detail
