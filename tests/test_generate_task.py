"""Tests for the generate_guide task's failure handling — missing rows and retries."""
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery.exceptions import Retry

from api import models


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
