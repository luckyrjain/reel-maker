"""Person-name extraction shared by evaluator.py (scoring), visual_fallback.py (player backfill)
and asset_sourcer.py (Wikipedia lookups), so the three cannot drift apart again.

Stdlib-only on purpose: callers sit in both engine.generation and engine.render.

context_enricher.py's _NAMED_ENTITY_RE is deliberately NOT built on this: it counts every named
entity (teams, tournaments, places) as a specificity signal, which is the opposite of excluding them.
"""
import re

# Words of 2+ letters — "Di Maria", "De Paul", "Lo Celso" must match as one name. The older
# evaluator/visual_fallback regex required 3+ letters per word and silently dropped them.
# Horizontal whitespace only: a line break is never inside a name.
_NAME_RE = re.compile(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:[^\S\r\n]+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+\b")

_NON_PERSON_PHRASES = {
    "world cup", "copa america", "copa libertadores", "premier league", "premier division",
    "champions league", "ask france", "ask colombia", "ask anyone", "can argentina",
    "south america", "north america", "united states", "real madrid", "manchester city",
    "manchester united", "inter milan", "south american", "north american", "central american",
    "south american side", "west european", "la liga", "el clasico", "atletico madrid",
}

# Sentence openers / connectives the regex picks up when a clause starts with a capital. They are
# STRIPPED from the front of a match, not used to drop it: "Is Vinicius Junior the best?" is the
# name "Vinicius Junior" (hooks are interrogative), while "Because Messi" leaves one word and goes.
# 2-letter function words matter now that words may be 2 letters. Deliberately absent: "Di", "De",
# "Lo", "Al", "El" (name particles), "Li"/"Xi"/"Do" (real surnames) and "Will".
_OPENER_WORDS = {
    "ask", "can", "the", "this", "that", "and", "but", "for", "because", "although", "however",
    "without", "despite", "unlike", "within", "against", "between", "during",
    "is", "in", "so", "if", "as", "on", "at", "by", "to", "of", "he", "we", "it", "no", "up", "be",
    "or", "my", "us", "are", "was", "does", "did", "who", "what", "how", "why", "when", "watch",
}

# Club/league/region prefixes. First word only: "Kanye West" is a person, "West European" is not.
_NON_PERSON_FIRST_WORDS = {
    "south", "north", "west", "east", "premier", "copa", "champions", "united", "real", "inter",
}

# Club suffixes: "Leeds United", "Cardiff City", "Mexico City" — open-ended, so a word rather
# than a phrase list. No person is surnamed either.
_NON_PERSON_LAST_WORDS = {"united", "city"}


def person_names(text: str) -> list[str]:
    """Every plausible person name in `text`, in order, whitespace-normalized."""
    result = []
    for match in _NAME_RE.findall(text):
        words = match.split()
        while words and words[0].lower() in _OPENER_WORDS:
            words.pop(0)
        if len(words) < 2:
            continue
        name = " ".join(words)
        if (
            name.lower() in _NON_PERSON_PHRASES
            or words[0].lower() in _NON_PERSON_FIRST_WORDS
            or words[-1].lower() in _NON_PERSON_LAST_WORDS
        ):
            continue
        result.append(name)
    return result


def first_person_name(text: str) -> str | None:
    names = person_names(text)
    return names[0] if names else None
