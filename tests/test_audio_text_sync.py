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
from engine.generation.postprocess import clean_guide
from engine.render.compositor import _build_beat_transcripts, _build_text_filter, _whisper_timestamps


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


# ── engine/render/captions.py: TranscriptResult (T2 — SRT caption export) ─────

class _FakeWhisperModel:
    def __init__(self, result):
        self._result = result

    def transcribe(self, path, word_timestamps=True, fp16=False):
        return self._result


_FAKE_WHISPER_RESULT = {
    "segments": [
        {
            "text": " Hello world.",
            "start": 0.5,
            "end": 1.8,
            "words": [
                {"word": "Hello", "start": 0.5, "end": 1.0},
                {"word": "world.", "start": 1.0, "end": 1.8},
            ],
        },
        {
            "text": " Goodbye.",
            "start": 2.0,
            "end": 2.9,
            "words": [
                {"word": "Goodbye.", "start": 2.0, "end": 2.9},
            ],
        },
    ],
}


def test_transcribe_audio_returns_words_and_segments_from_one_pass(monkeypatch, tmp_path):
    """TranscriptResult.words keeps the exact pre-refactor shape/values (flattened
    words, beat_offset_s=0.0 default); .segments is new, one full-segment
    CaptionSegment per Whisper segment, derived from the same transcribe() call."""
    from engine.render import captions

    monkeypatch.setattr(captions, "_load_model", lambda name: _FakeWhisperModel(_FAKE_WHISPER_RESULT))
    audio = tmp_path / "beat.wav"
    audio.write_bytes(b"not really audio, transcribe() is mocked")

    result = captions.transcribe_audio(audio)

    assert [(w.text, w.start_s, w.end_s) for w in result.words] == [
        ("Hello", 0.5, 1.0),
        ("world.", 1.0, 1.8),
        ("Goodbye.", 2.0, 2.9),
    ]
    assert [(s.text, s.start_s, s.end_s) for s in result.segments] == [
        ("Hello world.", 0.5, 1.8),
        ("Goodbye.", 2.0, 2.9),
    ]


def test_transcribe_audio_beat_offset_shifts_both_fields_identically(monkeypatch, tmp_path):
    """beat_offset_s keeps its pre-existing beat-relative semantics for BOTH fields —
    a non-default caller-supplied offset shifts .words and .segments the same way it
    always shifted .words alone. (composite_cut() itself never passes a non-zero
    value — see test_build_beat_transcripts_never_passes_a_nonzero_offset below.)"""
    from engine.render import captions

    monkeypatch.setattr(captions, "_load_model", lambda name: _FakeWhisperModel(_FAKE_WHISPER_RESULT))
    audio = tmp_path / "beat.wav"
    audio.write_bytes(b"x")

    result = captions.transcribe_audio(audio, beat_offset_s=10.0)

    assert result.words[0].start_s == pytest.approx(10.5)
    assert result.segments[0].start_s == pytest.approx(10.5)


def test_transcribe_audio_returns_empty_transcript_result_without_whisper(monkeypatch, tmp_path):
    """ImportError path (openai-whisper not installed) degrades to an empty
    TranscriptResult, not None — callers can keep doing `.words`/`.segments`
    without a None-check, same "degrade gracefully" posture as before this change."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "whisper":
            raise ImportError("no whisper")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    from engine.render import captions
    result = captions.transcribe_audio(tmp_path / "beat.wav")
    assert result.words == []
    assert result.segments == []


# ── offset-handling regression guard (design §2 — the central safety-critical rule) ──
#
# The original design draft would have had composite_cut() call transcribe_audio()
# with a per-beat cumulative offset to pre-shift .words to the reel's absolute
# timeline. That's wrong: _whisper_timestamps() (below) already adds each beat's
# cumulative start time to .words when building the burned-in-text drawtext filter.
# A pre-shifted .words would get that addition applied a SECOND time, silently
# corrupting caption timing for every beat past the first. Caught in design review
# (see docs/specs/2026-09-srt-caption-export-system-design.md §2's "Revision note")
# and fixed by keeping .words/.segments beat-relative at the source, always. The two
# tests below are independent guards against this regression ever coming back:
# one on the exact call arguments compositor.py uses, one on the resulting values.

def test_build_beat_transcripts_never_passes_a_nonzero_offset(monkeypatch, tmp_path):
    """THE single most important regression test in this change (per the design's
    own criticality rationale): compositor.py's beat-transcript builder must call
    transcribe_audio() at its default beat_offset_s=0.0 for every beat, never a
    per-beat cumulative offset.

    Mutation-tested: temporarily changing _build_beat_transcripts() to pass a
    non-zero per-beat offset (e.g. a running sum of beat durations) into
    transcribe_audio() — simulating the exact bug the design review caught — makes
    this assertion fail immediately (`calls` would be e.g. `[0.0, 5.0]` instead of
    `[0.0, 0.0]`). Confirmed by hand during implementation, then reverted.
    """
    from engine.render.captions import TranscriptResult

    calls = []

    def fake_transcribe(path, beat_offset_s=0.0):
        calls.append(beat_offset_s)
        return TranscriptResult(words=[], segments=[])

    monkeypatch.setattr("engine.render.captions.transcribe_audio", fake_transcribe)

    vo0 = tmp_path / "beat0.wav"
    vo1 = tmp_path / "beat1.wav"
    vo0.write_bytes(b"x")
    vo1.write_bytes(b"x")

    _build_beat_transcripts([vo0, vo1])

    assert calls == [0.0, 0.0], (
        f"transcribe_audio() was called with offset(s) {calls} — a non-zero, "
        "per-beat value here would double-apply _whisper_timestamps()'s own "
        "beat_start shift"
    )


def test_whisper_timestamps_multi_beat_absolute_times_not_doubled():
    """Numeric-value regression guard, complementing the call-argument guard above:
    feeding two beats' beat-relative .words (as transcribe_audio() with
    beat_offset_s=0.0 actually produces) through _whisper_timestamps() at their real
    cumulative beat_start values must yield exactly beat_start + raw_word_time — not
    beat_start applied twice. This directly re-derives the byte-identical claim the
    system design's §3.1 makes for a multi-beat reel — beat 1's non-zero beat_start
    is exactly where a doubling bug would become visible (beat 0's beat_start is 0,
    so doubling it is invisible there — included for completeness, not as the
    discriminating assertion).
    """
    from dataclasses import dataclass

    @dataclass
    class _Seg:
        text: str
        start_s: float
        end_s: float

    # Beat-relative words, exactly as transcribe_audio(beat_offset_s=0.0) produces.
    beat0_words = [_Seg("Hello", 0.5, 1.0), _Seg("world.", 1.0, 1.8)]
    beat1_words = [_Seg("Goodbye.", 0.2, 0.9)]

    beat0_start, beat0_dur = 0.0, 5.0
    beat1_start, beat1_dur = 5.0, 4.0  # beat 1 starts where beat 0 ends

    timed0 = _whisper_timestamps(["Hello world."], beat0_words, beat0_start, beat0_dur)
    timed1 = _whisper_timestamps(["Goodbye."], beat1_words, beat1_start, beat1_dur)

    _, s0, e0 = timed0[0]
    assert s0 == pytest.approx(0.5)   # beat0_start(0.0) + 0.5, not doubled
    assert e0 == pytest.approx(1.8)

    _, s1, e1 = timed1[0]
    # Correct: 5.0 + 0.2 = 5.2 / 5.0 + 0.9 = 5.9. A doubling bug would instead
    # compute 5.0 + 5.0 + 0.2 = 10.2 (clamped by beat_dur, but still visibly wrong).
    assert s1 == pytest.approx(5.2)
    assert e1 == pytest.approx(5.9)
