"""Tests for engine/generation/guide_schema.py::compute_guide_fingerprint().

No existing test file covered this function -- see
docs/specs/2026-09-stale-video-on-failed-rerender-system-design.md.
"""
from engine.generation.guide_schema import compute_guide_fingerprint


def _guide(vo="Test narration.", hashtags=None):
    return {
        "platform": "youtube_shorts",
        "target_length_s": 45.0,
        "beats": [
            {"index": 0, "type": "hook", "duration_s": 2.0, "visual_direction": "x",
             "on_screen_text": ["Hi"], "vo_script": vo, "transition": "cut"},
        ],
        "caption": "A caption",
        "hashtags": hashtags or ["a", "b", "c", "d", "e"],
    }


def test_compute_guide_fingerprint_is_none_for_a_falsy_guide():
    assert compute_guide_fingerprint(None) is None
    assert compute_guide_fingerprint({}) is None


def test_compute_guide_fingerprint_is_deterministic():
    guide = _guide()
    assert compute_guide_fingerprint(guide) == compute_guide_fingerprint(guide)


def test_compute_guide_fingerprint_is_independent_of_dict_key_insertion_order():
    """json.dumps(..., sort_keys=True) normalizes key order at every level -- the same
    logical guide built with keys inserted in a different order must fingerprint
    identically, or a round-trip through a different code path (e.g. a dict rebuilt from
    a DB row vs. one freshly constructed) could spuriously "mismatch" nothing at all."""
    ordered = _guide()
    reordered = {
        "hashtags": ordered["hashtags"],
        "caption": ordered["caption"],
        "beats": [dict(reversed(list(ordered["beats"][0].items())))],
        "target_length_s": ordered["target_length_s"],
        "platform": ordered["platform"],
    }
    assert compute_guide_fingerprint(ordered) == compute_guide_fingerprint(reordered)


def test_compute_guide_fingerprint_changes_when_vo_script_changes():
    """The actual bug this function exists to catch: an operator edits vo_script (a PATCH
    /cuts/{id} guide edit) -- the fingerprint must change so a stale pre-edit video is
    detectable at publish time."""
    before = compute_guide_fingerprint(_guide(vo="Original line."))
    after = compute_guide_fingerprint(_guide(vo="Edited line."))
    assert before != after


def test_compute_guide_fingerprint_changes_when_beat_order_or_list_order_changes():
    """Unlike dict-key order, LIST order is meaningful (beat sequence, hashtag list) and
    must NOT be normalized away -- a genuine reorder is a genuine content change."""
    a = compute_guide_fingerprint(_guide(hashtags=["a", "b", "c", "d", "e"]))
    b = compute_guide_fingerprint(_guide(hashtags=["e", "d", "c", "b", "a"]))
    assert a != b


def test_compute_guide_fingerprint_returns_a_hex_string():
    fp = compute_guide_fingerprint(_guide())
    assert fp is not None
    assert len(fp) == 64
    int(fp, 16)  # raises ValueError if not valid hex
