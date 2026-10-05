"""Two leftovers of the sourcer download guards (see docs/specs/2026-10-sourcer-selection-decisions-module-design.md).

1. The download loops in `PexelsVideoSource.search` / `WikipediaImageSource.search` catch `Exception`
   to move on to the next candidate. Celery's `SoftTimeLimitExceeded` (billiard) is an `Exception`
   subclass, so a soft limit landing mid-download was eaten and the next candidate started with a fresh
   budget. It is now re-raised (matched by class name, so `engine/` does not import Celery).
2. The scratch file of a download had a fixed name (`pexels_1.tmp`, `<final>.tmp`), so two processes
   fetching the same asset truncated each other's file. It now has a unique name, and stale ones that a
   killed process left behind are swept.
"""
import os
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import PexelsVideoSource
from tests.test_sourcer_download_guards import (
    PEXELS_OK, WIKI_OK, WIKI_THUMB, _Resp, _Stream, _pexels, _wiki,
)
from tests.test_sourcer_selection import _MOD, _json_resp, _summary, _video, _vf

PEXELS_2 = "https://videos.pexels.com/video-files/2/b.mp4"


class SoftTimeLimitExceeded(Exception):
    """Same class name as billiard's, which is all the engine's check looks at."""


class _SubSoftLimit(SoftTimeLimitExceeded):
    pass


def _real_billiard():
    return pytest.importorskip("billiard.exceptions").SoftTimeLimitExceeded


SOFT_LIMITS = [SoftTimeLimitExceeded, _SubSoftLimit, pytest.param(None, id="billiard")]


def _soft(cls):
    return (_real_billiard() if cls is None else cls)("soft time limit")


# ── the check itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls", SOFT_LIMITS)
def test_is_soft_time_limit_matches_by_class_name_through_the_mro(cls):
    assert AS._is_soft_time_limit(_soft(cls)) is True


@pytest.mark.parametrize("exc", [
    ValueError("x"), RuntimeError("SoftTimeLimitExceeded"), OSError(), KeyError("k"),
    type("SoftTimeLimitExceededish", (Exception,), {})(), type("NotSoftTimeLimitExceeded", (Exception,), {})(),
])
def test_is_soft_time_limit_rejects_everything_else(exc):
    assert AS._is_soft_time_limit(exc) is False


# ── Pexels ───────────────────────────────────────────────────────────────────

def _two_hits():
    return [_video(1, [_vf(1080, 1920, link=PEXELS_OK)]), _video(2, [_vf(1080, 1920, link=PEXELS_2)])]


@pytest.mark.parametrize("cls", SOFT_LIMITS)
def test_pexels_a_soft_limit_when_the_connection_opens_propagates_and_the_next_hit_is_not_tried(tmp_path, cls):
    stream = _Stream({PEXELS_OK: _soft(cls), PEXELS_2: _Resp(chunks=(b"ok",))})
    with pytest.raises(Exception) as info:
        _pexels(tmp_path, _two_hits(), stream)
    assert AS._is_soft_time_limit(info.value)
    assert stream.urls == [PEXELS_OK]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("cls", SOFT_LIMITS)
def test_pexels_a_soft_limit_mid_body_propagates_and_leaves_no_tmp(tmp_path, cls):
    class _Dies(_Resp):
        def iter_bytes(self, chunk_size=None):
            yield b"partial"
            raise _soft(cls)

    stream = _Stream({PEXELS_OK: _Dies(), PEXELS_2: _Resp(chunks=(b"ok",))})
    with pytest.raises(Exception) as info:
        _pexels(tmp_path, _two_hits(), stream)
    assert AS._is_soft_time_limit(info.value)
    assert stream.urls == [PEXELS_OK]
    assert list(tmp_path.iterdir()) == []


def test_pexels_any_other_error_still_moves_on_to_the_next_hit(tmp_path):
    stream = _Stream({PEXELS_OK: RuntimeError("boom"), PEXELS_2: _Resp(chunks=(b"ok",))})
    result = _pexels(tmp_path, _two_hits(), stream)
    assert stream.urls == [PEXELS_OK, PEXELS_2] and result.local_path.read_bytes() == b"ok"


# ── Wikipedia ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls", SOFT_LIMITS)
def test_wikipedia_a_soft_limit_propagates_and_the_thumbnail_is_not_tried(tmp_path, cls):
    stream = _Stream({WIKI_OK: _soft(cls), WIKI_THUMB: _Resp(chunks=(b"t",))})
    with pytest.raises(Exception) as info:
        _wiki(tmp_path, _summary(), stream)
    assert AS._is_soft_time_limit(info.value)
    assert stream.urls == [WIKI_OK]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("cls", SOFT_LIMITS)
def test_wikipedia_a_soft_limit_mid_body_propagates_and_leaves_no_file(tmp_path, cls):
    class _Dies(_Resp):
        def iter_bytes(self, chunk_size=None):
            yield b"partial"
            raise _soft(cls)

    stream = _Stream({WIKI_OK: _Dies(), WIKI_THUMB: _Resp(chunks=(b"t",))})
    with pytest.raises(Exception) as info:
        _wiki(tmp_path, _summary(), stream)
    assert AS._is_soft_time_limit(info.value)
    assert stream.urls == [WIKI_OK]
    assert list(tmp_path.iterdir()) == []


def test_wikipedia_any_other_error_still_falls_back_to_the_thumbnail(tmp_path):
    stream = _Stream({WIKI_OK: RuntimeError("boom"), WIKI_THUMB: _Resp(chunks=(b"t",))})
    assert _wiki(tmp_path, _summary(), stream).local_path.read_bytes() == b"t"


# ── unique scratch files ─────────────────────────────────────────────────────

def test_tmp_for_is_unique_per_call_and_next_to_the_target(tmp_path):
    target = tmp_path / "pexels_1.mp4"
    a, b = AS._tmp_for(target), AS._tmp_for(target)
    assert a != b
    assert a.parent == b.parent == tmp_path
    assert a.name.startswith("pexels_1.mp4.") and a.name.endswith(".tmp")


def test_atomic_write_uses_a_fresh_scratch_name_each_time(tmp_path):
    srcs = []
    real_replace = os.replace
    with patch.object(AS.os, "replace", lambda s, d: (srcs.append(os.fspath(s)), real_replace(s, d))[1]):
        AS._atomic_write(tmp_path / "a.png", b"one")
        AS._atomic_write(tmp_path / "a.png", b"two")
    assert len(set(srcs)) == 2 and all(s.endswith(".tmp") for s in srcs)
    assert (tmp_path / "a.png").read_bytes() == b"two" and [p.name for p in tmp_path.iterdir()] == ["a.png"]


def test_atomic_write_leaves_nothing_behind_on_failure(tmp_path):
    with patch.object(AS.os, "replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            AS._atomic_write(tmp_path / "a.png", b"x")
    assert list(tmp_path.iterdir()) == []


def test_pexels_two_overlapping_downloads_of_one_asset_do_not_corrupt_each_other(tmp_path):
    """The second download runs to completion while the first is mid-body; the first finishes after it."""
    names_seen = {"outer": None, "inner": None}
    src = PexelsVideoSource(api_key="k", store_dir=tmp_path)
    videos = [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])]

    def scratch_files():
        return sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp") or ".tmp" in p.name)

    class _Inner(_Resp):
        def iter_bytes(self, chunk_size=None):
            names_seen["inner"] = scratch_files()
            yield b"INNER-INNER-INNER"

    class _Outer(_Resp):
        def iter_bytes(self, chunk_size=None):
            yield b"OUTER-"
            names_seen["outer"] = scratch_files()
            inner_stream = _Stream({PEXELS_OK: _Inner()})
            inner = PexelsVideoSource(api_key="k", store_dir=tmp_path)
            with patch(f"{_MOD}._httpx_stream", inner_stream):
                inner_result = inner.search("q", 1.0)
            assert inner_result.local_path.read_bytes() == b"INNER-INNER-INNER"
            yield b"DATA"

    outer_stream = _Stream({PEXELS_OK: _Outer()})
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": videos})), \
            patch(f"{_MOD}._httpx_stream", outer_stream):
        result = src.search("q", 1.0)

    assert result.local_path.read_bytes() == b"OUTER-DATA"          # intact, not a mix of the two
    assert len(names_seen["outer"]) == 1 and len(names_seen["inner"]) == 2    # two different scratch files at once
    assert [p.name for p in tmp_path.iterdir()] == ["pexels_1.mp4"]


# ── stale scratch files from a killed process ────────────────────────────────

def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_stale_scratch_files_of_the_same_target_are_swept(tmp_path):
    target = tmp_path / "pexels_1.mp4"
    stale = tmp_path / "pexels_1.mp4.deadbeef.tmp"
    stale.write_bytes(b"x" * 10)
    _age(stale, AS._STALE_TMP_S + 60)
    AS._tmp_for(target)
    assert not stale.exists()


def test_a_recent_scratch_file_is_left_alone(tmp_path):
    """Another live download of the same asset is writing it right now."""
    target = tmp_path / "pexels_1.mp4"
    fresh = tmp_path / "pexels_1.mp4.cafef00d.tmp"
    fresh.write_bytes(b"x")
    AS._tmp_for(target)
    assert fresh.exists()


def test_the_sweep_only_touches_scratch_files_of_that_target(tmp_path):
    target = tmp_path / "pexels_1.mp4"
    other = tmp_path / "pexels_2.mp4.deadbeef.tmp"
    real = tmp_path / "pexels_1.mp4"
    prefix_only = tmp_path / "pexels_1.mp4.old"          # not *.tmp
    for p in (other, real, prefix_only):
        p.write_bytes(b"x")
        _age(p, AS._STALE_TMP_S * 10)
    AS._tmp_for(target)
    assert other.exists() and real.exists() and prefix_only.exists()


def test_a_sweep_failure_never_blocks_the_download(tmp_path):
    target = tmp_path / "pexels_1.mp4"
    stale = tmp_path / "pexels_1.mp4.deadbeef.tmp"
    stale.write_bytes(b"x")
    _age(stale, AS._STALE_TMP_S + 60)
    with patch("pathlib.Path.unlink", side_effect=PermissionError("nope")):
        assert AS._tmp_for(target).name.endswith(".tmp")


def test_the_stale_threshold_outlasts_the_longest_download_budget():
    assert AS._STALE_TMP_S > AS._VIDEO_DEADLINE_S * 2


def test_pexels_a_download_sweeps_a_killed_runs_leftover(tmp_path):
    leftover = tmp_path / "pexels_1.mp4.deadbeef.tmp"
    leftover.write_bytes(b"x" * 100)
    _age(leftover, AS._STALE_TMP_S + 60)
    stream = _Stream({PEXELS_OK: _Resp(chunks=(b"ok",))})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is not None
    assert [p.name for p in tmp_path.iterdir()] == ["pexels_1.mp4"]
