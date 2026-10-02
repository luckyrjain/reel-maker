"""Characterization of which capitalized phrases resolve_beat_assets() sends to Wikipedia."""
from unittest.mock import patch

from engine.render.asset_sourcer import resolve_beat_assets


class _NoneSourcer:
    def search(self, query, min_duration_s):
        return None


class _RecordingWiki:
    def __init__(self):
        self.names = []

    def search(self, name):
        self.names.append(name)
        return None


def _wiki_lookups(query: str) -> list[str]:
    wiki = _RecordingWiki()
    with patch("engine.render.asset_sourcer.time.sleep"):
        resolve_beat_assets(None, query, 5.0, _NoneSourcer(), wiki=wiki)
    return wiki.names


def test_a_full_name_is_looked_up():
    assert _wiki_lookups("Lionel Messi through-ball") == ["Lionel Messi"]


def test_two_names_are_each_looked_up_in_order():
    assert _wiki_lookups("Lionel Messi and Cristian Romero") == ["Lionel Messi", "Cristian Romero"]


def test_short_name_particles_are_kept_in_the_lookup():
    assert _wiki_lookups("Angel Di Maria scores") == ["Angel Di Maria"]
    assert _wiki_lookups("Rodrigo De Paul presses") == ["Rodrigo De Paul"]


def test_non_person_phrases_are_currently_looked_up_too():
    # CURRENT behavior: no exclusion list at all. Changed on purpose by the shared-names PR.
    assert _wiki_lookups("Real Madrid Bernabeu stadium") == ["Real Madrid Bernabeu"]
    assert _wiki_lookups("Premier League highlights") == ["Premier League"]


def test_no_names_means_no_lookup():
    assert _wiki_lookups("stadium crowd wide shot") == []
