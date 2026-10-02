"""Characterization tests for visual_fallback._first_person (previously untested)."""
import pytest

from engine.generation.visual_fallback import _first_person


def test_existing_player_wins_over_any_name_in_the_vo():
    assert _first_person("Lionel Messi dribbling", "Emiliano Martinez") == "Emiliano Martinez"


@pytest.mark.parametrize("vo,expected", [
    ("Lionel Messi dribbling past three defenders", "Lionel Messi"),
    ("Messi and Cristian Romero press high", "Cristian Romero"),
    ("no names here at all", ""),
    ("", ""),
    # non-person capitalized phrases are skipped
    ("Real Madrid derby", ""),
    ("Premier League highlights", ""),
    ("United Kingdom", ""),
    ("East Germany", ""),
    ("Inter Milan lost", ""),
    ("World Cup final", ""),
    # the first plausible name wins, skipping a non-person ahead of it
    ("World Cup hero Lionel Messi", "Lionel Messi"),
])
def test_first_person_current_behavior(vo, expected):
    assert _first_person(vo, "") == expected
