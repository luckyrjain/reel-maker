"""Tests for api/display.py::safe_text -- display-time cleaning of free text from the DB."""
import pytest

from api.display import safe_text

BIDI_OVERRIDES = ["‪", "‫", "‬", "‭", "‮",
                  "⁦", "⁧", "⁨", "⁩"]


def test_none_is_empty_and_non_strings_are_stringified():
    assert safe_text(None) == ""
    assert safe_text(42) == "42"
    assert safe_text("") == ""


def test_plain_text_is_returned_unchanged():
    assert safe_text("Premier League football") == "Premier League football"


@pytest.mark.parametrize("ch", [chr(c) for c in list(range(0x00, 0x09)) + list(range(0x0E, 0x20))])
def test_c0_controls_are_removed(ch):
    assert safe_text(f"a{ch}b") == "ab"


def test_nul_is_removed():
    assert safe_text("niche\x00name") == "nichename"


def test_del_is_removed():
    assert safe_text("a\x7fb") == "ab"


# 0x85 (NEL) is whitespace, not removed: it becomes a space (see the whitespace test below)
@pytest.mark.parametrize("cp", [c for c in range(0x80, 0xA0) if c != 0x85])
def test_c1_controls_are_removed(cp):
    assert safe_text(f"a{chr(cp)}b") == "ab"


@pytest.mark.parametrize("ws", ["\t", "\n", "\r", "\r\n", "\v", "\f", "\x85", " ", " "])
def test_whitespace_controls_become_one_space(ws):
    assert safe_text(f"line one{ws}line two") == "line one line two"


def test_a_run_of_whitespace_controls_collapses_to_one_space():
    assert safe_text("a\n\n\r\n\tb") == "a b"


@pytest.mark.parametrize("ch", BIDI_OVERRIDES)
def test_bidi_overrides_embeddings_and_isolates_are_removed(ch):
    assert safe_text(f"safe{ch}text") == "safetext"


def test_a_bidi_override_cannot_reorder_the_displayed_text():
    spoof = "user‮gnp.exe"
    assert "‮" not in safe_text(spoof)


@pytest.mark.parametrize("mark", ["‎", "‏", "؜"])
def test_directional_marks_are_kept(mark):
    """LRM / RLM / ALM are used by ordinary RTL text; stripping them would change how it reads."""
    assert safe_text(f"a{mark}b") == f"a{mark}b"


@pytest.mark.parametrize("text", [
    "Lionel Messi", "Martínez", "Ñandú", "مرحبا بالعالم", "שלום עולם", "日本語のテキスト",
    "🔥 goal! ⚽", "é (combining acute)", "a‍b (zero-width joiner)",
])
def test_ordinary_unicode_is_kept(text):
    assert safe_text(text) == text


def test_html_is_left_to_jinja_autoescape_not_altered_here():
    assert safe_text("<script>alert(1)</script>") == "<script>alert(1)</script>"


def test_is_idempotent():
    dirty = "a\x00b‮c\nd\x85e⁧f"
    assert safe_text(safe_text(dirty)) == safe_text(dirty)


def test_a_large_value_is_cleaned_in_linear_time():
    big = ("x\x00‮ " * 200_000)
    out = safe_text(big)
    assert "\x00" not in out and "‮" not in out and len(out) == 400_000
