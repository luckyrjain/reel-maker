"""Tests for engine/generation/estimate.py — pre-generation cost/time estimate."""
from unittest.mock import patch

from api import models
from engine.generation.estimate import estimate_generation, resolve_generation_path

_STRUCTURED_CTX = "GOALKEEPER\nMartinez saves.\nDEFENSE\nRomero leads.\nMIDFIELD\nDe Paul runs."
_FREE_TEXT_CTX = "Argentina are a strong squad heading into the tournament with real depth."


def test_resolve_generation_path_forced_structured():
    assert resolve_generation_path(_FREE_TEXT_CTX, "structured") == "structured"


def test_resolve_generation_path_forced_standard():
    assert resolve_generation_path(_STRUCTURED_CTX, "standard") == "standard"


def test_resolve_generation_path_auto_detects_structured():
    assert resolve_generation_path(_STRUCTURED_CTX, "auto") == "structured"


def test_resolve_generation_path_auto_detects_standard():
    assert resolve_generation_path(_FREE_TEXT_CTX, "auto") == "standard"


def test_estimate_with_no_history_reports_no_data(db_session):
    with patch("engine.generation.estimate.settings.nvidia_price_per_1m_input_tokens", 0.0), \
         patch("engine.generation.estimate.settings.nvidia_price_per_1m_output_tokens", 0.0):
        est = estimate_generation(db_session, _FREE_TEXT_CTX, "auto")
    assert est.path == "standard"
    assert est.avg_cost_usd is None
    assert est.sample_size == 0
    assert est.pricing_configured is False


def test_estimate_averages_cost_across_matching_past_reels(db_session):
    db = db_session
    for cost in (0.01, 0.03):
        reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        db.add(models.Job(
            type=models.JobType.generate, reel_id=reel.id,
            status=models.JobStatus.done, meta={"path": "standard"},
        ))
        db.add(models.StageEvent(reel_id=reel.id, stage="generate", cost_usd=cost, ok=True))
    db.commit()

    est = estimate_generation(db, _FREE_TEXT_CTX, "auto")
    assert est.sample_size == 2
    assert round(est.avg_cost_usd, 3) == 0.02


def test_estimate_ignores_reels_on_a_different_path(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    db.add(models.Job(
        type=models.JobType.generate, reel_id=reel.id,
        status=models.JobStatus.done, meta={"path": "structured"},
    ))
    db.add(models.StageEvent(reel_id=reel.id, stage="enrich", cost_usd=0.05, ok=True))
    db.commit()

    est = estimate_generation(db, _FREE_TEXT_CTX, "auto")  # resolves to "standard"
    assert est.avg_cost_usd is None
    assert est.sample_size == 0


def test_estimate_excludes_structured_fallback_reels_from_standard_bucket(db_session):
    """A reel that tried structured first and fell through to standard pays for
    both attempts. Its job.meta["path"] ends up "standard", but counting its full
    cost as a plain "standard" sample would inflate the estimate for reels that
    went straight to standard — see engine/generation/estimate.py's docstring.
    """
    db = db_session

    # A genuine straight-to-standard reel.
    clean_reel = models.Reel(context="clean", status=models.ReelStatus.guide_ready)
    db.add(clean_reel)
    db.flush()
    db.add(models.Job(
        type=models.JobType.generate, reel_id=clean_reel.id,
        status=models.JobStatus.done, meta={"path": "standard"},
    ))
    db.add(models.StageEvent(reel_id=clean_reel.id, stage="generate", cost_usd=0.02, ok=True))

    # A reel that tried structured, failed the quality gate, and fell through —
    # its StageEvents include the wasted structured-attempt cost too.
    fallback_reel = models.Reel(context="fallback", status=models.ReelStatus.guide_ready)
    db.add(fallback_reel)
    db.flush()
    db.add(models.Job(
        type=models.JobType.generate, reel_id=fallback_reel.id,
        status=models.JobStatus.done,
        meta={"path": "standard", "structured_score": 40, "structured_fallback": True},
    ))
    db.add(models.StageEvent(reel_id=fallback_reel.id, stage="enrich", cost_usd=0.05, ok=True))
    db.add(models.StageEvent(reel_id=fallback_reel.id, stage="generate", cost_usd=0.02, ok=True))
    db.commit()

    est = estimate_generation(db, _FREE_TEXT_CTX, "auto")  # resolves to "standard"
    assert est.sample_size == 1
    assert round(est.avg_cost_usd, 3) == 0.02
