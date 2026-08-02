"""
Tests for audio ↔ on-screen text synchronisation.

Covers three bug-fix areas:
  1. clean_guide() always re-derives on_screen_text from vo_script in voiceover mode
     so compositor proportional timing has correct sentence-aligned content.
  2. _build_text_filter() proportional fallback uses ALL VO sentences for the
     word-count denominator (not just the first n) and never overflows beat boundary.
  3. PATCH endpoint re-derives on_screen_text when vo_script is edited.
"""
import re

import pytest

from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.postprocess import _derive_on_screen, clean_guide
from engine.render.compositor import _build_text_filter


# ── helpers ───────────────────────────────────────────────────────────────────

def _beat(index, vo, on_screen=None, duration_s=8, type="body"):
    return Beat(
        index=index, type=type, duration_s=duration_s,
        visual_direction="player action",
        on_screen_text=on_screen or ["PLACEHOLDER"],
        vo_script=vo,
    )


_PAD_BEAT = Beat(
    index=99, type="cta", duration_s=5,
    visual_direction="player action",
    on_screen_text=["Subscribe"],
    vo_script="Subscribe for more.",
)

def _guide(*beats, voiceover_mode="voiceover") -> MasterGuide:
    # PlatformGuide requires min_length=3; pad with a hook and a cta if needed.
    beat_list = list(beats)
    if len(beat_list) < 2:
        beat_list.insert(0, Beat(
            index=98, type="hook", duration_s=5,
            visual_direction="player action",
            on_screen_text=["HOOK_PAD"],
            vo_script="Could Argentina win the World Cup?",
        ))
    if len(beat_list) < 3:
        beat_list.append(_PAD_BEAT)
    return MasterGuide(
        title="Test", niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts", target_length_s=45,
            caption="Test caption for the reel.",
            hashtags=["football"] * 10,
            beats=beat_list,
        )],
    )


# ── postprocess: clean_guide always regenerates from VO ──────────────────────

def _find(guide, index):
    return next(b for b in guide.cuts[0].beats if b.index == index)


def test_clean_guide_replaces_arbitrary_llm_on_screen_text():
    """LLM phrases that don't correspond to VO sentences are replaced."""
    guide = _guide(_beat(0, "Cristian Romero changes everything.", on_screen=["Arbitrary phrase"]))
    clean_guide(guide, "voiceover")
    beat = _find(guide, 0)
    assert beat.on_screen_text != ["Arbitrary phrase"]
    assert any("Cristian" in line or "Romero" in line or "changes" in line
               for line in beat.on_screen_text)


def test_clean_guide_replaces_section_labels():
    """ALL-CAPS section labels are replaced with VO-derived content."""
    guide = _guide(_beat(0, "Messi scores the winner.", on_screen=["HOOK"]))
    clean_guide(guide, "voiceover")
    beat = _find(guide, 0)
    assert "HOOK" not in beat.on_screen_text
    assert beat.on_screen_text  # non-empty


def test_clean_guide_one_line_per_sentence():
    """on_screen_text lines equal the number of VO sentences after clean_guide."""
    vo = "Sentence one. Sentence two. Sentence three."
    b = _beat(0, vo, on_screen=["arbitrary", "junk", "lines", "too", "many"])
    guide = _guide(b)
    clean_guide(guide, "voiceover")
    # Find our beat by index (pad beats may be inserted before/after)
    target = next(x for x in guide.cuts[0].beats if x.index == 0)
    sentences = [s.strip() for s in re.split(r"[.!?—]+", vo) if s.strip()]
    assert len(target.on_screen_text) == len(sentences)


def test_clean_guide_non_vo_mode_keeps_non_label_lines():
    """In music_only mode, non-label on_screen_text lines are preserved."""
    guide = _guide(_beat(0, "Messi scores.", on_screen=["Custom keeper line"]))
    clean_guide(guide, "music_only")
    beat = _find(guide, 0)
    assert "Custom keeper line" in beat.on_screen_text


def test_clean_guide_strips_vo_label_prefix():
    """Structural prefixes like 'HOOK: ' are stripped from vo_script."""
    guide = _guide(_beat(0, "HOOK: Messi changes everything."))
    clean_guide(guide, "voiceover")
    beat = _find(guide, 0)
    assert not beat.vo_script.startswith("HOOK:")
    assert beat.vo_script.startswith("Messi")


# ── compositor: proportional timing ──────────────────────────────────────────

def _timed(beats_data, beat_durations):
    """Run _build_text_filter and extract (text, start, end) tuples from the filter string."""
    f = _build_text_filter(beats_data, beat_durations)
    import re
    segments = []
    for m in re.finditer(
        r"between\(t,([\d.]+),([\d.]+)\).*?text='([^']*)'", f
    ):
        segments.append((m.group(3), float(m.group(1)), float(m.group(2))))
    return segments


def test_proportional_timing_uses_full_vo_word_count():
    """
    Single on_screen_text line for a two-sentence VO must cover the full beat,
    not just the fraction corresponding to the first sentence.
    """
    vo = "Short sentence. A much longer second sentence with many more words here."
    beat = {
        "vo_script": vo,
        "on_screen_text": ["Short sentence"],  # 1 line, 2 VO sentences
        "duration_s": 10.0,
    }
    segs = _timed([beat], [10.0])
    assert len(segs) == 1
    # The single line should span the entire beat (0 → 10), not just fraction of it
    _, start, end = segs[0]
    assert start == pytest.approx(0.0, abs=0.1)
    assert end == pytest.approx(10.0, abs=0.1)


def test_proportional_timing_never_overflows_beat_boundary():
    """Segment end times must never exceed the beat's end time."""
    vo = "One. Two. Three."
    beat = {
        "vo_script": vo,
        "on_screen_text": ["One", "Two", "Three", "Four", "Five"],  # more lines than sentences
        "duration_s": 3.0,
    }
    segs = _timed([beat], [3.0])
    beat_end = 0.0 + 3.0
    for text, start, end in segs:
        assert end <= beat_end + 0.001, f"'{text}' ends at {end:.3f} > beat_end {beat_end}"
        assert start < end, f"'{text}' has start {start:.3f} >= end {end:.3f}"


def test_proportional_timing_multi_beat_accumulation():
    """Text filter offsets accumulate correctly across multiple beats."""
    beats_data = [
        {"vo_script": "First beat sentence.", "on_screen_text": ["First"], "duration_s": 5.0},
        {"vo_script": "Second beat sentence.", "on_screen_text": ["Second"], "duration_s": 8.0},
    ]
    segs = _timed(beats_data, [5.0, 8.0])
    assert len(segs) == 2
    _, s0_start, s0_end = segs[0]
    _, s1_start, s1_end = segs[1]
    # Beat 0 within [0, 5); beat 1 within [5, 13)
    assert s0_start >= 0.0 and s0_end <= 5.0 + 0.001
    assert s1_start >= 5.0 - 0.001 and s1_end <= 13.0 + 0.001


def test_proportional_timing_matches_sentence_proportions():
    """
    With two sentences of unequal length, the longer sentence gets more screen time.
    """
    vo = "Short. " + " ".join(["word"] * 20) + "."  # 1-word sent + 20-word sent
    beat = {
        "vo_script": vo,
        "on_screen_text": ["Short", "Long"],
        "duration_s": 10.0,
    }
    segs = _timed([beat], [10.0])
    assert len(segs) == 2
    _, _, s0_end = segs[0]
    _, s1_start, _ = segs[1]
    # "Short" should get a small slice; "Long" should get most of the beat
    short_duration = s0_end - 0.0
    long_duration = 10.0 - s1_start
    assert long_duration > short_duration * 3, (
        f"Short segment ({short_duration:.2f}s) should be much shorter than "
        f"long segment ({long_duration:.2f}s)"
    )


# ── visual direction prompt anchoring ─────────────────────────────────────

def test_build_visuals_system_message_anchors_to_beat_vo():
    """build_visuals_messages system prompt must instruct the LLM to use only beat VO content."""
    from engine.generation.prompt import build_visuals_messages
    beats = [{'index': 0, 'beat_type': 'hook', 'duration_s': 3.0,
              'vo_script': 'Argentina looking strong this tournament.', 'player': ''}]
    messages = build_visuals_messages(beats, 'football')
    system_msg = next(m['content'] for m in messages if m['role'] == 'system')
    assert 'ONLY' in system_msg
    assert 'explicitly' in system_msg


# ── whisper timing fallback ───────────────────────────────────────────────

def test_whisper_timing_falls_back_when_fewer_words_than_lines():
    """Whisper transcripts shorter than the line list must not drive the timing.

    _whisper_timestamps slices the word stream by `i * m // n`; when m < n the
    end index wraps to segments[-1], so several lines get identical windows and
    render stacked on top of each other. The caller must use proportional
    timing instead.
    """
    from dataclasses import dataclass

    @dataclass
    class _Seg:
        text: str
        start_s: float
        end_s: float

    beat = {
        "vo_script": "One. Two. Three. Four. Five.",
        "on_screen_text": ["One", "Two", "Three", "Four", "Five"],
        "duration_s": 5.0,
    }
    two_words = [_Seg("one", 0.0, 0.5), _Seg("two", 0.5, 1.0)]

    chain = _build_text_filter([beat], [5.0], [two_words])
    windows = re.findall(r"between\(t,([\d.]+),([\d.]+)\)", chain)

    assert len(windows) == 5
    starts = [float(s) for s, _ in windows]
    assert starts == sorted(starts), "segments must not overlap or run backwards"
    assert len(set(starts)) == 5, "each line needs its own window, not a shared one"
    # Proportional timing fills the beat; the truncated whisper stream would stop at 1.0s
    assert float(windows[-1][1]) == pytest.approx(5.0)
