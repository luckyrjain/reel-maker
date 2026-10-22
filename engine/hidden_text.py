"""Removal of Unicode TAG characters (U+E0000-E007F) from text that is shown to a person AND fed to
an LLM.

The tag block mirrors ASCII (U+E0041 is an invisible "A", and so on), is rendered as nothing by
browsers and most fonts, and an LLM reads it. So a note that looks like "Hooks phrased as questions
win." can carry a whole hidden instruction ("ASCII smuggling"). Performance notes are exactly such
text: an operator types or pastes them, and every active one is put into the standard-path
generation prompt as feedback.

One legitimate use exists: three emoji flags are spelled as BLACK FLAG + tag characters + CANCEL TAG
-- England, Scotland and Wales. Those exact sequences are kept (an allowlist, not a shape check: a
"BLACK FLAG + [a-z0-9]+ + CANCEL TAG" test would let a readable lower-case message through inside a
flag). Every other tag character is removed, so no covert capacity is left: only three fixed
strings are possible.

Written with chr(), never as literal characters: they are invisible.
"""
import re

_TAG_BASE = 0xE0000
_BLACK_FLAG = chr(0x1F3F4)
_CANCEL_TAG = chr(0xE007F)


def _flag(subdivision: str) -> str:
    return _BLACK_FLAG + "".join(chr(_TAG_BASE + ord(c)) for c in subdivision) + _CANCEL_TAG


# RGI subdivision flags: England, Scotland, Wales.
ALLOWED_FLAGS = (_flag("gbeng"), _flag("gbsct"), _flag("gbwls"))

# One of the three allowed flags (captured as `flag`) or any single tag character. A flag starts with
# BLACK FLAG, which is not itself a tag character, so the two alternatives can never both match at a
# position and their order does not matter.
_TAG_RUN = re.compile(
    "(?P<flag>" + "|".join(re.escape(f) for f in ALLOWED_FLAGS) + ")"
    "|[" + chr(_TAG_BASE) + "-" + chr(_TAG_BASE + 0x7F) + "]"
)


def strip_tag_chars(text: str) -> str:
    """`text` without any tag character, except inside the three allowed flag sequences."""
    return _TAG_RUN.sub(lambda m: m.group("flag") or "", text)
