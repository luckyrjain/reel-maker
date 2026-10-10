"""One wall-clock budget across a whole render's asset sourcing (engine/render/asset_sourcer.py).

Each search() has its own budget (900 s Pexels / 240 s Wikipedia), but a render resolves assets for every
beat, so those add up to hours. `asset_budget(seconds)` sets one deadline for everything inside it
(`render_cut` wraps its beat loop in it): each search's budget is clipped to what is left, no further
download starts once it is spent, and the paid HuggingFace tiers are not called. A cached file is still
served. Outside the context nothing changes, and no extra clock reading is made.
"""
from unittest.mock import MagicMock, patch

import pytest

from engine.render import asset_sourcer as AS
from tests.test_sourcer_download_guards import PEXELS_OK, WIKI_OK, _Resp, _Stream, _fake_clock, _pexels, _wiki
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

def _hf(result=None):
    src = MagicMock()
    src.api_key = "k"
    src.generate.return_value = result
    src.last_call_was_generated = False
    return src


def _no_result_sourcer():
    s = MagicMock()
    s.search.return_value = None
    return s


def test_hf_tiers_are_skipped_once_the_budget_is_spent(monkeypatch):
    _clock(monkeypatch, 0.0)
    hf_video, hf = _hf(), _hf()
    with AS.asset_budget(-1.0):
        out = AS.resolve_beat_assets(MagicMock(), "q", 1.0, _no_result_sourcer(), None, hf_video, hf, reel_id=1)
    assert out == [(None, None)]
    hf_video.generate.assert_not_called()
    hf.generate.assert_not_called()


def test_hf_tiers_run_while_budget_remains(monkeypatch):
    _clock(monkeypatch, 0.0)
    hf_video, hf = _hf(), _hf()
    with AS.asset_budget(100.0):
        AS.resolve_beat_assets(MagicMock(), "q", 1.0, _no_result_sourcer(), None, hf_video, hf, reel_id=1)
    hf_video.generate.assert_called_once()
    hf.generate.assert_called_once()


def test_hf_tiers_run_without_any_budget():
    hf_video, hf = _hf(), _hf()
    AS.resolve_beat_assets(MagicMock(), "q", 1.0, _no_result_sourcer(), None, hf_video, hf, reel_id=1)
    hf_video.generate.assert_called_once()
    hf.generate.assert_called_once()


def test_a_spent_budget_writes_no_stage_event_for_a_skipped_hf_call(monkeypatch):
    _clock(monkeypatch, 0.0)
    with patch.object(AS, "record_stage") as rs, AS.asset_budget(-1.0):
        AS.resolve_beat_assets(MagicMock(), "q", 1.0, _no_result_sourcer(), None, _hf(), _hf(), reel_id=1)
    rs.assert_not_called()


def test_the_skip_is_logged(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger=AS.__name__)
    _clock(monkeypatch, 0.0)
    with AS.asset_budget(-1.0):
        AS.resolve_beat_assets(MagicMock(), "q", 1.0, _no_result_sourcer(), None, _hf(), None, reel_id=1)
    assert "asset budget" in caplog.text
