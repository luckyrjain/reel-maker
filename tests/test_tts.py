"""Tests for TTS provider selection.

The default used to be "chatterbox", which matches no implementation and fell
through to SilentProvider. Because SilentProvider returns one fixed 1 s file for
every beat, render_cut's "override duration_s with measured audio length" step
then collapsed a 45 s reel to ~1 s per beat — a silent, truncated video that the
pipeline still reported as `done`.
"""
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


def test_silent_provider_returns_one_shared_file(tmp_path):
    """Why render_cut must not measure SilentProvider output for beat durations."""
    provider = SilentProvider(tmp_path)
    assert provider.synthesize("a long beat of narration") == provider.synthesize("short")
