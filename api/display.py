"""Display-time cleaning of free text that comes from the database (LLM output, operator
input) before it is put on a page.

Jinja's autoescape already neutralises HTML. It does not touch control or bidirectional-override
characters, which survive into the page: a NUL or other C0/C1 control can break copy/paste, logs
and some browsers' rendering, and an override/embedding/isolate (U+202A-202E, U+2066-2069) can
reorder the text around it so that it reads differently from what is stored ("Trojan Source").
The stored value is never changed; only what is displayed.

Kept on purpose: the directional MARKS U+200E, U+200F and U+061C (ALM) -- ordinary RTL text uses
them and stripping them would change how real Arabic/Hebrew reads; and every printable character,
emoji and combining mark. Whitespace controls (tab, newline, the \\x1c-\\x1f information
separators, NEL, U+2028/2029) become a single space so a multi-line value stays readable on one
table row, and leading/trailing whitespace is trimmed so a value that was only whitespace comes
out empty (the page's "no value" fallback then shows).

Lone UTF-16 surrogates (U+D800-DFFF) are removed too: they cannot be encoded as UTF-8, so one in a
hook line (JSON text can carry the escape) would make the whole response fail to encode.
"""
import re

# Written as escapes, not literal characters: these are invisible or direction-changing, and a
# source file that contains them verbatim cannot be reviewed by eye.
_BIDI_OVERRIDES = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_WHITESPACE_CONTROLS = re.compile("[\t\n\v\f\r\x1c-\x1f\x85\u2028\u2029]+")
# C0 controls (minus the whitespace ones above, handled first), DEL, C1 controls, lone surrogates,
# and the bidi overrides/embeddings/isolates.
_STRIP = re.compile("[\x00-\x08\x0e-\x1b\x7f\x80-\x9f\ud800-\udfff" + _BIDI_OVERRIDES + "]")


def safe_text(value) -> str:
    """`value` as text that is safe to show: None -> "", controls, surrogates and bidi overrides
    removed, whitespace controls collapsed to one space, outer whitespace trimmed."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return _STRIP.sub("", _WHITESPACE_CONTROLS.sub(" ", text)).strip()
