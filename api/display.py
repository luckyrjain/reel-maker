"""Display-time cleaning of free text that comes from the database (LLM output, operator
input) before it is put on a page.

Jinja's autoescape already neutralises HTML. It does not touch control or bidirectional-override
characters, which survive into the page: a NUL or other C0/C1 control can break copy/paste, logs
and some browsers' rendering, and an override/embedding/isolate (U+202A-202E, U+2066-2069) can
reorder the text around it so that it reads differently from what is stored ("Trojan Source").
The stored value is never changed; only what is displayed.

Kept on purpose: the directional MARKS U+200E, U+200F and U+061C (ALM) -- ordinary RTL text uses
them and stripping them would change how real Arabic/Hebrew reads; and every printable character,
emoji and combining mark. Whitespace controls (tab, newline, ...) become a single space so a
multi-line value stays readable on one table row.
"""
import re

_BIDI_OVERRIDES = "‪‫‬‭‮⁦⁧⁨⁩"
_WHITESPACE_CONTROLS = re.compile(r"[\t\n\v\f\r\x85  ]+")
# C0 controls (minus the whitespace ones above, handled first), DEL, C1 controls, and the bidi
# overrides/embeddings/isolates.
_STRIP = re.compile("[\x00-\x08\x0e-\x1f\x7f\x80-\x9f" + _BIDI_OVERRIDES + "]")


def safe_text(value) -> str:
    """`value` as text that is safe to show: None -> "", controls and bidi overrides removed."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return _STRIP.sub("", _WHITESPACE_CONTROLS.sub(" ", text))
