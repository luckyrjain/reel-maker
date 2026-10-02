"""Characterization tests for the no-Whisper proportional timing fallbacks in
engine/render/compositor.py: `_build_text_filter()` (burned-in drawtext windows)
and `_proportional_caption_cues()` (SRT cues).

Both implement the same word-count-proportional placement (0.3 s minimum segment,
clamp to the beat end, stop at the boundary, stretch the last segment to the end).
These tests pin the CURRENT behavior of both — including two known quirks flagged
below — so extracting the shared core is provably behavior-preserving.
"""
import re

import pytest

from engine.render.compositor import (
    _build_text_filter,
    _proportional_caption_cues,
    _proportional_spans,
    _split_vo_sentences,
)


def _windows(beats, durations):
    """(text, start, end) for every drawtext window in the filter string."""
    f = _build_text_filter(beats, durations)
    return [
        (m.group(3), float(m.group(1)), float(m.group(2)))
        for m in re.finditer(r"between\(t,([\d.]+),([\d.]+)\).*?text='([^']*)'", f)
    ]


def _cues(vo, duration):
    return [(c.text, c.start_s, c.end_s) for c in _proportional_caption_cues(vo, duration)]


# ── SRT fallback: _proportional_caption_cues ─────────────────────────────────

@pytest.mark.parametrize("vo", ["", None, "...", " . ! "])
def test_cues_empty_vo_yields_no_cues(vo):
    assert _cues(vo, 6.0) == []


def test_cues_weight_by_sentence_word_count():
    cues = _cues("One two. Three four five six.", 9.0)
    assert [c[0] for c in cues] == ["One two", "Three four five six"]
    assert cues[0][1:] == pytest.approx((0.0, 3.0))
    assert cues[1][1:] == pytest.approx((3.0, 9.0))


def test_cues_split_on_em_dash_and_question_exclamation():
    cues = _cues("Hello there — big news. Wow!", 8.0)
    assert [c[0] for c in cues] == ["Hello there", "big news", "Wow"]
    assert [round(c[1], 3) for c in cues] == [0.0, 3.2, 6.4]
    assert cues[-1][2] == 8.0


def test_cues_single_sentence_spans_whole_beat():
    assert _cues("Only one sentence here", 4.0) == [("Only one sentence here", 0.0, 4.0)]


def test_cues_are_beat_relative_and_last_cue_stretched_to_duration():
    cues = _cues("A b c. D e f.", 7.0)
    assert cues[0][1] == 0.0
    assert cues[-1][2] == 7.0


def test_cues_zero_duration_yields_no_cues():
    assert _cues("A b. C d.", 0.0) == []


def test_cues_floor_drops_sentences_that_do_not_fit_current_behavior():
    """KNOWN QUIRK (current behavior, deliberately preserved by the refactor):
    when duration/n < 0.3 s the 0.3 s floor makes the running cursor outrun the beat,
    so later sentences are silently dropped from the caption track. 20 one-word
    sentences in 3 s place only the first 11 (the last one zero-width, from float
    noise at the boundary). Fixing this is a separate product decision."""
    vo = ". ".join(["w"] * 20) + "."
    cues = _cues(vo, 3.0)
    assert len(cues) == 11
    assert cues[0][1:] == pytest.approx((0.0, 0.3))
    assert cues[9][1:] == pytest.approx((2.7, 3.0))
    assert cues[-1][2] == 3.0


# ── drawtext fallback: _build_text_filter ────────────────────────────────────

def test_drawtext_empty_vo_splits_equally_across_lines():
    """Deliberate divergence from the SRT copy: no VO sentences means an equal split
    across the displayed lines, not no output."""
    for vo in ("", None):
        beat = {"on_screen_text": ["a", "b", "c"], "duration_s": 6.0}
        if vo is not None:
            beat["vo_script"] = vo
        wins = _windows([beat], [6.0])
        assert [w[0] for w in wins] == ["a", "b", "c"]
        assert [w[1:] for w in wins] == pytest.approx([(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)])


def test_drawtext_fewer_lines_than_sentences_uses_all_sentences_as_denominator():
    """3 sentences (2, 4, 1 words) but 2 lines: line 'a' gets 2/7 of the beat, and the
    last line is stretched to the beat end."""
    beat = {
        "vo_script": "One two. Three four five six. Seven.",
        "on_screen_text": ["a", "b"],
    }
    wins = _windows([beat], [10.0])
    assert [w[0] for w in wins] == ["a", "b"]
    assert wins[0][1:] == pytest.approx((0.0, 10.0 * 2 / 7), abs=1e-3)
    assert wins[1][1:] == pytest.approx((10.0 * 2 / 7, 10.0), abs=1e-3)


def test_drawtext_more_lines_than_sentences_pads_weight_one_and_stops_at_end():
    """2 one-word sentences, 4 lines: padded lines get weight 1 but the denominator is
    still the sentence word total (2), so the first two lines already fill the beat
    and the rest are not placed."""
    beat = {"vo_script": "One. Two.", "on_screen_text": ["a", "b", "c", "d"]}
    wins = _windows([beat], [8.0])
    assert [w[0] for w in wins] == ["a", "b"]
    assert [w[1:] for w in wins] == pytest.approx([(0.0, 4.0), (4.0, 8.0)])


def test_drawtext_windows_are_absolute_across_beats():
    beats = [
        {"vo_script": "x", "on_screen_text": ["a"]},
        {"vo_script": "One two. Three four five six.", "on_screen_text": ["a", "b"]},
    ]
    wins = _windows(beats, [2.0, 9.0])
    assert [w[1:] for w in wins] == pytest.approx([(0.0, 2.0), (2.0, 5.0), (5.0, 11.0)])


def test_drawtext_floor_drops_lines_that_do_not_fit_current_behavior():
    """Same floor quirk as the SRT copy, bounded here by the 5-line cap: 5 lines over
    20 one-word sentences in 1 s place 4 lines (the 4th clamped to the beat end)."""
    beat = {
        "vo_script": ". ".join(["w"] * 20) + ".",
        "on_screen_text": [f"l{i}" for i in range(5)],
    }
    wins = _windows([beat], [1.0])
    assert [w[0] for w in wins] == ["l0", "l1", "l2", "l3"]
    assert [w[1:] for w in wins] == pytest.approx(
        [(0.0, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 1.0)]
    )


def test_drawtext_zero_duration_places_nothing():
    beat = {"vo_script": "A b. C d.", "on_screen_text": ["a", "b"]}
    assert _windows([beat], [0.0]) == []


# ── shared core: _proportional_spans / _split_vo_sentences ───────────────────

def test_spans_share_duration_by_weight():
    spans = _proportional_spans([1, 3], 4, 8.0)
    assert spans == pytest.approx([(0, 0.0, 2.0), (1, 2.0, 8.0)])


def test_spans_start_offsets_absolute_time():
    spans = _proportional_spans([1, 1], 2, 4.0, start=10.0)
    assert spans == pytest.approx([(0, 10.0, 12.0), (1, 12.0, 14.0)])


def test_spans_total_weight_is_independent_of_weights():
    """The drawtext caller places 1 of 3 sentences' worth of lines: the denominator is
    the caller's, so a lone weight-1 item out of 4 gets only a quarter of the beat
    (then is stretched to the end, being the last placed)."""
    spans = _proportional_spans([1], 4, 8.0)
    assert spans == [(0, 0.0, 8.0)]
    spans = _proportional_spans([1, 1], 4, 8.0)
    assert spans == pytest.approx([(0, 0.0, 2.0), (1, 2.0, 8.0)])


def test_spans_stretch_last_span_to_the_end():
    assert _proportional_spans([1, 1], 4, 8.0)[-1][2] == 8.0


def test_spans_stop_placing_once_the_end_is_reached():
    spans = _proportional_spans([1, 1, 1, 1], 2, 8.0)
    assert [i for i, _, _ in spans] == [0, 1]


def test_spans_apply_minimum_span_floor():
    spans = _proportional_spans([1, 1], 1000, 10.0)
    assert spans[0] == pytest.approx((0, 0.0, 0.3))
    assert spans[1][1] == pytest.approx(0.3)


def test_spans_overrunning_span_ends_exactly_at_the_end():
    spans = _proportional_spans([100], 100, 0.2)
    assert spans == [(0, 0.0, 0.2)]


def test_spans_zero_duration_and_empty_weights_place_nothing():
    assert _proportional_spans([1, 1], 2, 0.0) == []
    assert _proportional_spans([], 1, 5.0) == []


def test_split_vo_sentences_boundaries_and_stripping():
    assert _split_vo_sentences(" One. Two! Three? Four \u2014 five ") == [
        "One", "Two", "Three", "Four", "five",
    ]
    assert _split_vo_sentences("...") == []
    assert _split_vo_sentences("") == []
    assert _split_vo_sentences(None) == []
