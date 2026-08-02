"""Tests for judge_guide's failure fallback — judging must never block generation."""
import httpx

from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.llm_judge import judge_guide


def _guide() -> MasterGuide:
    beats = [
        Beat(index=0, type="hook", duration_s=3.0, visual_direction="Argentina squad",
             on_screen_text=["One weak spot"], vo_script="One position could cost them everything."),
        Beat(index=1, type="body", duration_s=10.0, visual_direction="Cristian Romero tackle",
             on_screen_text=["Romero presses"], vo_script="Romero's press lets them defend higher."),
        Beat(index=2, type="cta", duration_s=5.0, visual_direction="Argentina celebrating",
             on_screen_text=["Your call"], vo_script="So what do you think? Drop it below."),
    ]
    return MasterGuide(
        title="Test", niche="football",
        cuts=[PlatformGuide(platform="youtube_shorts", target_length_s=30.0,
                            caption="Argentina's one weak spot.", hashtags=["football"] * 10,
                            beats=beats)],
    )


class _RaisingProvider:
    def complete(self, messages, json_mode=True):
        raise httpx.ConnectTimeout("judge unreachable")


class _GarbageProvider:
    def complete(self, messages, json_mode=True):
        return "I think this script is pretty good, honestly."


class _OutOfRangeProvider:
    def complete(self, messages, json_mode=True):
        return '{"factual_accuracy": 99, "expertise_depth": 5, "natural_speech": 5, ' \
               '"hallucination_risk": 5, "shareability": 5}'


def test_provider_exception_returns_neutral_score():
    score, issues = judge_guide("Argentina squad review.", _guide(), _RaisingProvider())
    assert score == 50
    assert issues and "judge unavailable" in issues[0].lower()


def test_unparseable_response_returns_neutral_score():
    score, issues = judge_guide("Argentina squad review.", _guide(), _GarbageProvider())
    assert score == 50
    assert issues


def test_out_of_range_dimension_returns_neutral_score():
    """Pydantic bounds (0-20) reject the payload; that must fall back, not raise."""
    score, issues = judge_guide("Argentina squad review.", _guide(), _OutOfRangeProvider())
    assert score == 50
    assert issues
