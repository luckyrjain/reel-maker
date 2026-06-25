"""
Pre-generation context quality evaluation and LLM enrichment.

evaluate_context() — rule-based scorer (5 axes × 20 pts = 100 max).
llm_enrich()       — LLM call to improve thin context; returns enriched string or None.

Threshold: score < 60 triggers enrichment.
"""
import logging
import re

_log = logging.getLogger(__name__)

ENRICH_THRESHOLD = 60

# ── Axis helpers ─────────────────────────────────────────────────────────────

_NAMED_ENTITY_RE = re.compile(
    r'\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{1,}(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{1,})+\b'
)
_NUMBER_RE = re.compile(r'\b\d+(?:[.,]\d+)?%?\b')

_TENSION_WORDS = {
    "but", "however", "despite", "risk", "challenge", "collapse",
    "concern", "pressure", "threat", "weakness", "problem",
    "controversy", "doubt", "debate", "question", "exposed",
    "fragile", "vulnerable", "worry", "danger",
}

_DISCOURSE_CONNECTORS = {
    "first", "then", "however", "finally", "but", "because",
    "which means", "therefore", "meanwhile", "although", "despite",
    "nevertheless", "yet", "whereas",
}

_HOOK_ADDRESS_RE = re.compile(
    r'^(imagine|you |here\'s|this is|what if|was this|is this|did you|'
    r'why does|how does|the real|meet )',
    re.IGNORECASE,
)


def _score_length(words: list[str]) -> tuple[int, list[str]]:
    n = len(words)
    if n < 50:
        return 0, ["context_too_short"]
    if n < 100:
        return 10, []
    return 20, []


def _score_specificity(text: str) -> tuple[int, list[str]]:
    names = _NAMED_ENTITY_RE.findall(text)
    numbers = _NUMBER_RE.findall(text)
    count = len(set(names)) + len(set(numbers))
    if count == 0:
        return 0, ["lacks_specific_details"]
    if count <= 2:
        return 10, []
    return 20, []


def _score_stakes(text: str) -> tuple[int, list[str]]:
    lower = text.lower()
    found = sum(1 for w in _TENSION_WORDS if w in lower)
    if found == 0:
        return 0, ["no_conflict_or_tension"]
    if found == 1:
        return 10, []
    return 20, []


def _score_narrative(text: str) -> tuple[int, list[str]]:
    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
    lower = text.lower()
    connector_count = sum(1 for c in _DISCOURSE_CONNECTORS if c in lower)
    if connector_count >= 2 and len(sentences) >= 4:
        return 20, []
    if connector_count >= 1 or len(sentences) >= 3:
        return 10, []
    return 0, ["weak_narrative_structure"]


_WEAK_OPENER_RE = re.compile(
    r'^(the\s+\w+\s+(had|have|has|was|were|is|are|did|does|do|got|get|played|made)\b)',
    re.IGNORECASE,
)


def _score_hook(text: str) -> tuple[int, list[str]]:
    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
    if not sentences:
        return 0, ["weak_hook_potential"]
    first = sentences[0]
    if "?" in first:
        return 20, []
    if _HOOK_ADDRESS_RE.match(first.strip()):
        return 20, []
    if _NUMBER_RE.search(first) and re.search(
        r'\b(most|best|greatest|worst|ever|never|only|first|last)\b', first, re.IGNORECASE
    ):
        return 20, []
    # Partial credit only for short openers that aren't generic subject-verb constructions
    if len(first.split()) <= 8 and not _WEAK_OPENER_RE.match(first.strip()):
        return 10, []
    return 0, ["weak_hook_potential"]


def evaluate_context(context: str) -> tuple[int, list[str]]:
    """Score the input context for video engagement potential (0–100).

    Returns (score, issues) where issues is a list of axis-failure labels.
    score < ENRICH_THRESHOLD (60) means the context should be enriched.
    """
    if not context or not context.strip():
        return 0, [
            "context_too_short", "lacks_specific_details",
            "no_conflict_or_tension", "weak_narrative_structure", "weak_hook_potential",
        ]

    words = context.split()
    s1, i1 = _score_length(words)
    s2, i2 = _score_specificity(context)
    s3, i3 = _score_stakes(context)
    s4, i4 = _score_narrative(context)
    s5, i5 = _score_hook(context)

    total = s1 + s2 + s3 + s4 + s5
    issues = i1 + i2 + i3 + i4 + i5
    return total, issues


def llm_enrich(context: str, niche: str, llm) -> str | None:
    """Call the LLM to enrich a thin context for video engagement.

    Returns the enriched context string, or None on any failure.
    The caller is responsible for wrapping this in record_stage().
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are a video content strategist. "
                "You improve topic descriptions to maximise short-form video engagement. "
                "Return the improved context only — no explanation, no markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                "Improve the following context for a short-form video script.\n\n"
                "Requirements:\n"
                "- Add specific details (names, numbers, dates, events) if mentioned but vague\n"
                "- Add at least one clear conflict, risk, or open question that creates tension\n"
                "- Make the first sentence a strong hook: a direct question, bold claim, "
                "or direct address to the viewer\n"
                "- Preserve all original facts — do not invent statistics or events\n"
                "- Max 1200 words\n\n"
                f"NICHE: {niche or 'general'}\n\n"
                f"CONTEXT:\n{context}"
            ),
        },
    ]
    try:
        result = llm.complete(messages)
        if result and result.strip():
            return result.strip()
        return None
    except Exception as exc:
        _log.warning("llm_enrich failed: %s", exc)
        return None
