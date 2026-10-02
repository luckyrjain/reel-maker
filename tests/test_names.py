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


@pytest.mark.parametrize("text,expected", [
    ("Julián Álvarez scores", ["Julián Álvarez"]),
    ("Enzo Fernández presses", ["Enzo Fernández"]),
    ("Peña Nieto speaks", ["Peña Nieto"]),
    ("Ángel Di María scores", ["Ángel Di María"]),
    ("Ñandú Óscar arrives", ["Ñandú Óscar"]),
])
def test_accented_names_match(text, expected):
    assert person_names(text) == expected


# ── sentence openers are stripped, not part of the name ──────────────────────

@pytest.mark.parametrize("opener", [
    "Ask", "Can", "The", "This", "That", "And", "But", "For", "Because", "Although", "However",
    "Without", "Despite", "Unlike", "Within", "Against", "Between", "During",
    "Is", "In", "So", "If", "As", "On", "At", "By", "To", "Of", "He", "We", "It", "No", "Up",
    "Be", "Or", "My", "Us", "Are", "Was", "Does", "Did", "Who", "What", "How", "Why", "When",
    "Watch",
])
def test_a_leading_opener_is_stripped_and_the_name_kept(opener):
    assert person_names(f"{opener} Lionel Messi scores") == ["Lionel Messi"]


@pytest.mark.parametrize("text", [
    "Can Messi do it", "Ask Messi", "Although Messi tried", "Is Haaland back", "So Haaland scored",
    "In Argentina they love him", "To Messi", "In Madrid, La Liga fans say so",
])
def test_an_opener_followed_by_a_single_word_is_not_a_name(text):
    assert person_names(text) == []


def test_an_interrogative_hook_keeps_the_player_name():
    # The app's core hook format: "Is X the best?" — the opener must not become part of the name.
    assert person_names("Is Vinicius Junior the best player alive?") == ["Vinicius Junior"]
    assert first_person_name("To Lionel Messi, it was everything") == "Lionel Messi"


def test_name_particles_and_will_are_not_openers():
    assert person_names("Will Smith attends") == ["Will Smith"]
    assert person_names("Giovani Lo Celso passes") == ["Giovani Lo Celso"]


# ── club / region prefixes and suffixes ──────────────────────────────────────

@pytest.mark.parametrize("text", [
    "South Korea won", "North Korea", "West Ham", "East Germany", "Premier Inn", "Copa Del Rey",
    "Champions Cup", "United Kingdom", "Real Sociedad", "Inter Miami",
])
def test_club_and_region_first_words_block_the_name(text):
    assert person_names(text) == []


@pytest.mark.parametrize("text", [
    "Leeds United won", "Newcastle United", "Sheffield United", "Cardiff City", "Mexico City",
    "Atletico Madrid", "La Liga title", "El Clasico tonight",
])
def test_club_suffixes_and_phrases_block_the_name(text):
    assert person_names(text) == []


# ── whitespace ───────────────────────────────────────────────────────────────

def test_exclusions_are_not_bypassed_by_odd_whitespace():
    assert person_names("World  Cup final") == []
    assert person_names("World\tCup final") == []
    assert person_names("World\nCup final") == []


def test_a_line_break_is_never_inside_a_name():
    assert person_names("Messi\nRomero") == []
    assert person_names("Lionel\u00a0Messi") == ["Lionel Messi"]


def test_extra_spaces_inside_a_name_are_normalized():
    assert person_names("Lionel   Messi scores") == ["Lionel Messi"]
