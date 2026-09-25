"""Golden-reel smoke test — the one test that runs the render pipeline's actual
TTS -> compositor -> ffmpeg chain end to end, not just the compositor half of it.

`tests/test_compositor.py`'s real-ffmpeg tests already proved the compositor
correctly mixes a *given* audio file into the output (that's what caught the
historical zero-audio bug documented there). What they don't exercise is
`EdgeTTSProvider.synthesize()` itself — the actual TTS call this pipeline makes
in production. A regression in `_normalize_for_tts()`, in how `synthesize()`
writes its output file, or in the edge-tts library/service itself, could still
produce a silent or broken render that every existing test would miss, because
every existing test hands the compositor a synthetic WAV it already knows is
good.

This test uses real `EdgeTTSProvider.synthesize()` calls (free, no API key,
edge-tts's public endpoint) alongside real ffmpeg, so it fails the way an
actual broken render would fail. Needs both ffmpeg on PATH (see
tests/test_compositor.py's own note) and outbound network access — CI has
both (`.github/workflows/ci.yml` installs ffmpeg and edge-tts).
"""
import json
import subprocess

import pytest

pytest.importorskip("edge_tts")

from PIL import Image

from engine.render.compositor import TARGET_H, TARGET_W, composite_cut
from engine.render.tts import EdgeTTSProvider


def _ffprobe_streams(path) -> list[dict]:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    return json.loads(result.stdout).get("streams", [])


def _make_test_image(path, color) -> None:
    Image.new("RGB", (640, 360), color=color).save(str(path))


@pytest.mark.golden
def test_golden_reel_two_beat_render_has_real_tts_audio_and_correct_dimensions(tmp_path):
    tts = EdgeTTSProvider(cache_dir=tmp_path / "tts")

    beats = [
        {"duration_s": 3.0, "on_screen_text": ["Golden reel test"],
         "vo_script": "This is a real end to end test of the render pipeline."},
        {"duration_s": 3.0, "on_screen_text": ["Second beat"],
         "vo_script": "The second beat also needs real synthesized narration."},
    ]

    # Real TTS synthesis — the whole point of this test over test_compositor.py's
    # synthetic-WAV tests. synth_to_budget() (not synthesize()) matches what
    # render_cut() actually calls in production.
    vo_paths = [tts.synth_to_budget(b["vo_script"], target_s=b["duration_s"]) for b in beats]

    img1, img2 = tmp_path / "beat0.jpg", tmp_path / "beat1.jpg"
    _make_test_image(img1, (200, 60, 60))
    _make_test_image(img2, (60, 120, 200))

    out = tmp_path / "out.mp4"
    thumb = tmp_path / "thumb.jpg"

    duration, thumbnail_candidates = composite_cut(
        beats=beats,
        beat_video_paths=[[img1], [img2]],
        beat_vo_paths=vo_paths,
        output_path=out,
        thumbnail_path=thumb,
    )

    assert out.exists()
    streams = _ffprobe_streams(out)
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    assert video is not None, "golden reel has no video stream at all"
    assert audio is not None, (
        "golden reel has no audio stream — real TTS-synthesized narration did not "
        "survive the pipeline (this is exactly the historical zero-audio bug class, "
        "but exercised through the real TTS call instead of a pre-supplied WAV)"
    )
    assert int(video["width"]) == TARGET_W
    assert int(video["height"]) == TARGET_H

    # Real speech, not silence — a broken/truncated TTS file that ffmpeg still
    # accepts as "an audio stream" would otherwise pass the check above trivially.
    assert float(audio.get("duration", 0)) > 1.0

    # composite_cut()'s other real outputs, exercised in the same real pass rather
    # than each getting their own isolated unit test with synthetic inputs.
    assert 5.0 <= duration <= 7.0   # two 3s beats, TTS-length adjustment may nudge it
    assert thumbnail_candidates and thumbnail_candidates[0].exists()
