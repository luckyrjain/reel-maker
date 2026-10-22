"""Tests for api/display.py::safe_text -- display-time cleaning of free text from the DB."""
import pytest

from api.display import safe_text

BIDI_OVERRIDES = ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
                  "\u2066", "\u2067", "\u2068", "\u2069"]


def test_none_is_empty_and_non_strings_are_stringified():
    assert safe_text(None) == ""
    assert safe_text(42) == "42"
    assert safe_text("") == ""


def test_plain_text_is_returned_unchanged():
    assert safe_text("Premier League football") == "Premier League football"


# 0x09-0x0D (tab, LF, VT, FF, CR) and 0x1C-0x1F (information separators) are whitespace: see below
@pytest.mark.parametrize("ch", [chr(c) for c in list(range(0x00, 0x09)) + list(range(0x0E, 0x1C))])
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


@pytest.mark.parametrize("ws", ["\t", "\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x1f",
                                "\x85", "\u2028", "\u2029"])
def test_whitespace_controls_become_one_space(ws):
    assert safe_text(f"line one{ws}line two") == "line one line two"


def test_a_run_of_whitespace_controls_collapses_to_one_space():
    assert safe_text("a\n\n\r\n\tb") == "a b"


@pytest.mark.parametrize("ch", BIDI_OVERRIDES)
def test_bidi_overrides_embeddings_and_isolates_are_removed(ch):
    assert safe_text(f"safe{ch}text") == "safetext"


def test_a_bidi_override_cannot_reorder_the_displayed_text():
    spoof = "user\u202egnp.exe"
    assert "\u202e" not in safe_text(spoof)


@pytest.mark.parametrize("mark", ["\u200e", "\u200f", "\u061c"])
def test_directional_marks_are_kept(mark):
    """LRM / RLM / ALM are used by ordinary RTL text; stripping them would change how it reads."""
    assert safe_text(f"a{mark}b") == f"a{mark}b"


@pytest.mark.parametrize("text", [
    "Lionel Messi", "Martínez", "Ñandú", "مرحبا بالعالم", "שלום עולם", "日本語のテキスト",
    "🔥 goal! ⚽", "e\u0301 (combining acute)", "a\u200db (zero-width joiner)",
])
def test_ordinary_unicode_is_kept(text):
    assert safe_text(text) == text


def test_html_is_left_to_jinja_autoescape_not_altered_here():
    assert safe_text("<script>alert(1)</script>") == "<script>alert(1)</script>"


def test_is_idempotent():
    dirty = "a\x00b\u202ec\nd\x85e\u2067f"
    assert safe_text(safe_text(dirty)) == safe_text(dirty)


def test_a_large_value_is_cleaned_in_linear_time():
    big = ("x\x00\u202e " * 200_000)
    out = safe_text(big)
    assert "\x00" not in out and "\u202e" not in out and len(out) == 399_999   # "x x x ... x": trailing space trimmed


# ── round-3 additions ────────────────────────────────────────────────────────

@pytest.mark.parametrize("lone", ["\ud800", "\udbff", "\udc00", "\udfff"])
def test_lone_surrogates_are_removed(lone):
    """They cannot be encoded as UTF-8: one left in a hook line would make the response fail."""
    out = safe_text(f"hello {lone} world")
    assert out == "hello  world"
    out.encode("utf-8")      # must not raise


def test_a_real_astral_character_is_not_mistaken_for_a_surrogate():
    assert safe_text("goal \U0001F600 \U0001F3F4") == "goal \U0001F600 \U0001F3F4"


@pytest.mark.parametrize("blank", [
    " ", "   ", "\n", "\t", "\r\n", "\x00", "\x00 \n \x1f", "\u202e", " \u202e\n ",
])
def test_a_value_with_nothing_visible_comes_out_empty(blank):
    """So the page's `or "—"` fallback shows instead of a blank cell."""
    assert safe_text(blank) == ""


def test_outer_whitespace_is_trimmed_inner_is_kept():
    assert safe_text("  two  words \n") == "two  words"


def test_non_breaking_space_inside_text_is_kept():
    assert safe_text("a\u00a0b") == "a\u00a0b"


# ── round-3 review additions ─────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "a\u00a0b",                      # no-break space
    "co\u00adop",                    # soft hyphen
    "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",   # Persian: ZWNJ is part of the spelling
    "a\u200bb",                      # zero-width space
    "a\u2060b",                      # word joiner
    "\ufeffx",                       # BOM / zero-width no-break space
])
def test_no_break_space_soft_hyphen_and_zero_width_format_characters_are_kept(text):
    """Printable or layout-only format characters that real text relies on are not stripped
    (only controls, surrogates and the bidi overrides are)."""
    assert safe_text(text) == text


def test_every_whitespace_run_is_collapsed_not_just_the_first():
    assert safe_text("a\nb\nc\td") == "a b c d"


@pytest.mark.parametrize("falsy, shown", [(0, "0"), (False, "False"), (0.0, "0.0")])
def test_falsy_non_none_values_are_shown_not_dropped(falsy, shown):
    assert safe_text(falsy) == shown

