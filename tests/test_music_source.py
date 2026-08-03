"""Tests for engine/render/asset_sourcer.py::LocalMusicSource."""
from engine.render.asset_sourcer import LocalMusicSource, get_music_sourcer


def _touch(path):
    path.write_bytes(b"")
    return path


def test_no_match_when_library_dir_missing(tmp_path):
    source = LocalMusicSource(tmp_path / "does_not_exist")
    assert source.find("tense minimal") is None


def test_no_match_when_library_empty(tmp_path):
    source = LocalMusicSource(tmp_path)
    assert source.find("tense minimal") is None


def test_none_for_empty_or_missing_cue(tmp_path):
    _touch(tmp_path / "tense_minimal_01.mp3")
    source = LocalMusicSource(tmp_path)
    assert source.find(None) is None
    assert source.find("") is None
    assert source.find("   ") is None


def test_matches_best_overlapping_track(tmp_path):
    _touch(tmp_path / "tense_minimal_01.mp3")
    _touch(tmp_path / "upbeat_energetic_02.mp3")
    _touch(tmp_path / "uplifting_03.wav")
    source = LocalMusicSource(tmp_path)

    assert source.find("tense minimal") == tmp_path / "tense_minimal_01.mp3"
    assert source.find("uplifting") == tmp_path / "uplifting_03.wav"


def test_picks_higher_overlap_score_over_partial_match(tmp_path):
    _touch(tmp_path / "upbeat_01.mp3")
    _touch(tmp_path / "upbeat_energetic_02.mp3")
    source = LocalMusicSource(tmp_path)

    assert source.find("upbeat energetic") == tmp_path / "upbeat_energetic_02.mp3"


def test_no_match_returns_none_rather_than_a_random_track(tmp_path):
    _touch(tmp_path / "tense_minimal_01.mp3")
    source = LocalMusicSource(tmp_path)
    assert source.find("completely unrelated words") is None


def test_ignores_non_audio_files(tmp_path):
    _touch(tmp_path / "tense_minimal.txt")
    _touch(tmp_path / "readme.md")
    source = LocalMusicSource(tmp_path)
    assert source.find("tense minimal") is None


def test_get_music_sourcer_uses_configured_library_dir(monkeypatch):
    from api.config import settings
    monkeypatch.setattr(settings, "music_library_dir", "/some/configured/path")
    source = get_music_sourcer()
    assert str(source.library_dir) == "/some/configured/path"
