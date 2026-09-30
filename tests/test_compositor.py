"""Integration test for composite_cut — the only test that exercises real MoviePy/ffmpeg.

Added after a live end-to-end run found every rendered video had zero audio
streams. Root cause: engine/render/compositor.py called `.audio_fadein()` /
`.audio_fadeout()` on AudioFileClip — methods that do not exist on MoviePy 2.x
(fades are effects: `.with_effects([AudioFadeIn(d), AudioFadeOut(d)])`). The
AttributeError was swallowed by a bare `except Exception: pass` around the VO
track builder, so every beat silently rendered with no voiceover, with the
job still reporting `done`. This test synthesizes a real WAV, renders one
beat, and asserts the output actually has an audio stream — the assertion
that would have caught the regression before it shipped.
"""
import hashlib
import json
import subprocess
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf
from moviepy import AudioFileClip, VideoFileClip
from PIL import Image

from engine.render.compositor import (
    DEFAULT_TEXT_COLOR, TARGET_H, TARGET_W, _build_beat_clip, _build_collage_clip,
    _build_ffmpeg_args, _build_text_filter,
    _write_thumbnail_candidates,
    composite_cut,
)


def _make_test_video(path, duration_s: float, color: str = "blue") -> None:
    """A tiny real MP4 via ffmpeg's lavfi source -- no external fixture/network
    needed, consistent with this file's own real-ffmpeg testing philosophy."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x240:d={duration_s}",
         "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, check=True,
    )


def _has_audio_stream(path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    streams = json.loads(result.stdout).get("streams", [])
    return any(s.get("codec_type") == "audio" for s in streams)


def _mean_abs_amplitude(path) -> float:
    data, _ = sf.read(str(path))
    return float(np.abs(data).mean())


def _assert_valid_srt_with_cues(path):
    """A real .srt file: sequential numbering, `-->` timestamp lines, at least one cue.
    Doesn't assert exact cue text/timing — that depends on real Whisper's transcription
    of the test's synthesized audio (or the proportional vo_script fallback if Whisper
    produces no segments for it), either of which is a legitimate "real
    Whisper-or-fallback timing" outcome per the design."""
    assert path is not None
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert content.strip(), "SRT file was written but is empty"
    assert "1\n" in content
    assert " --> " in content


def test_composite_cut_preserves_vo_audio(tmp_path):
    vo_path = tmp_path / "beat0.wav"
    sf.write(str(vo_path), np.zeros(24000 * 2, dtype=np.float32), 24000)  # 2s of audio

    beat = {
        "duration_s": 2.0,
        "vo_script": "Test narration.",
        "on_screen_text": ["Test"],
    }
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    duration, thumbnail_candidates, subtitle_path = composite_cut(
        beats=[beat],
        beat_video_paths=[[None]],
        beat_vo_paths=[vo_path],
        output_path=out,
        thumbnail_path=thumb,
    )

    assert out.exists()
    assert _has_audio_stream(out), (
        "rendered video has no audio stream — the VO track builder silently "
        "dropped every beat's audio (see module docstring)"
    )
    # A beat with non-empty vo_script always produces at least a fallback cue, even
    # if this synthesized (silent) WAV makes real Whisper return no segments for it.
    _assert_valid_srt_with_cues(subtitle_path)
    assert subtitle_path == out.with_suffix(".srt")


# ── _write_thumbnail_candidates — no ffmpeg, a fake clip object is enough ───

class _FakeClip:
    """Duck-types the bits of a MoviePy clip _write_thumbnail_candidates uses."""
    def __init__(self, duration):
        self.duration = duration

    def get_frame(self, t):
        return np.zeros((10, 10, 3), dtype=np.uint8)


def test_write_thumbnail_candidates_first_is_the_original_thumbnail_path(tmp_path):
    thumb = tmp_path / "thumb.jpg"
    paths = _write_thumbnail_candidates(_FakeClip(duration=20.0), thumb)
    assert paths[0] == thumb
    assert thumb.exists()


def test_write_thumbnail_candidates_writes_distinct_sibling_files(tmp_path):
    thumb = tmp_path / "thumb.jpg"
    paths = _write_thumbnail_candidates(_FakeClip(duration=20.0), thumb)
    assert len(paths) == len(set(paths)) == 4   # 1 original + 3 extra candidates
    for p in paths:
        assert p.exists()
    assert paths[1].name == "thumb_1.jpg"


def test_write_thumbnail_candidates_clamps_to_a_very_short_clip(tmp_path):
    """A clip barely longer than the safety margin must not produce a negative/zero timestamp crash."""
    thumb = tmp_path / "thumb.jpg"
    paths = _write_thumbnail_candidates(_FakeClip(duration=0.3), thumb)
    assert len(paths) == 4
    for p in paths:
        assert p.exists()


# ── _build_text_filter text_color — no ffmpeg, pure string building ─────────

_ONE_BEAT = [{"duration_s": 5.0, "vo_script": "Test narration.", "on_screen_text": ["Test"]}]


def test_text_filter_uses_default_color_when_none_given():
    chain = _build_text_filter(_ONE_BEAT, [5.0])
    assert f"fontcolor={DEFAULT_TEXT_COLOR}" in chain


def test_text_filter_uses_a_curated_color():
    chain = _build_text_filter(_ONE_BEAT, [5.0], text_color="yellow")
    assert "fontcolor=yellow" in chain


def test_text_filter_falls_back_to_default_for_an_uncurated_color():
    """Defense in depth — api/routers/reels.py already validates against
    CURATED_TEXT_COLORS before storing Reel.text_color, but _build_text_filter() must
    not blindly interpolate an arbitrary string into the ffmpeg filter graph either."""
    chain = _build_text_filter(_ONE_BEAT, [5.0], text_color="not_a_real_color")
    assert f"fontcolor={DEFAULT_TEXT_COLOR}" in chain
    assert "fontcolor=not_a_real_color" not in chain


def test_text_filter_rejects_a_filter_graph_injection_attempt():
    """The color value is interpolated into the filter string with no escaping (unlike
    on-screen text content, which _escape_drawtext() sanitizes) — an attempted injection
    via the color field must be caught by the same curated-set check, not just happen
    not to break anything."""
    chain = _build_text_filter(_ONE_BEAT, [5.0], text_color="white:enable=0,drawbox=1")
    assert f"fontcolor={DEFAULT_TEXT_COLOR}" in chain
    assert "drawbox" not in chain


def test_text_filter_disables_drawtext_expansion():
    """See the real-ffmpeg regression below (test_composite_cut_renders_on_screen_text_
    containing_a_percent_sign) for why: drawtext's own `%`-expansion engine, left at its
    default, treats a lone `%` as a "Stray %" parse error regardless of backslash-escaping
    -- confirmed against real ffmpeg that `%`, `\\%`, and `%%` all fail identically.
    `:expansion=none` on the filter itself is the actual fix; this just pins that the
    option is always present, since the real-ffmpeg test below only proves the combined
    behavior, not which half of the fix supplied it."""
    chain = _build_text_filter(_ONE_BEAT, [5.0])
    assert ":expansion=none" in chain


def test_escape_drawtext_leaves_percent_untouched():
    """`_escape_drawtext()` must not backslash-escape `%` -- with `expansion=none` on the
    filter (see above), a bare `%` already renders correctly, and separate manual
    real-ffmpeg frame-byte comparison (not itself a permanent test here -- see the design
    doc's Correction 1) confirmed `\\%` and `%` render identically under expansion=none,
    so there is nothing left for a `%`-specific escape to accomplish."""
    from engine.render.compositor import _escape_drawtext
    assert _escape_drawtext("50% off") == "50% off"


# ── _build_ffmpeg_args — pure function, no subprocess needed ────────────────

def test_ffmpeg_args_no_music_uses_simple_drawtext_pass():
    args = _build_ffmpeg_args(
        notxt_path="in.mp4", tmp_path="out.mp4", text_filter="drawtext=...",
        music_path=None, has_vo_audio=True,
    )
    assert "-filter_complex" not in args
    assert "-vf" in args
    assert "-c:a" in args and args[args.index("-c:a") + 1] == "copy"


def test_ffmpeg_args_with_music_and_vo_ducks_via_sidechaincompress():
    args = _build_ffmpeg_args(
        notxt_path="in.mp4", tmp_path="out.mp4", text_filter="drawtext=...",
        music_path="music.mp3", has_vo_audio=True, total_duration=12.5,
    )
    filter_complex = args[args.index("-filter_complex") + 1]
    assert "sidechaincompress" in filter_complex
    assert "amix" in filter_complex
    assert "normalize=0" in filter_complex, "without this amix halves the VO's volume"
    assert "atrim=duration=12.500" in filter_complex, (
        "the looped music input needs an explicit stop point — -shortest alone "
        "isn't enough (reproduced as a bogus ffmpeg 'No space left on device' "
        "filtering error without it)"
    )
    assert "-stream_loop" in args and "-1" in args
    assert "-shortest" in args
    assert "-map" in args and "[vout]" in args and "[aout]" in args


def test_ffmpeg_args_with_music_no_vo_skips_ducking():
    args = _build_ffmpeg_args(
        notxt_path="in.mp4", tmp_path="out.mp4", text_filter="drawtext=...",
        music_path="music.mp3", has_vo_audio=False, total_duration=12.5,
    )
    filter_complex = args[args.index("-filter_complex") + 1]
    assert "sidechaincompress" not in filter_complex
    assert "amix" not in filter_complex
    assert "atrim=duration=12.500" in filter_complex
    assert "volume=" in filter_complex


# ── Real end-to-end music mixing (real ffmpeg — no mocking) ─────────────────

def _sine_wav(path, freq=440.0, seconds=2.0, sr=24000, amplitude=0.5):
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    data = (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    sf.write(str(path), data, sr)
    return path


def test_composite_cut_mixes_music_under_vo(tmp_path):
    vo_path = _sine_wav(tmp_path / "vo.wav", freq=880.0, seconds=2.0, amplitude=0.8)
    music_path = _sine_wav(tmp_path / "music.wav", freq=220.0, seconds=5.0, amplitude=0.8)

    beat = {"duration_s": 2.0, "vo_script": "Test narration.", "on_screen_text": ["Test"]}
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    duration, thumbnail_candidates, subtitle_path = composite_cut(
        beats=[beat], beat_video_paths=[[None]], beat_vo_paths=[vo_path],
        output_path=out, thumbnail_path=thumb, music_path=music_path,
    )

    assert out.exists()
    assert _has_audio_stream(out)
    wav_out = tmp_path / "out.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(out), str(wav_out)], capture_output=True, check=True)
    assert _mean_abs_amplitude(wav_out) > 0.001, "mixed output should not be silent"
    _assert_valid_srt_with_cues(subtitle_path)


def test_composite_cut_mixes_music_with_no_vo(tmp_path):
    """voiceover_mode="music_only"/"silent" — no VO track to duck against."""
    music_path = _sine_wav(tmp_path / "music.wav", freq=220.0, seconds=5.0, amplitude=0.8)

    beat = {"duration_s": 2.0, "vo_script": "", "on_screen_text": []}
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    duration, thumbnail_candidates, subtitle_path = composite_cut(
        beats=[beat], beat_video_paths=[[None]], beat_vo_paths=[None],
        output_path=out, thumbnail_path=thumb, music_path=music_path,
    )

    assert out.exists()
    assert _has_audio_stream(out)
    wav_out = tmp_path / "out.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(out), str(wav_out)], capture_output=True, check=True)
    assert _mean_abs_amplitude(wav_out) > 0.001, "music-only output should not be silent"
    # No VO audio and an empty vo_script — nothing to caption anywhere in the fallback
    # chain either, so subtitle_path must be None (write_srt writes nothing for []).
    assert subtitle_path is None


def test_composite_cut_without_music_path_is_unaffected(tmp_path):
    """No music_path (the default) must behave exactly as before this feature."""
    vo_path = _sine_wav(tmp_path / "vo.wav", freq=880.0, seconds=2.0, amplitude=0.8)
    beat = {"duration_s": 2.0, "vo_script": "Test narration.", "on_screen_text": ["Test"]}
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    duration, thumbnail_candidates, subtitle_path = composite_cut(
        beats=[beat], beat_video_paths=[[None]], beat_vo_paths=[vo_path],
        output_path=out, thumbnail_path=thumb,
    )

    assert out.exists()
    assert _has_audio_stream(out)
    _assert_valid_srt_with_cues(subtitle_path)


# ── VideoFileClip reader leak (docs/specs/2026-09-moviepy-reader-leak-system-design.md) ──

class _ReaderTracker:
    """Wraps <clip_cls>.__init__/.close to record every instance opened and
    closed, so a test can assert the two sets are equal -- proving every reader
    actually opened during a render was actually closed, not just that close()
    was called on SOME object (a bare call-counter could pass vacuously if
    construction itself went untracked). Defaults to VideoFileClip; pass
    clip_cls=AudioFileClip to track VO tracks instead."""
    def __init__(self, clip_cls=VideoFileClip):
        self.clip_cls = clip_cls
        self.opened: list = []
        self.closed: list = []
        self._orig_init = clip_cls.__init__
        self._orig_close = clip_cls.close

    def __enter__(self):
        orig_init, orig_close = self._orig_init, self._orig_close
        opened, closed = self.opened, self.closed

        # Plain functions, not bound methods: assigning a bound method as a class
        # attribute breaks Python's normal descriptor auto-binding (instance.foo(...)
        # would call the bound method directly, without injecting the instance as
        # the first arg) -- these closures keep that binding intact.
        def tracking_init(clip_self, *args, **kwargs):
            orig_init(clip_self, *args, **kwargs)
            opened.append(clip_self)

        def tracking_close(clip_self, *args, **kwargs):
            closed.append(clip_self)
            return orig_close(clip_self, *args, **kwargs)

        self._patches = [
            patch.object(self.clip_cls, "__init__", tracking_init),
            patch.object(self.clip_cls, "close", tracking_close),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()

    def assert_every_opened_reader_was_closed(self):
        opened_ids = {id(c) for c in self.opened}
        closed_ids = {id(c) for c in self.closed}
        assert opened_ids, "test setup bug: no VideoFileClip was opened at all"
        assert opened_ids == closed_ids, (
            f"opened {len(opened_ids)} readers, only closed {len(closed_ids)} -- "
            f"leaked: {opened_ids - closed_ids}"
        )


def test_composite_cut_closes_every_video_reader_it_opens(tmp_path):
    """The bug this fix closes: neither concatenate_videoclips()'s default
    "chain" method nor CompositeVideoClip.close() reach the VideoFileClip
    readers nested inside per-beat media clips -- see the design doc. Two
    beats: one whose video is shorter than the beat duration (the loop-replica
    branch -- the probe clip plus N loop copies, the most instances to leak at
    once) and one whose video is already long enough (the single-reader
    branch), so both code paths in _build_media_sub_clip() are covered."""
    short_video = tmp_path / "short.mp4"
    _make_test_video(short_video, duration_s=1.0)   # shorter than beat 0's 3s -> loops
    long_video = tmp_path / "long.mp4"
    _make_test_video(long_video, duration_s=3.0, color="red")   # longer than beat 1's 2s

    beats = [
        {"duration_s": 3.0, "vo_script": "", "on_screen_text": []},
        {"duration_s": 2.0, "vo_script": "", "on_screen_text": []},
    ]
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    with _ReaderTracker() as tracker:
        composite_cut(
            beats=beats,
            beat_video_paths=[[short_video], [long_video]],
            beat_vo_paths=[None, None],
            output_path=out, thumbnail_path=thumb,
        )

    assert out.exists()
    tracker.assert_every_opened_reader_was_closed()


def test_composite_cut_still_closes_readers_when_thumbnail_generation_fails(tmp_path):
    """A design-review correction: _write_thumbnail_candidates() moved inside
    composite_cut()'s try block so a failure there still reaches the cleanup
    path -- it used to run before the try even started, so raising there
    skipped every close() (video readers, vo_tracks, final) entirely."""
    video = tmp_path / "v.mp4"
    _make_test_video(video, duration_s=2.0)
    beats = [{"duration_s": 2.0, "vo_script": "", "on_screen_text": []}]
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    with (
        _ReaderTracker() as tracker,
        patch(
            "engine.render.compositor._write_thumbnail_candidates",
            side_effect=RuntimeError("boom"),
        ),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            composite_cut(
                beats=beats, beat_video_paths=[[video]], beat_vo_paths=[None],
                output_path=out, thumbnail_path=thumb,
            )

    tracker.assert_every_opened_reader_was_closed()


def test_composite_cut_closes_earlier_beats_readers_when_a_later_beat_fails_to_build(tmp_path):
    """A second design-review correction: the per-beat loop that opens video
    readers used to run BEFORE composite_cut()'s try block even started, so a
    later beat failing to build (e.g. a corrupt video file) left every EARLIER
    beat's already-opened reader leaked -- the try/finally that owns
    video_readers had never been entered. The loop is now inside the try."""
    from engine.render.compositor import _build_beat_clip as real_build_beat_clip

    video = tmp_path / "v.mp4"
    _make_test_video(video, duration_s=2.0)
    beats = [
        {"duration_s": 2.0, "vo_script": "", "on_screen_text": []},
        {"duration_s": 2.0, "vo_script": "", "on_screen_text": []},
    ]
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    call_count = {"n": 0}

    def flaky_build_beat_clip(media_paths, duration_s, stack):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated corrupt media on beat 2")
        return real_build_beat_clip(media_paths, duration_s, stack)

    with (
        _ReaderTracker() as tracker,
        patch("engine.render.compositor._build_beat_clip", side_effect=flaky_build_beat_clip),
    ):
        with pytest.raises(RuntimeError, match="simulated corrupt media"):
            composite_cut(
                beats=beats, beat_video_paths=[[video], [None]], beat_vo_paths=[None, None],
                output_path=out, thumbnail_path=thumb,
            )

    tracker.assert_every_opened_reader_was_closed()


def test_composite_cut_propagates_the_original_exception_even_if_a_readers_close_fails(tmp_path):
    """Security/Red-Team review finding on the ExitStack refactor: ExitStack's own
    unwind does NOT protect against a registered reader's close() itself raising --
    that close()-time exception would otherwise REPLACE whatever exception
    triggered the unwind, silently masking (e.g.) a real corrupt-media error behind
    an unrelated "close failed" message in job.error. This codebase's own invariant
    elsewhere (worker/tasks/common.py: "errors while recording a failure ... never
    mask the original exception") requires this not happen here either. Forces
    both a real build failure on beat 2 AND VideoFileClip.close() raising for the
    reader beat 1 already opened -- the ORIGINAL RuntimeError must still be what
    propagates, not the close-time one. Fixed via compositor.py's `_closing()`
    wrapper, which swallows (and logs) a close()-time failure instead of letting
    it reach ExitStack's own unwind."""
    from engine.render.compositor import _build_beat_clip as real_build_beat_clip

    video = tmp_path / "v.mp4"
    _make_test_video(video, duration_s=2.0)
    beats = [
        {"duration_s": 2.0, "vo_script": "", "on_screen_text": []},
        {"duration_s": 2.0, "vo_script": "", "on_screen_text": []},
    ]
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    call_count = {"n": 0}

    def flaky_build_beat_clip(media_paths, duration_s, stack):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated corrupt media on beat 2")
        return real_build_beat_clip(media_paths, duration_s, stack)

    with (
        patch("engine.render.compositor._build_beat_clip", side_effect=flaky_build_beat_clip),
        patch.object(VideoFileClip, "close", side_effect=RuntimeError("close failed")),
    ):
        with pytest.raises(RuntimeError, match="simulated corrupt media on beat 2"):
            composite_cut(
                beats=beats, beat_video_paths=[[video], [None]], beat_vo_paths=[None, None],
                output_path=out, thumbnail_path=thumb,
            )


def test_build_beat_clip_closes_an_earlier_items_reader_when_a_later_item_in_the_same_beat_fails(
    tmp_path,
):
    """A third, deeper gap (found by yet another review pass after the two above
    already shipped): a SINGLE beat can have multiple media_paths (the
    multi-image-per-beat feature). composite_cut()'s own try/finally can only
    close readers _build_beat_clip() actually RETURNS to it -- if the second
    media item in one beat fails inside _build_media_sub_clip() (corrupt
    media), the first item's already-opened reader was never returned to
    anything outside _build_beat_clip()'s own now-abandoned local scope, so no
    outer try/finally can ever reach it. _build_beat_clip() must close its own
    partial state before re-raising -- calling this function directly (not
    through the full composite_cut()/ffmpeg pipeline) to isolate exactly this
    layer."""
    from engine.render.compositor import _build_media_sub_clip as real_build_media_sub_clip

    video = tmp_path / "v.mp4"
    _make_test_video(video, duration_s=2.0)

    call_count = {"n": 0}

    def flaky_build_media_sub_clip(media_path, duration_s, stack):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated corrupt media on the second item")
        return real_build_media_sub_clip(media_path, duration_s, stack)

    with (
        _ReaderTracker() as tracker,
        patch(
            "engine.render.compositor._build_media_sub_clip",
            side_effect=flaky_build_media_sub_clip,
        ),
    ):
        from engine.render.compositor import _build_beat_clip

        with ExitStack() as stack, pytest.raises(RuntimeError, match="simulated corrupt media"):
            _build_beat_clip([video, video], 2.0, stack)

    tracker.assert_every_opened_reader_was_closed()


def test_composite_cut_closes_a_vo_track_opened_but_not_fully_built(tmp_path):
    """A fourth, sibling gap (found by yet another review pass): the VO-track
    AudioFileClip opens BEFORE .subclipped()/.with_effects()/.with_start() run.
    If any of those later steps raises, the enclosing try/except only logs and
    moves to the next beat -- it never appends `track` to vo_tracks, so the
    finally block's vo_tracks close loop can never reach it either. Same bug
    class as the video-reader leaks above, one call site over."""
    vo_path = _sine_wav(tmp_path / "vo.wav", freq=440.0, seconds=3.0)
    beats = [{"duration_s": 2.0, "vo_script": "Test.", "on_screen_text": ["Test"]}]
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    with (
        _ReaderTracker(clip_cls=AudioFileClip) as tracker,
        patch.object(AudioFileClip, "with_effects", side_effect=RuntimeError("boom")),
    ):
        # The except-and-continue swallows this internally (beat renders silent,
        # matching existing behavior for a broken VO track) -- composite_cut()
        # itself must not raise.
        composite_cut(
            beats=beats, beat_video_paths=[[None]], beat_vo_paths=[vo_path],
            output_path=out, thumbnail_path=thumb,
        )

    tracker.assert_every_opened_reader_was_closed()


def test_composite_cut_renders_on_screen_text_containing_a_percent_sign(tmp_path):
    """Real-ffmpeg regression for the drawtext `%`-escaping bug: `_escape_drawtext()`
    used to backslash-escape `%` as `\\%`, but ffmpeg's own generic option-value parser
    strips that single backslash before drawtext's `%`-expansion engine (left at its
    default) ever sees it -- so `\\%` reached drawtext indistinguishable from a bare `%`,
    which is itself a "Stray %" parse error. Confirmed against real ffmpeg that a literal
    `%`, the old `\\%` escape, and `%%` all failed identically. Any beat whose
    on_screen_text contained a percent sign (plausible in this app's sports/stats niche --
    "50% pass completion") crashed the whole render, not just a cosmetic glitch. Fixed
    with `:expansion=none` on the filter itself (disables drawtext's %-expansion engine
    wholesale, including its `%{eif:...}` expression evaluator -- a real, if low-severity,
    injection surface for LLM-generated caption text that this closes as a side effect)
    plus removing the now-unnecessary `\\%` escape. This test fails against the pre-fix
    code with a RuntimeError ("FFmpeg text/audio pass failed (exit 234)") raised from
    composite_cut()'s ffmpeg subprocess call, not from MoviePy's write_videofile (there is
    no separate MoviePy write step in this pass -- see _build_ffmpeg_args())."""
    beats = [{"duration_s": 2.0, "vo_script": "", "on_screen_text": ["Win rate: 50% today"]}]
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    composite_cut(
        beats=beats, beat_video_paths=[[None]], beat_vo_paths=[None],
        output_path=out, thumbnail_path=thumb,
    )

    assert out.exists()


def _frame_md5(filter_chain: str | None, duration_s: float, t: float, out_path) -> str:
    """Render one frame at time `t` on a plain black source at this app's real target
    9:16 dimensions (drawtext's fixed y-position, _TEXT_Y, is computed from TARGET_H and
    lands off-screen on an arbitrary small test frame -- a 320x240 source would make any
    frame-comparison test pass vacuously regardless of what the filter did), through
    `filter_chain` if given, and return its content hash. `filter_chain=None` renders the
    plain color source with no drawtext filter at all, as a "definitely no text" baseline
    for proving text was actually drawn, not just that two frames happened to match."""
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s={TARGET_W}x{TARGET_H}:d={duration_s}"]
    if filter_chain:
        cmd += ["-vf", filter_chain]
    cmd += ["-ss", str(t), "-frames:v", "1", "-update", "1", str(out_path)]
    subprocess.run(cmd, capture_output=True, check=True)
    return hashlib.md5(out_path.read_bytes()).hexdigest()


def test_text_filter_percent_expansion_stays_literal_not_expanded(tmp_path):
    """The security property `:expansion=none` exists for, proven rather than assumed:
    with expansion left at its default, `%{pts}` is drawtext's own syntax for expanding
    to the filter's current timestamp, so a rendered frame at t=0.5s would visibly differ
    from one at t=2.5s. With expansion=none, on_screen_text containing `%{pts}` (a stand-in
    for an LLM-generated caption that happens to contain, or is crafted to contain, a
    `%{...}`-shaped substring) must render as the literal, unchanging text `%{pts}` at
    every timestamp -- i.e. the two frames must be byte-identical. This is a stronger
    guarantee than "ffmpeg doesn't error" (already covered by the sibling percent-sign
    test above): it confirms expansion is actually OFF, not just that this particular
    string happens not to trip a parse error.

    Caught by an independent test-quality audit: the first version of this test rendered
    onto a small 320x240 color source while drawtext's y-position is computed from this
    app's real 1080x1920 target frame (_TEXT_Y = int(TARGET_H * TEXT_Y_CENTER) - ... =
    1371px down) -- the text landed entirely below the visible 240px-tall test frame, so
    BOTH "frames" were identical plain black regardless of the expansion setting, and the
    test passed even with :expansion=none removed. Fixed by rendering at this app's real
    TARGET_W x TARGET_H (matching what `_build_text_filter()`'s `_TEXT_Y` actually assumes)
    and by asserting the text-bearing frame differs from a genuinely textless baseline
    frame, so a future off-screen-text regression fails loudly instead of passing quietly."""
    chain = _build_text_filter(
        [{"duration_s": 3.0, "vo_script": "", "on_screen_text": ["%{pts}"]}], [3.0],
    )

    frame_early = _frame_md5(chain, 3.0, 0.5, tmp_path / "early.png")
    frame_late = _frame_md5(chain, 3.0, 2.5, tmp_path / "late.png")
    frame_no_text = _frame_md5(None, 3.0, 0.5, tmp_path / "no_text.png")

    assert frame_early != frame_no_text   # text is actually visible in the frame
    assert frame_early == frame_late


# ── 2-up collage layout (docs/specs/2026-09-multi-image-collage-system-design.md) ──

def _sample_pixel(video_path, t: float, x_frac: float, y_frac: float, out_path) -> tuple:
    """Extract the frame at time `t` from a real rendered MP4 and return the RGB
    pixel at the given fractional (x_frac, y_frac) position -- used to prove two
    collage halves are visible SIMULTANEOUSLY at the same timestamp, which a
    doesn't-crash-only test can't distinguish from the old sequential cycling."""
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(t), "-i", str(video_path), "-frames:v", "1", str(out_path)],
        capture_output=True, check=True,
    )
    img = Image.open(out_path).convert("RGB")
    x, y = int(img.width * x_frac), int(img.height * y_frac)
    return img.getpixel((x, y))


def _assert_close_to(rgb, expected, tol=40):
    assert all(abs(a - b) <= tol for a, b in zip(rgb, expected)), f"{rgb} not close to {expected}"


def test_composite_cut_collage_shows_both_items_simultaneously(tmp_path):
    """The actual bug this design fixes: a 2-item beat used to cycle
    sequentially, each item visible for only half the beat. Two distinctly
    colored real videos, sampled on the LEFT and RIGHT quarters of the frame at
    the SAME timestamp (the beat's midpoint): only a collage shows red on the
    left AND blue on the right at once -- the old sequential path would show
    only one color at any given instant. Explicit RGB tolerance (not exact
    equality) to absorb H.264 compression drift."""
    red_video = tmp_path / "red.mp4"
    _make_test_video(red_video, duration_s=2.0, color="red")
    blue_video = tmp_path / "blue.mp4"
    _make_test_video(blue_video, duration_s=2.0, color="blue")

    beats = [{"duration_s": 2.0, "vo_script": "", "on_screen_text": []}]
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    composite_cut(
        beats=beats,
        beat_video_paths=[[red_video, blue_video]],
        beat_vo_paths=[None],
        output_path=out, thumbnail_path=thumb,
    )

    left_rgb = _sample_pixel(out, 1.0, 0.25, 0.5, tmp_path / "left.png")
    right_rgb = _sample_pixel(out, 1.0, 0.75, 0.5, tmp_path / "right.png")

    _assert_close_to(left_rgb, (255, 0, 0))
    _assert_close_to(right_rgb, (0, 0, 255))


def test_composite_cut_closes_readers_for_a_collage_beat(tmp_path):
    """The collage path's own reader-leak regression, mirroring
    test_composite_cut_closes_every_video_reader_it_opens() above for the
    sequential path -- two real videos in ONE beat, both opened via
    _build_media_sub_clip() inside _build_collage_clip(), must both be closed
    by composite_cut()'s existing finally block."""
    red_video = tmp_path / "red2.mp4"
    _make_test_video(red_video, duration_s=2.0, color="red")
    blue_video = tmp_path / "blue2.mp4"
    _make_test_video(blue_video, duration_s=2.0, color="blue")

    beats = [{"duration_s": 2.0, "vo_script": "", "on_screen_text": []}]
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    with _ReaderTracker() as tracker:
        composite_cut(
            beats=beats,
            beat_video_paths=[[red_video, blue_video]],
            beat_vo_paths=[None],
            output_path=out, thumbnail_path=thumb,
        )

    assert out.exists()
    tracker.assert_every_opened_reader_was_closed()


def test_build_beat_clip_does_not_use_collage_for_one_or_three_items():
    """Collage is scoped to EXACTLY 2 items -- 1-item and 3+-item beats keep
    today's sequential-cycling behavior unchanged. Mocking _build_collage_clip
    to raise if it's ever called proves the boundary directly rather than
    inferring it from output pixels.

    Also restores (via _ReaderTracker) a property the pre-ExitStack version of
    this test proved via `readers1 == readers3 == []`: a None/missing
    media_path must never open a real VideoFileClip reader. Losing that
    assertion when the tuple return was dropped was a real Test-Quality gap
    caught on review -- mutation-tested against a version of
    _build_media_sub_clip() that opens an unnecessary real reader for the
    None/missing-path branch, which every other test in this file passed
    against undetected."""
    with (
        _ReaderTracker() as tracker,
        patch(
            "engine.render.compositor._build_collage_clip",
            side_effect=AssertionError("collage must not be used for a non-2-item beat"),
        ),
        ExitStack() as stack,
    ):
        clip1 = _build_beat_clip([None], 2.0, stack)
        clip3 = _build_beat_clip([None, None, None], 3.0, stack)

    assert clip1.duration == 2.0
    assert clip3.duration == 3.0
    assert tracker.opened == [], "a None/missing media_path must never open a real VideoFileClip reader"


def test_build_beat_clip_collage_respects_the_duration_floor(tmp_path):
    """Regression for a design-review-caught bug: the `len(media_paths) == 2`
    branch in _build_beat_clip() must sit AFTER the existing
    `duration_s = max(duration_s, 0.5)` floor, not before it -- placed before,
    a short real beat would reach _build_collage_clip() with an unfloored
    (near-zero) duration, diverging from every other path's guarantee.
    Mutation-tested by moving the branch above the floor line and confirming
    this then fails with clip.duration == 0.1 instead of 0.5."""
    video = tmp_path / "v.mp4"
    _make_test_video(video, duration_s=1.0)

    with ExitStack() as stack:
        clip = _build_beat_clip([video, video], 0.1, stack)
        assert clip.duration == 0.5


def test_build_collage_clip_closes_both_readers_when_the_crop_or_composite_step_fails(tmp_path):
    """A second review round (Security/Red-Team, on the opened PR) caught a gap
    the first draft's try/except didn't cover: it only closed readers when the
    SECOND _build_media_sub_clip() call raised. Once BOTH sub-clips already
    succeeded, _center_crop_half()/the CompositeVideoClip(...) construction ran
    with no exception handling at all -- either raising there would leak two
    real VideoFileClip readers with no caller ever able to reach them, the
    identical leak class this codebase mutation-tested and fixed four times
    already for the sequential path (Phase 7i). Forces the failure in
    _center_crop_half() -- after both real videos have already opened readers
    -- and asserts both are closed, not just the first item's."""
    left_video = tmp_path / "left.mp4"
    _make_test_video(left_video, duration_s=2.0, color="red")
    right_video = tmp_path / "right.mp4"
    _make_test_video(right_video, duration_s=2.0, color="blue")

    with (
        _ReaderTracker() as tracker,
        patch(
            "engine.render.compositor._center_crop_half",
            side_effect=RuntimeError("simulated crop failure after both sub-clips succeeded"),
        ),
    ):
        with ExitStack() as stack, pytest.raises(RuntimeError, match="simulated crop failure"):
            _build_collage_clip([left_video, right_video], 2.0, stack)

    tracker.assert_every_opened_reader_was_closed()


def test_build_collage_clip_with_one_missing_path_renders_a_black_half(tmp_path):
    """Defensive-only case (real code never produces a mixed real/missing pair
    today -- see the design doc's Failure-strategy section) -- but
    _build_collage_clip() must not crash if it ever does: the missing side
    degrades to a black half via _build_media_sub_clip()'s own existing
    None/missing-file handling, same as a single-item beat already does.

    A Test-Quality Auditor review caught the first version of this test
    proving only "doesn't crash + right shape" -- any non-crashing fill of the
    right dimensions would have passed, black or not. Samples the RIGHT
    half's actual pixel content (media_paths[1] is the missing path) via
    clip.get_frame() -- a pure in-memory MoviePy read, no ffmpeg render
    needed -- and asserts it's genuinely near-black, not just present."""
    red_video = tmp_path / "red_v.mp4"
    _make_test_video(red_video, duration_s=2.0, color="red")
    missing = tmp_path / "does_not_exist.mp4"

    with _ReaderTracker() as tracker, ExitStack() as stack:
        clip = _build_collage_clip([red_video, missing], 2.0, stack)
        assert clip.duration == 2.0
        assert clip.size == (TARGET_W, TARGET_H)
        assert tracker.opened, "the real red_video side should have opened at least one reader"

        frame = clip.get_frame(1.0)
        right_pixel = frame[TARGET_H // 2, int(TARGET_W * 0.75)]
        assert all(c <= 20 for c in right_pixel[:3]), (
            f"missing-path half should render near-black, got {right_pixel}"
        )
    tracker.assert_every_opened_reader_was_closed()
