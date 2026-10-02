"""Tests for engine/names.py — the person-name extractor evaluator, visual_fallback and
asset_sourcer share."""
import pytest

from engine.names import first_person_name, person_names


def test_person_names_extracts_correctly():
    names = person_names("Lionel Messi and Cristian Romero play for Argentina in the World Cup.")
    assert "Lionel Messi" in names
    assert "Cristian Romero" in names


def test_person_names_excludes_non_persons():
    names = person_names("World Cup and Premier League are tournaments. Real Madrid won.")
    assert not any(n.lower() in {"world cup", "premier league", "real madrid"} for n in names)


def test_person_names_keeps_order():
    assert person_names("Cristian Romero then Lionel Messi") == ["Cristian Romero", "Lionel Messi"]


@pytest.mark.parametrize("text,expected", [
    # short particles are part of the name (the old 3-letter-minimum regex dropped these)
    ("Angel Di Maria scores", ["Angel Di Maria"]),
    ("Rodrigo De Paul presses", ["Rodrigo De Paul"]),
    ("Giovani Lo Celso passes", ["Giovani Lo Celso"]),
])
def test_short_name_particles_match(text, expected):
    assert person_names(text) == expected


@pytest.mark.parametrize("text", [
    "Real Madrid derby", "Real Betis won", "Inter Milan lost", "United Kingdom", "East Germany",
    "Premier League highlights", "Premier Division title", "Manchester United scored",
    "Manchester City won", "South American side", "West European clubs", "Copa America final",
    "Champions League night", "World Cup final",
])
def test_non_persons_are_excluded(text):
    assert person_names(text) == []


@pytest.mark.parametrize("text", [
    "Because Messi scored", "However Romero defended", "The Argentina side", "Ask France",
])
def test_sentence_openers_are_not_names(text):
    assert person_names(text) == []


def test_a_person_whose_surname_is_a_club_word_is_still_a_person():
    # club/region words block only as the FIRST word; "Kanye West" must not be dropped
    assert person_names("Kanye West performs") == ["Kanye West"]


def test_first_person_name():
    assert first_person_name("World Cup hero Lionel Messi and Cristian Romero") == "Lionel Messi"
    assert first_person_name("no names at all") is None
    assert first_person_name("") is None
