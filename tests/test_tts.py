"""Tests for TTS provider selection.

The default used to be "chatterbox", which matches no implementation and fell
through to SilentProvider. Because SilentProvider returns one fixed 1 s file for
every beat, render_cut's "override duration_s with measured audio length" step
then collapsed a 45 s reel to ~1 s per beat — a silent, truncated video that the
pipeline still reported as `done`.
"""
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from api.config import Settings
from engine.render.tts import EdgeTTSProvider, SilentProvider, get_tts_provider


def test_default_provider_is_a_real_engine():
    """The shipped default must produce audio, not silently degrade to silence."""
    assert Settings.model_fields["tts_provider"].default == "edge"


def test_unknown_provider_falls_back_to_silent(tmp_path):
    with patch("engine.render.tts.settings") as mock_settings:
        mock_settings.tts_provider = "chatterbox"
        assert isinstance(get_tts_provider(tmp_path), SilentProvider)


def test_edge_provider_selected_when_configured(tmp_path):
    pytest.importorskip("edge_tts")
    with patch("engine.render.tts.settings") as mock_settings:
        mock_settings.tts_provider = "edge"
        assert isinstance(get_tts_provider(tmp_path), EdgeTTSProvider)


# ── per-reel voice override ──────────────────────────────────────────────────

def test_curated_voice_is_threaded_through_to_edge_provider(tmp_path):
    pytest.importorskip("edge_tts")
    with patch("engine.render.tts.settings") as mock_settings:
        mock_settings.tts_provider = "edge"
        provider = get_tts_provider(tmp_path, voice="en-US-JennyNeural")
    assert isinstance(provider, EdgeTTSProvider)
    assert provider.voice == "en-US-JennyNeural"


def test_no_voice_uses_the_provider_default(tmp_path):
    pytest.importorskip("edge_tts")
    with patch("engine.render.tts.settings") as mock_settings:
        mock_settings.tts_provider = "edge"
        provider = get_tts_provider(tmp_path, voice=None)
    assert provider.voice == EdgeTTSProvider.DEFAULT_VOICE


def test_uncurated_voice_falls_back_to_the_default_instead_of_reaching_edge_tts(tmp_path):
    """Defense in depth — api/routers/reels.py already validates against
    CURATED_EDGE_VOICES before storing Reel.tts_voice, but get_tts_provider() must
    not blindly forward an arbitrary string to edge_tts.Communicate() either."""
    pytest.importorskip("edge_tts")
    with patch("engine.render.tts.settings") as mock_settings:
        mock_settings.tts_provider = "edge"
        provider = get_tts_provider(tmp_path, voice="not-a-real-voice")
    assert provider.voice == EdgeTTSProvider.DEFAULT_VOICE


def test_voice_override_is_ignored_for_kokoro(tmp_path):
    """Kokoro's voice IDs (e.g. 'af_heart') are a different namespace than edge-tts's —
    an edge voice name must never reach KokoroProvider."""
    pytest.importorskip("kokoro")
    with patch("engine.render.tts.settings") as mock_settings:
        mock_settings.tts_provider = "kokoro"
        provider = get_tts_provider(tmp_path, voice="en-US-JennyNeural")
    assert provider.voice == "af_heart"   # KokoroProvider's own default, untouched


def test_silent_provider_returns_one_shared_file(tmp_path):
    """Why render_cut must not measure SilentProvider output for beat durations."""
    provider = SilentProvider(tmp_path)
    assert provider.synthesize("a long beat of narration") == provider.synthesize("short")


# ── synth_to_budget rate clamping ─────────────────────────────────────────


def _budget_provider(tmp_path):
    """EdgeTTSProvider with synthesize stubbed to record the rate it is asked for."""
    provider = EdgeTTSProvider(tmp_path)
    calls = []

    def fake_synthesize(text, rate="+0%"):
        calls.append(rate)
        return tmp_path / "out.mp3"

    provider.synthesize = fake_synthesize
    return provider, calls


def test_no_resynth_when_within_tolerance(tmp_path):
    provider, calls = _budget_provider(tmp_path)
    with patch("engine.render.tts._audio_duration", return_value=10.5):
        provider.synth_to_budget("some narration", target_s=10.0)
    assert calls == ["+0%"], "5% drift is inside the 15% tolerance — no second synth"


def test_overlong_audio_speeds_up_and_clamps_to_plus_25(tmp_path):
    """14s of audio for a 10s beat is +40% drift; the rate must clamp to +25%."""
    provider, calls = _budget_provider(tmp_path)
    with patch("engine.render.tts._audio_duration", return_value=14.0):
        provider.synth_to_budget("some narration", target_s=10.0)
    assert calls == ["+0%", "+25%"]


def test_short_audio_slows_down_and_clamps_to_minus_25(tmp_path):
    """5s of audio for a 10s beat is -50% drift; the rate must clamp to -25%."""
    provider, calls = _budget_provider(tmp_path)
    with patch("engine.render.tts._audio_duration", return_value=5.0):
        provider.synth_to_budget("some narration", target_s=10.0)
    assert calls == ["+0%", "-25%"]


def test_unmeasurable_audio_returns_first_take(tmp_path):
    provider, calls = _budget_provider(tmp_path)
    with patch("engine.render.tts._audio_duration", return_value=None):
        provider.synth_to_budget("some narration", target_s=10.0)
    assert calls == ["+0%"]


# ── synthesize() network hang/failure hardening ──────────────────────────


def test_synthesize_writes_atomically_no_tmp_file_left_behind(tmp_path):
    pytest.importorskip("edge_tts")
    provider = EdgeTTSProvider(tmp_path)

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%"):
            pass

        async def save(self, path):
            Path(path).write_bytes(b"fake-mp3")

    with patch("edge_tts.Communicate", FakeCommunicate):
        out = provider.synthesize("hello world")

    assert out.exists()
    assert out.read_bytes() == b"fake-mp3"
    assert not out.with_suffix(out.suffix + ".tmp").exists()


def test_synthesize_cleans_up_tmp_file_when_the_final_replace_fails(tmp_path):
    """A successful download followed by a failed rename (disk full, permission
    error) must not leak the `.tmp` file — `tmp.replace(out)` has to be inside
    the same try/except as the write, not after it. Discriminates true atomic
    write from a version that only guards the download step: on a version where
    `tmp.replace(out)` sits outside the try/except, this scenario leaks the
    `.tmp` file because nothing ever cleans it up."""
    pytest.importorskip("edge_tts")
    provider = EdgeTTSProvider(tmp_path)

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%"):
            pass

        async def save(self, path):
            Path(path).write_bytes(b"fake-mp3")

    with patch("edge_tts.Communicate", FakeCommunicate), \
         patch("pathlib.Path.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            provider.synthesize("hello world")

    leftovers = list(tmp_path.glob("*.mp3")) + list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"leftover file(s) after a failed rename: {leftovers}"


def test_synthesize_retries_once_after_a_timeout(tmp_path):
    """A hung connection to the edge-tts endpoint must not block forever —
    it gets one retry after SYNTH_TIMEOUT_S, not an indefinite stall."""
    pytest.importorskip("edge_tts")
    provider = EdgeTTSProvider(tmp_path)
    provider.SYNTH_TIMEOUT_S = 0.05
    attempts = []

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%"):
            pass

        async def save(self, path):
            attempts.append(1)
            if len(attempts) == 1:
                await asyncio.sleep(1)  # exceeds SYNTH_TIMEOUT_S, forces a timeout
            Path(path).write_bytes(b"fake-mp3")

    with patch("edge_tts.Communicate", FakeCommunicate):
        out = provider.synthesize("hello world")

    assert len(attempts) == 2, "expected exactly one retry after the timeout"
    assert out.exists()


def test_synthesize_failed_attempt_does_not_cache_a_truncated_file(tmp_path):
    """A write failure must not leave a partial file at the cache path — the
    `if out.exists()` cache check above would otherwise reuse that corrupt file
    forever, the same failure class this codebase already guards against for
    every other downloader (see CLAUDE.md's Atomic file writes convention)."""
    pytest.importorskip("edge_tts")
    provider = EdgeTTSProvider(tmp_path)

    class FailingCommunicate:
        def __init__(self, text, voice, rate="+0%"):
            pass

        async def save(self, path):
            Path(path).write_bytes(b"partial-garbage")
            raise RuntimeError("network dropped mid-write")

    with patch("edge_tts.Communicate", FailingCommunicate):
        with pytest.raises(RuntimeError):
            provider.synthesize("hello world")

    leftovers = list(tmp_path.glob("*.mp3")) + list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"leftover cache-poisoning file(s): {leftovers}"
