"""Person-name extraction shared by evaluator.py (scoring), visual_fallback.py (player backfill)
and asset_sourcer.py (Wikipedia lookups), so the three cannot drift apart again.

Stdlib-only on purpose: callers sit in both engine.generation and engine.render.

context_enricher.py's _NAMED_ENTITY_RE is deliberately NOT built on this: it counts every named
entity (teams, tournaments, places) as a specificity signal, which is the opposite of excluding them.
"""
import re

# Words of 2+ letters — "Di Maria", "De Paul", "Lo Celso" must match as one name. The older
# evaluator/visual_fallback regex required 3+ letters per word and silently dropped them.
_NAME_RE = re.compile(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+\b")

_NON_PERSON_PHRASES = {
    "world cup", "copa america", "copa libertadores", "premier league", "premier division",
    "champions league", "ask france", "ask colombia", "ask anyone", "can argentina",
    "south america", "north america", "united states", "real madrid", "manchester city",
    "manchester united", "inter milan", "south american", "north american", "central american",
    "south american side", "west european",
}

# Sentence openers / connectives the regex picks up when a clause starts with a capital.
_NON_NAME_FIRST_WORDS = {
    "ask", "can", "the", "this", "that", "and", "but", "for", "because", "although", "however",
    "without", "despite", "unlike", "within", "against", "between", "during",
}

# Club/league/region prefixes. First word only: "Kanye West" is a person, "West European" is not.
_NON_PERSON_FIRST_WORDS = {
    "south", "north", "west", "east", "premier", "copa", "champions", "united", "real", "inter",
}


def person_names(text: str) -> list[str]:
    """Every plausible person name in `text`, in order."""
    result = []
    for name in _NAME_RE.findall(text):
        if name.lower() in _NON_PERSON_PHRASES:
            continue
        first = name.split()[0].lower()
        if first in _NON_NAME_FIRST_WORDS or first in _NON_PERSON_FIRST_WORDS:
            continue
        result.append(name)
    return result


def first_person_name(text: str) -> str | None:
    names = person_names(text)
    return names[0] if names else None
