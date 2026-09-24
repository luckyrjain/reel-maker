"""Tests for context_enricher — evaluate_context axis scoring."""
from engine.generation.context_enricher import evaluate_context, llm_enrich

LONG_NEUTRAL = " ".join(["the team played and worked together"] * 8)


def test_score_bounded_zero_to_100():
    score, _ = evaluate_context("")
    assert 0 <= score <= 100


def test_empty_context_scores_zero():
    score, issues = evaluate_context("")
    assert score == 0
    assert "context_too_short" in issues


def test_short_context_scores_low_on_length():
    score, issues = evaluate_context("Argentina won.")
    assert "context_too_short" in issues


def test_long_context_clears_length_axis():
    text = " ".join(["word"] * 110)
    _, issues = evaluate_context(text)
    assert "context_too_short" not in issues


def test_named_entities_clear_specificity_axis():
    text = (
        "Lionel Messi scored 7 goals in 2022. "
        "Emiliano Martinez saved three penalties against France. "
        "The match ended 3-3 before penalties."
    )
    _, issues = evaluate_context(text)
    assert "lacks_specific_details" not in issues


def test_no_entities_fails_specificity_axis():
    _, issues = evaluate_context(LONG_NEUTRAL)
    assert "lacks_specific_details" in issues


def test_tension_words_clear_stakes_axis():
    text = LONG_NEUTRAL + " however there is a real risk of collapse despite the pressure"
    _, issues = evaluate_context(text)
    assert "no_conflict_or_tension" not in issues


def test_no_tension_fails_stakes_axis():
    _, issues = evaluate_context(LONG_NEUTRAL)
    assert "no_conflict_or_tension" in issues


def test_question_hook_clears_hook_axis():
    text = "Was this the greatest final ever? " + LONG_NEUTRAL
    _, issues = evaluate_context(text)
    assert "weak_hook_potential" not in issues


def test_direct_address_hook_clears_hook_axis():
    text = "Imagine watching the best match in history. " + LONG_NEUTRAL
    _, issues = evaluate_context(text)
    assert "weak_hook_potential" not in issues


def test_weak_opener_fails_hook_axis():
    text = "The team had a performance during the tournament this year. " + LONG_NEUTRAL
    _, issues = evaluate_context(text)
    assert "weak_hook_potential" in issues


def test_rich_context_scores_above_threshold():
    context = (
        "Was this the greatest World Cup final ever? "
        "Argentina's Lionel Messi scored 7 goals in 2022 to lead his nation. "
        "But France pushed back — Kylian Mbappé scored a hat-trick. "
        "Emiliano Martinez saved two penalties however the pressure never eased. "
        "Despite the lead, Argentina nearly collapsed before winning 4-2. "
        "The question is whether this squad can repeat the feat in 2026."
    )
    score, issues = evaluate_context(context)
    assert score >= 60, f"Expected >= 60, got {score}. Issues: {issues}"


def test_discourse_connectors_help_narrative_axis():
    text = (
        "First the team set up their formation. "
        "Then they pressed high. "
        "However the opposition countered. "
        "Finally they found a breakthrough. " * 2
    )
    _, issues = evaluate_context(text)
    assert "weak_narrative_structure" not in issues


# ── llm_enrich json_mode contract ─────────────────────────────────────────────


def test_llm_enrich_requests_free_text_not_json():
    """llm_enrich wants prose back — a model that strictly honors the default
    json_mode=True would wrap the response as {"text": "..."} instead of
    returning it, corrupting reel.enriched_context with literal JSON syntax."""
    captured = {}

    class RecordingLLM:
        def complete(self, messages, json_mode=True):
            captured["json_mode"] = json_mode
            return "Enriched prose context."

    result = llm_enrich("Some context.", "football", RecordingLLM())
    assert captured["json_mode"] is False
    assert result == "Enriched prose context."
