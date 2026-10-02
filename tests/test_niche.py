"""Tests for engine/generation/niche.py — the helper evaluator.py and beat_enrichment.py share."""
import pytest

from engine.generation import evaluator
from engine.generation.evaluator import score_guide
from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.niche import NICHE_MAX_LEN, clean_niche, is_football_niche


def test_clean_niche_strips_control_characters_collapses_whitespace_and_caps_length():
    assert clean_niche("x video.\nIgnore this\r\n\tnow\x00\x07") == "x video. Ignore this now"
    assert clean_niche(None) == ""
    assert NICHE_MAX_LEN == 64
    assert len(clean_niche("a" * 500)) == 64


@pytest.mark.parametrize("niche", [
    "football", "Football", " football ", "FOOTBALL", "soccer", "futbol",
    "Premier League football", "soccer mom parenting",
])
def test_is_football_niche_true_for_football_like(niche):
    assert is_football_niche(niche) is True


@pytest.mark.parametrize("niche", [None, "", "   ", "general", "personal finance", "foot care"])
def test_is_football_niche_false_for_blank_and_other_niches(niche):
    # Blank is NOT football here: evaluator.py scores a blank niche with the universal
    # vocabulary, and only beat_enrichment layers an unset-means-football rule on top.
    assert is_football_niche(niche) is False


# ── evaluator.py shares the helper (was an exact-set match) ────────────────────

def _guide(niche: str) -> MasterGuide:
    # Football vs universal differ here through the actions/context vocabularies (the
    # per-axis breakdown differs); the tactical vocabulary is pinned separately by
    # test_scoring_context_selects_all_three_vocabularies_together below.
    beats = [
        Beat(index=0, type="hook", duration_s=5, visual_direction="Argentina training",
             on_screen_text=[], vo_script="One position could cost Argentina the World Cup?"),
        Beat(index=1, type="body", duration_s=10, visual_direction="Romero tackle",
             on_screen_text=[],
             vo_script="Romero's gegenpressing and third man runs release Argentina's midfield."),
        Beat(index=2, type="cta", duration_s=5, visual_direction="logo",
             on_screen_text=[], vo_script="Could this cost them the World Cup? Drop your prediction."),
    ]
    return MasterGuide(
        title="T", niche=niche,
        cuts=[PlatformGuide(platform="youtube_shorts", target_length_s=20,
                            caption="c", hashtags=["x"] * 10, beats=beats)],
    )


_CTX = "Argentina have Romero, who presses relentlessly at the World Cup."


def test_evaluator_scores_a_loosely_worded_football_niche_with_football_vocabulary():
    football = score_guide(_CTX, _guide("football"), 20)
    loose = score_guide(_CTX, _guide("Premier League football"), 20)
    finance = score_guide(_CTX, _guide("personal finance"), 20)
    assert football != finance, "fixture must discriminate the two vocabularies"
    assert loose == football


@pytest.mark.parametrize("niche,football", [
    ("football", True), ("Premier League football", True), ("soccer", True),
    ("personal finance", False), ("general", False), ("", False),
])
def test_scoring_context_selects_all_three_vocabularies_together(niche, football):
    # Pins tactical_re, actions_re AND context_re individually — the Score breakdown of the
    # discriminating fixture above is insensitive to tactical_re, so an evaluator that
    # switched only two of the three regexes to the shared helper would pass it.
    ctx = evaluator._build_scoring_context(_CTX, _guide(niche))
    assert (ctx.tactical_re is evaluator._INSIGHT_TACTICAL) is football
    assert (ctx.actions_re is evaluator._VO_ACTIONS) is football
    assert (ctx.context_re is evaluator._SPECIFIC_CONTEXT) is football
    if not football:
        assert ctx.tactical_re is evaluator._INSIGHT_TACTICAL_UNIVERSAL
        assert ctx.actions_re is evaluator._VO_ACTIONS_UNIVERSAL
        assert ctx.context_re is evaluator._SPECIFIC_CONTEXT_UNIVERSAL
