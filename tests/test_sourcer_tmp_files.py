"""Temp files of the sourcers' downloads (engine/render/asset_sourcer.py).

Every download is written to a temp file and `os.replace`d onto its final name, so a killed process
never leaves a half-written file under a name the `exists()` caches would serve. The temp name used
to be FIXED per target (`pexels_1.tmp`, `wiki_5.jpg.tmp`): two workers fetching the same asset at the
same moment wrote into the same file, and the loser's failure or deadline could leave a file with
holes at the final path. Temp names are now unique per download; a crashed process's leftovers are
swept when a source is created (only files older than an hour: a younger one may be another worker's
write in progress).
"""
import os
import threading
import time
from unittest.mock import patch

import pytest

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import (
    HuggingFaceImageSource, HuggingFaceVideoSource, PexelsVideoSource, WikipediaImageSource, _atomic_write,
)
from tests.test_sourcer_download_guards import PEXELS_OK, _Resp, _Stream, _pexels
from tests.test_sourcer_selection import _video, _vf


def _spy_replace(monkeypatch):
    seen = []
    real = os.replace
    monkeypatch.setattr(AS.os, "replace", lambda a, b: (seen.append((str(a), str(b))), real(a, b))[1])
    return seen


# ── unique names ─────────────────────────────────────────────────────────────────────────────

def test_atomic_write_uses_a_different_temp_name_every_time(tmp_path, monkeypatch):
    seen = _spy_replace(monkeypatch)
    _atomic_write(tmp_path / "y.png", b"1")
    _atomic_write(tmp_path / "y.png", b"2")
    assert seen[0][0] != seen[1][0] and seen[0][1] == seen[1][1] == str(tmp_path / "y.png")


def test_atomic_write_temp_is_beside_the_target_so_the_rename_is_atomic(tmp_path, monkeypatch):
    seen = _spy_replace(monkeypatch)
    _atomic_write(tmp_path / "y.png", b"1")
    tmp = seen[0][0]
    assert os.path.dirname(tmp) == str(tmp_path) and os.path.basename(tmp).startswith("y.png.")
    assert tmp.endswith(".tmp")


def test_two_simultaneous_writes_of_one_file_never_interleave(tmp_path):
    """With one shared temp name the two writers truncated and appended into each other's file."""
    target = tmp_path / "wiki_1.jpg"
    a, b = b"A" * 400_000, b"B" * 400_000
    barrier = threading.Barrier(2)

    errors = []

    def write(data):
        barrier.wait()
        try:
            for _ in range(20):
                _atomic_write(target, data)
        except Exception as exc:                                  # noqa: BLE001 - report, don't die silently
            errors.append(repr(exc))

    threads = [threading.Thread(target=write, args=(d,)) for d in (a, b)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []                                           # one writer's replace() used to find its temp file gone
    assert target.read_bytes() in (a, b)                          # one whole version, never a mix
    assert [p.name for p in tmp_path.iterdir()] == ["wiki_1.jpg"]


def test_pexels_downloads_of_the_same_asset_use_different_temp_names(tmp_path, monkeypatch):
    seen = _spy_replace(monkeypatch)
    for _ in range(2):                                            # same directory, same video id, twice
        _pexels(tmp_path, [_video(7, [_vf(1080, 1920, link=PEXELS_OK)])],
                _Stream({PEXELS_OK: _Resp(chunks=(b"v",))}))
        (tmp_path / "pexels_7.mp4").unlink()                      # so the second one downloads again
    assert seen[0][0] != seen[1][0]                               # a fixed name would be identical here
    assert seen[0][0].endswith(".tmp") and "pexels_7.mp4." in seen[0][0]
    assert list(tmp_path.iterdir()) == []


def test_pexels_a_stale_temp_file_with_the_old_fixed_name_is_not_used(tmp_path):
    (tmp_path / "pexels_1.tmp").write_bytes(b"JUNK")              # what a crashed older version left
    result = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])],
                     _Stream({PEXELS_OK: _Resp(chunks=(b"video",))}))
    assert result.local_path.read_bytes() == b"video"


def test_pexels_a_failed_download_removes_only_its_own_temp_file(tmp_path):
    other = tmp_path / "pexels_1.other-worker.tmp"
    other.write_bytes(b"someone else's in-flight write")
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])],
                   _Stream({PEXELS_OK: _Resp(status=500)})) is None
    assert other.exists() and [p.name for p in tmp_path.iterdir()] == [other.name]


# ── the sweep ────────────────────────────────────────────────────────────────────────────────

def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.mark.parametrize("make", [
    lambda d: PexelsVideoSource("k", d),
    lambda d: WikipediaImageSource(d),
    lambda d: HuggingFaceImageSource("k", "m", d),
    lambda d: HuggingFaceVideoSource("k", "m", d),
], ids=["pexels", "wikipedia", "hf-image", "hf-video"])
def test_creating_a_source_sweeps_old_temp_files_only(tmp_path, make):
    old, young, keep, subdir = (tmp_path / "pexels_1.aaaa.tmp", tmp_path / "pexels_2.bbbb.tmp",
                                tmp_path / "pexels_3.mp4", tmp_path / "dir.tmp")
    old.write_bytes(b"x"); young.write_bytes(b"x"); keep.write_bytes(b"x"); subdir.mkdir()
    _age(old, 2 * 3600); _age(keep, 2 * 3600)
    make(tmp_path)
    assert not old.exists()                          # a crashed process's leftover
    assert young.exists()                            # may be another worker's write in progress
    assert keep.exists() and subdir.is_dir()         # never a real asset, never a directory


def test_the_sweep_survives_an_unremovable_file(tmp_path, monkeypatch):
    old = tmp_path / "x.tmp"
    old.write_bytes(b"x")
    _age(old, 2 * 3600)
    monkeypatch.setattr(AS.Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("no")))
    PexelsVideoSource("k", tmp_path)                 # must not raise
    assert AS._STALE_TMP_AGE_S == 3600
