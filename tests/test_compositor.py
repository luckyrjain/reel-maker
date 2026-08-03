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
import pytest
import soundfile as sf

from engine.render.compositor import _build_ffmpeg_args, composite_cut


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
