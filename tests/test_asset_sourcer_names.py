"""Which capitalized phrases resolve_beat_assets() sends to Wikipedia as person names."""
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


def _wiki_lookups(query: str, sleeps: list | None = None) -> list[str]:
    wiki = _RecordingWiki()
    with patch("engine.render.asset_sourcer.time.sleep") as sleep:
        resolve_beat_assets(None, query, 5.0, _NoneSourcer(), wiki=wiki)
    if sleeps is not None:
        sleeps.extend(c.args[0] for c in sleep.call_args_list)
    return wiki.names


def test_a_full_name_is_looked_up():
    assert _wiki_lookups("Lionel Messi through-ball") == ["Lionel Messi"]


def test_two_names_are_each_looked_up_in_order():
    assert _wiki_lookups("Lionel Messi and Cristian Romero") == ["Lionel Messi", "Cristian Romero"]


def test_short_name_particles_are_kept_in_the_lookup():
    assert _wiki_lookups("Angel Di Maria scores") == ["Angel Di Maria"]
    assert _wiki_lookups("Rodrigo De Paul presses") == ["Rodrigo De Paul"]


def test_non_person_phrases_are_not_looked_up():
    # Changed by the shared engine/names.py: the sourcer used to have no exclusions at all and
    # sent clubs and tournaments to Wikipedia as if they were people.
    assert _wiki_lookups("Real Madrid Bernabeu stadium") == []
    assert _wiki_lookups("Premier League highlights") == []
    assert _wiki_lookups("Real Madrid derby with Lionel Messi") == ["Lionel Messi"]


def test_no_names_means_no_lookup():
    assert _wiki_lookups("stadium crowd wide shot") == []


def test_rate_limit_pause_happens_between_names_only():
    one, two = [], []
    _wiki_lookups("Lionel Messi through-ball", one)
    _wiki_lookups("Lionel Messi and Cristian Romero", two)
    assert one == []
    assert two == [0.5]


def test_a_leading_opener_does_not_leak_into_the_wikipedia_query():
    assert _wiki_lookups("Is Vinicius Junior the best?") == ["Vinicius Junior"]
