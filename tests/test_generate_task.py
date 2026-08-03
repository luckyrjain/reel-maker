"""Tests for the generate_guide task's failure handling — missing rows and retries."""
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery.exceptions import Retry

from api import models
from engine.generation.script_parser import BeatStub
from worker.tasks.generate import _stubs_to_platform_guide


def _job(job_id=1, reel_id=10):
    job = MagicMock()
    job.id = job_id
    job.reel_id = reel_id
    job.cut_id = None
    job.status = models.JobStatus.pending
    job.attempts = 0
    job.progress = 0
    job.meta = {}
    job.error = None
    return job


def _reel():
    reel = MagicMock()
    reel.id = 10
    reel.context = "Argentina squad review."
    reel.enriched_context = None
    reel.niche = "football"
    reel.voiceover_mode = "voiceover"
    reel.status.value = "generating"
    return reel


def _cut():
    cut = MagicMock()
    cut.platform.value = "youtube_shorts"
    cut.target_length_s = 45.0
    return cut


def test_missing_reel_fails_job_with_actionable_message():
    """A deleted reel must produce an operator-readable error, not an AttributeError."""
    from worker.tasks.generate import generate_guide

    job = _job(reel_id=99)
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else None

    with patch("worker.tasks.generate.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="Reel 99 no longer exists"):
            generate_guide(1)

    assert job.status == models.JobStatus.failed
    assert "Reel 99 no longer exists" in job.error
    assert "AttributeError" not in job.error


def test_transient_failure_retries_and_resets_status_to_pending():
    """Retry must reset status to pending, or redelivery hits the idempotency guard."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.return_value.filter.return_value.all.return_value = [_cut()]

    with (
        patch("worker.tasks.generate.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider",
              side_effect=httpx.ConnectTimeout("LLM unreachable")),
        patch.object(generate_guide, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(Retry):
            generate_guide(1)

    mock_retry.assert_called_once()
    assert job.status == models.JobStatus.pending
    assert job.attempts == 1, "entry already incremented attempts; the retry branch must not"


def test_paid_call_budget_exceeded_fails_without_retry():
    """A reel that already hit its paid-call cap must fail cleanly, not retry."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel

    with (
        patch("worker.tasks.generate.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=20),
        patch.object(generate_guide, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(ValueError, match="Paid LLM call budget exceeded"):
            generate_guide(1)

    mock_retry.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_deterministic_failure_does_not_retry():
    """A bad-guide ValueError must fail once, exactly as before."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.return_value.filter.return_value.all.return_value = [_cut()]

    with (
        patch("worker.tasks.generate.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider",
              side_effect=ValueError("model not found")),
        patch.object(generate_guide, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(ValueError, match="model not found"):
            generate_guide(1)

    mock_retry.assert_not_called()
    assert job.status == models.JobStatus.failed


# ── _stubs_to_platform_guide — music_cue defaulting ──────────────────────────

def _stub(index, beat_type, vo="Some voiceover line here."):
    return BeatStub(
        index=index, beat_type=beat_type, section="", player="",
        vo_script=vo, duration_s=5.0, on_screen_text=["Text"],
    )


def test_structured_path_hook_beat_gets_a_default_music_cue():
    """Structured-script beats never carry an LLM-written music_cue — without a
    default, render_cut would never find a cue to look up a music track for and
    the structured path would silently never get background music.
    """
    stubs = [_stub(0, "hook"), _stub(1, "body"), _stub(2, "cta")]
    guide = _stubs_to_platform_guide(
        stubs, visuals={}, platform="youtube_shorts", target_length_s=30.0,
        caption="Caption", hashtags=["tag"] * 6,
    )
    assert guide.beats[0].type == "hook"
    assert guide.beats[0].music_cue == "upbeat energetic"


def test_structured_path_non_hook_beats_have_no_music_cue():
    """Only one cue is needed — render_cut picks the first non-empty cue across
    all beats for the whole cut's music track, so body/cta beats stay None.
    """
    stubs = [_stub(0, "hook"), _stub(1, "body"), _stub(2, "cta")]
    guide = _stubs_to_platform_guide(
        stubs, visuals={}, platform="youtube_shorts", target_length_s=30.0,
        caption="Caption", hashtags=["tag"] * 6,
    )
    assert guide.beats[1].music_cue is None
    assert guide.beats[2].music_cue is None
