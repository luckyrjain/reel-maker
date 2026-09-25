"""Tests for engine/render/srt.py — pure SRT formatting, no ffmpeg/network."""
from engine.render.captions import CaptionSegment
from engine.render.srt import _format_timestamp, write_srt


# ── timestamp formatting ──────────────────────────────────────────────────────

def test_format_timestamp_basic():
    assert _format_timestamp(0.0) == "00:00:00,000"
    assert _format_timestamp(1.5) == "00:00:01,500"
    assert _format_timestamp(61.25) == "00:01:01,250"


def test_format_timestamp_hour_boundary():
    """Exactly one hour, and just past it, must roll minutes/seconds over correctly."""
    assert _format_timestamp(3600.0) == "01:00:00,000"
    assert _format_timestamp(3661.005) == "01:01:01,005"


def test_format_timestamp_rounds_milliseconds():
    # 1.9999s rounds to 2.000s, not 1.999s — guards against truncation instead of rounding.
    assert _format_timestamp(1.9999) == "00:00:02,000"


def test_format_timestamp_never_negative():
    """A cue with a tiny negative start (float rounding) must clamp to zero, not wrap."""
    assert _format_timestamp(-0.001) == "00:00:00,000"


# ── write_srt: empty input ────────────────────────────────────────────────────

def test_write_srt_empty_cues_returns_none_and_writes_nothing(tmp_path):
    out = tmp_path / "captions.srt"
    result = write_srt([], out)
    assert result is None
    assert not out.exists()


# ── write_srt: sequential numbering + format ──────────────────────────────────

def test_write_srt_sequential_numbering_and_format(tmp_path):
    out = tmp_path / "captions.srt"
    cues = [
        CaptionSegment(text="Hello there.", start_s=0.0, end_s=1.5),
        CaptionSegment(text="General Kenobi.", start_s=1.5, end_s=3.0),
    ]
    result = write_srt(cues, out)
    assert result == out
    assert out.exists()

    content = out.read_text(encoding="utf-8")
    blocks = content.strip("\n").split("\n\n")
    assert len(blocks) == 2

    lines0 = blocks[0].split("\n")
    assert lines0[0] == "1"
    assert lines0[1] == "00:00:00,000 --> 00:00:01,500"
    assert lines0[2] == "Hello there."

    lines1 = blocks[1].split("\n")
    assert lines1[0] == "2"
    assert lines1[1] == "00:00:01,500 --> 00:00:03,000"
    assert lines1[2] == "General Kenobi."


# ── write_srt: multi-cue / multi-beat concatenation with offset times ────────

def test_write_srt_multi_cue_concatenation_with_absolute_offsets(tmp_path):
    """Cues from a later beat (already shifted to absolute time by the caller)
    must appear in file order with correctly increasing numbering and timestamps —
    this module does not do any shifting itself (that's composite_cut()'s job)."""
    out = tmp_path / "captions.srt"
    cues = [
        CaptionSegment(text="Beat zero, first cue.", start_s=0.0, end_s=2.0),
        CaptionSegment(text="Beat zero, second cue.", start_s=2.0, end_s=4.5),
        # Beat one starts at absolute t=8.0 (already offset by the caller)
        CaptionSegment(text="Beat one, first cue.", start_s=8.0, end_s=10.0),
    ]
    write_srt(cues, out)
    content = out.read_text(encoding="utf-8")
    blocks = content.strip("\n").split("\n\n")
    assert len(blocks) == 3

    numbers = [b.split("\n")[0] for b in blocks]
    assert numbers == ["1", "2", "3"]

    third_timestamp_line = blocks[2].split("\n")[1]
    assert third_timestamp_line == "00:00:08,000 --> 00:00:10,000"


def test_write_srt_creates_parent_directories(tmp_path):
    out = tmp_path / "nested" / "dir" / "captions.srt"
    result = write_srt([CaptionSegment(text="x", start_s=0.0, end_s=1.0)], out)
    assert result == out
    assert out.exists()
