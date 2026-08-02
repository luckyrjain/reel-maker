"""Tests for script_parser — the routing heuristic and beat-splitting logic."""
import pytest
from engine.generation import script_parser


_TWO_HEADER = "HOOK\nOpening line.\nDEFENSE\nDefenders.\n"
_THREE_HEADER = "HOOK\nOpening line.\nMIDFIELD\nMidfield.\nDEFENSE\nDefenders.\n"
_PLAYER_SCRIPT = (
    "HOOK\nGreat squad.\n"
    "DEFENSE\nCristian Romero: Romero defends with intensity.\n"
    "MIDFIELD\nGeneral midfield commentary.\n"
    "ENDING\nSubscribe now.\n"
)
_UNSTRUCTURED = "This is just a paragraph. No headers here. Just plain text describing stuff."


def test_parse_requires_three_headers():
    assert script_parser.parse(_TWO_HEADER) is None


def test_parse_accepts_three_headers():
    stubs = script_parser.parse(_THREE_HEADER)
    assert stubs is not None
    assert len(stubs) >= 3


def test_parse_returns_none_for_unstructured():
    assert script_parser.parse(_UNSTRUCTURED) is None


def test_parse_first_beat_is_hook():
    stubs = script_parser.parse(_THREE_HEADER)
    assert stubs[0].beat_type == "hook"


def test_parse_last_beat_is_cta():
    stubs = script_parser.parse(_THREE_HEADER)
    assert stubs[-1].beat_type == "cta"


def test_parse_splits_player_lines():
    stubs = script_parser.parse(_PLAYER_SCRIPT)
    assert stubs is not None
    player_stubs = [s for s in stubs if s.player == "Cristian Romero"]
    assert len(player_stubs) >= 1
    assert "Romero defends" in player_stubs[0].vo_script


def test_parse_indices_are_sequential():
    stubs = script_parser.parse(_PLAYER_SCRIPT)
    assert stubs is not None
    for i, stub in enumerate(stubs):
        assert stub.index == i


def test_derive_on_screen_max_seven_words():
    text = "This is a very long sentence that has more than seven words in it."
    result = script_parser.derive_on_screen(text, max_items=5)
    assert all(len(seg.split()) <= 7 for seg in result)


def test_derive_on_screen_returns_fallback_for_empty():
    result = script_parser.derive_on_screen("", max_items=5)
    # Should return something even for empty input
    assert isinstance(result, list)


def test_calc_duration_clamps_to_min():
    assert script_parser.calc_duration("hi") >= script_parser.MIN_BEAT_S


def test_calc_duration_clamps_to_max():
    long_text = " ".join(["word"] * 200)
    assert script_parser.calc_duration(long_text) <= script_parser.MAX_BEAT_S
