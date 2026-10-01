"""Tests for engine/generation/guide_edit.py — direct, DB-free tests of the
beat-field diff/normalize rules that api/routers/cuts.py's update_cut()/
choose_hook_variant() used to only be provable through a full FastAPI TestClient +
real DB. See docs/specs/2026-09-guide-edit-module-design.md."""
from engine.generation.guide_edit import replace_beat_vo, set_beat_field


def _beats():
    return [
        {"index": 0, "type": "hook", "duration_s": 5.0,
         "visual_direction": "Argentina training", "vo_script": "Could Argentina win?",
         "on_screen_text": ["Could Argentina win?"]},
        {"index": 1, "type": "body", "duration_s": 10.0,
         "visual_direction": "Messi highlights", "vo_script": "Messi creates chances.",
         "on_screen_text": ["Messi creates chances."]},
    ]


# ── duration_s ──────────────────────────────────────────────────────────────

def test_set_beat_field_duration_writes_through_a_genuine_change():
    beats = _beats()
    changed = set_beat_field(beats, 1, "duration_s", 12.0)
    assert changed is True
    assert beats[1]["duration_s"] == 12.0


def test_set_beat_field_duration_is_a_no_op_when_unchanged():
    beats = _beats()
    changed = set_beat_field(beats, 1, "duration_s", 10.0)
    assert changed is False
    assert beats[1]["duration_s"] == 10.0


# ── visual_direction ──────────────────────────────────────────────────────────

def test_set_beat_field_visual_direction_writes_through_a_genuine_change():
    beats = _beats()
    changed = set_beat_field(beats, 1, "visual_direction", "Messi through-ball")
    assert changed is True
    assert beats[1]["visual_direction"] == "Messi through-ball"


def test_set_beat_field_visual_direction_normalizes_the_submitted_side():
    """A submitted value always comes back through .strip() from the form; the
    stored value must not be reported as "changed" against a submission that's
    just the stripped version of itself."""
    beats = _beats()
    changed = set_beat_field(beats, 1, "visual_direction", "  Messi highlights  ")
    assert changed is False


def test_set_beat_field_visual_direction_normalizes_the_stored_side_too():
    """Review-round regression: a STORED value with pre-existing incidental
    whitespace (e.g. from LLM generation, never itself .strip()'d) must not look
    "changed" just because the submitted form value always comes back stripped."""
    beats = _beats()
    beats[1]["visual_direction"] = "Messi highlights   "   # stored with trailing whitespace
    changed = set_beat_field(beats, 1, "visual_direction", "Messi highlights")
    assert changed is False
    assert beats[1]["visual_direction"] == "Messi highlights   "   # untouched, not rewritten


# ── vo_script ──────────────────────────────────────────────────────────────

def test_set_beat_field_vo_script_writes_through_and_rederives_on_screen_text():
    beats = _beats()
    changed = set_beat_field(beats, 1, "vo_script", "Messi assists constantly.")
    assert changed is True
    assert beats[1]["vo_script"] == "Messi assists constantly."
    assert beats[1]["on_screen_text"] == ["Messi assists constantly"]   # derive_on_screen strips the terminator


def test_set_beat_field_vo_script_crlf_resubmit_is_not_a_change():
    """An HTML <textarea> always re-encodes its newlines as \\r\\n on submit,
    touched or not -- an untouched multi-line vo_script resubmitted with CRLF
    line endings must not look changed, and must NOT re-derive on_screen_text
    (which would be a spurious rewrite of an otherwise-untouched field)."""
    beats = _beats()
    beats[1]["vo_script"] = "Line one.\nLine two."
    original_on_screen = list(beats[1]["on_screen_text"])
    resubmitted = "Line one.\r\nLine two.\r\n"   # CRLF + trailing CR, as a browser would send
    changed = set_beat_field(beats, 1, "vo_script", resubmitted)
    assert changed is False
    assert beats[1]["vo_script"] == "Line one.\nLine two."
    assert beats[1]["on_screen_text"] == original_on_screen


def test_set_beat_field_vo_script_stored_side_with_its_own_crlf_is_not_a_change():
    """Second review-round regression: the STORED value can itself carry
    line-ending quirks that pre-date this feature and were never normalized --
    an untouched save must not treat that as a change either."""
    beats = _beats()
    beats[1]["vo_script"] = "Line one.\r\nLine two.\r\n"   # stored with CRLF already
    changed = set_beat_field(beats, 1, "vo_script", "Line one.\nLine two.")
    assert changed is False


# ── on_screen_text ────────────────────────────────────────────────────────────

def test_set_beat_field_on_screen_text_writes_through_a_genuine_change():
    beats = _beats()
    changed = set_beat_field(beats, 1, "on_screen_text", ["New line one", "New line two"])
    assert changed is True
    assert beats[1]["on_screen_text"] == ["New line one", "New line two"]


def test_set_beat_field_on_screen_text_strips_blanks_and_caps_at_five():
    beats = _beats()
    lines = ["a", "", "  ", "b", "c", "d", "e", "f"]
    set_beat_field(beats, 1, "on_screen_text", lines)
    assert beats[1]["on_screen_text"] == ["a", "b", "c", "d", "e"]


def test_set_beat_field_on_screen_text_resubmit_is_not_a_change():
    beats = _beats()
    beats[1]["on_screen_text"] = ["Line one", "Line two"]
    changed = set_beat_field(beats, 1, "on_screen_text", ["Line one", "Line two"])
    assert changed is False


def test_set_beat_field_unknown_field_raises():
    beats = _beats()
    try:
        set_beat_field(beats, 0, "not_a_real_field", "x")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ── replace_beat_vo ───────────────────────────────────────────────────────────

def test_replace_beat_vo_swaps_beat_zero_and_rederives_on_screen_text():
    guide = {"title": "Test", "beats": _beats()}
    new_guide = replace_beat_vo(guide, 0, "Is Romero the best defender alive?")
    assert new_guide["beats"][0]["vo_script"] == "Is Romero the best defender alive?"
    assert new_guide["beats"][0]["on_screen_text"] == ["Is Romero the best defender"]   # word-wrapped to 28 chars, terminator stripped
    # beat 1 (and everything else) untouched
    assert new_guide["beats"][1] == guide["beats"][1]


def test_replace_beat_vo_returns_a_new_guide_without_mutating_the_original():
    """SQLAlchemy's JSON column only detects attribute reassignment, not in-place
    mutation of a value it already holds -- replace_beat_vo() must hand back a new
    object the caller can reassign onto Cut.guide, not mutate the input in place."""
    guide = {"title": "Test", "beats": _beats()}
    original_vo = guide["beats"][0]["vo_script"]
    replace_beat_vo(guide, 0, "A different hook line entirely")
    assert guide["beats"][0]["vo_script"] == original_vo
