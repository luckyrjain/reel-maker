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
import json
import subprocess

import numpy as np
import soundfile as sf

from engine.render.compositor import (
    DEFAULT_TEXT_COLOR, _build_ffmpeg_args, _build_text_filter, _write_thumbnail_candidates,
    composite_cut,
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

    composite_cut(
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

    composite_cut(
        beats=[beat], beat_video_paths=[[None]], beat_vo_paths=[vo_path],
        output_path=out, thumbnail_path=thumb, music_path=music_path,
    )

    assert out.exists()
    assert _has_audio_stream(out)
    wav_out = tmp_path / "out.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(out), str(wav_out)], capture_output=True, check=True)
    assert _mean_abs_amplitude(wav_out) > 0.001, "mixed output should not be silent"


def test_composite_cut_mixes_music_with_no_vo(tmp_path):
    """voiceover_mode="music_only"/"silent" — no VO track to duck against."""
    music_path = _sine_wav(tmp_path / "music.wav", freq=220.0, seconds=5.0, amplitude=0.8)

    beat = {"duration_s": 2.0, "vo_script": "", "on_screen_text": []}
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    composite_cut(
        beats=[beat], beat_video_paths=[[None]], beat_vo_paths=[None],
        output_path=out, thumbnail_path=thumb, music_path=music_path,
    )

    assert out.exists()
    assert _has_audio_stream(out)
    wav_out = tmp_path / "out.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(out), str(wav_out)], capture_output=True, check=True)
    assert _mean_abs_amplitude(wav_out) > 0.001, "music-only output should not be silent"


def test_composite_cut_without_music_path_is_unaffected(tmp_path):
    """No music_path (the default) must behave exactly as before this feature."""
    vo_path = _sine_wav(tmp_path / "vo.wav", freq=880.0, seconds=2.0, amplitude=0.8)
    beat = {"duration_s": 2.0, "vo_script": "Test narration.", "on_screen_text": ["Test"]}
    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    composite_cut(
        beats=[beat], beat_video_paths=[[None]], beat_vo_paths=[vo_path],
        output_path=out, thumbnail_path=thumb,
    )

    assert out.exists()
    assert _has_audio_stream(out)
