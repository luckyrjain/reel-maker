"""Niche helpers shared by evaluator.py (scoring vocabulary) and beat_enrichment.py
(enrichment vocabulary/prompt) so the two cannot disagree about what counts as football."""

NICHE_MAX_LEN = 64
_FOOTBALL_KEYWORDS = ("football", "soccer", "futbol")


def clean_niche(niche: str | None) -> str:
    """Operator/LLM-supplied free text bound for a prompt: drop control characters (newlines
    included), collapse whitespace, cap length."""
    printable = "".join(ch for ch in (niche or "") if ch.isprintable() or ch.isspace())
    return " ".join(printable.split())[:NICHE_MAX_LEN]


def is_football_niche(niche: str | None) -> bool:
    """Substring (not exact) match, so "Premier League football" counts. A blank/None niche is
    NOT football here — callers with a football-by-default rule (beat_enrichment's unset-niche
    handling) layer it on top."""
    cleaned = clean_niche(niche).lower()
    return any(k in cleaned for k in _FOOTBALL_KEYWORDS)
