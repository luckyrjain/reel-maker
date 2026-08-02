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

from engine.render.compositor import composite_cut


def _has_audio_stream(path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    streams = json.loads(result.stdout).get("streams", [])
    return any(s.get("codec_type") == "audio" for s in streams)


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
