"""Tests for enrich_context task logic — mocks DB and LLM."""
import pytest
from unittest.mock import MagicMock, patch, call


def _make_job(status="pending", meta=None):
    job = MagicMock()
    job.id = 1
    job.reel_id = 10
    from api import models
    job.status = models.JobStatus(status)
    job.meta = meta or {"generation_path": "auto"}
    job.attempts = 0
    job.progress = 0
    return job


def _make_reel(context="Some context about a topic.", enriched_context=None):
    reel = MagicMock()
    reel.id = 10
    reel.context = context
    reel.enriched_context = enriched_context
    reel.niche = "sports"
    reel.status = MagicMock()
    reel.status.value = "enriching"
    return reel


def test_idempotency_guard_done_job():
    """Task returns immediately if job is already done."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job(status="done")
    db = MagicMock()
    db.get.return_value = job
    with patch("worker.tasks.common.SessionLocal", return_value=db):
        enrich_context(1)
    db.add.assert_not_called()


def test_idempotency_guard_running_job():
    """Task returns immediately if job is already running."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job(status="running")
    db = MagicMock()
    db.get.return_value = job
    with patch("worker.tasks.common.SessionLocal", return_value=db):
        enrich_context(1)
    db.add.assert_not_called()


def test_enrichment_runs_when_score_below_threshold():
    """When evaluate_context returns score < 60, llm_enrich is called."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job()
    reel = _make_reel()
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(40, ["context_too_short"])) as mock_eval,
        patch("worker.tasks.enrich_context.llm_enrich", return_value="Enriched context.") as mock_enrich,
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide") as mock_gen,
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    mock_eval.assert_called_once_with(reel.context)
    mock_enrich.assert_called_once()
    assert reel.enriched_context == "Enriched context."


def test_enrichment_skipped_when_score_above_threshold():
    """When evaluate_context returns score >= 60, llm_enrich is NOT called."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job()
    reel = _make_reel()
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(75, [])),
        patch("worker.tasks.enrich_context.llm_enrich") as mock_enrich,
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide") as mock_gen,
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    mock_enrich.assert_not_called()
    assert reel.enriched_context is None


def test_generate_guide_always_enqueued_on_success():
    """generate_guide.delay() is called regardless of whether enrichment ran."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job()
    reel = _make_reel()
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(80, [])),
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide") as mock_gen,
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    mock_gen.delay.assert_called_once()


def test_llm_failure_is_nonfatal_and_generate_still_enqueued():
    """If llm_enrich returns None, the task continues and enqueues generate_guide."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job()
    reel = _make_reel()
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(30, ["context_too_short"])),
        patch("worker.tasks.enrich_context.llm_enrich", return_value=None),
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide") as mock_gen,
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    assert reel.enriched_context is None
    mock_gen.delay.assert_called_once()


# ── structured script guard ───────────────────────────────────────────────


def test_is_structured_script_detects_three_caps_headers():
    from worker.tasks.enrich_context import _is_structured_script
    text = "GOALKEEPER\nMartinez is world class.\nDEFENSE\nRomero leads the line.\nMIDFIELD\nDe Paul is the engine."
    assert _is_structured_script(text) is True


def test_is_structured_script_rejects_free_text():
    from worker.tasks.enrich_context import _is_structured_script
    text = "Argentina are the best team in the world. Messi is the greatest. The squad looks strong."
    assert _is_structured_script(text) is False


def test_enrichment_skipped_for_structured_script_even_below_threshold():
    """llm_enrich must not be called when context has ≥3 ALL-CAPS headers, even when score < 60."""
    from worker.tasks.enrich_context import enrich_context
    structured_ctx = "GOALKEEPER\nMartinez saves penalties.\nDEFENSE\nRomero leads.\nMIDFIELD\nDe Paul runs."
    job = _make_job()
    reel = _make_reel(context=structured_ctx)
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(25, ["too_short"])),
        patch("worker.tasks.enrich_context.llm_enrich") as mock_enrich,
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide"),
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    mock_enrich.assert_not_called()
    assert reel.enriched_context is None


# ── missing row guard ─────────────────────────────────────────────────────


def test_missing_reel_fails_job_with_actionable_message():
    """A deleted reel must produce an operator-readable error, not an AttributeError."""
    from api import models
    from worker.tasks.enrich_context import enrich_context

    job = _make_job()
    job.reel_id = 42
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else None

    with patch("worker.tasks.common.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="Reel 42 no longer exists"):
            enrich_context(1)

    assert job.status == models.JobStatus.failed
    assert "Reel 42 no longer exists" in job.error


# ── owner rollback wiring ─────────────────────────────────────────────────


def test_failed_enqueue_after_done_rolls_the_reel_back_from_generating():
    """If generate_guide.delay() raises after the enrich job is committed done,
    the reel is stuck in "generating" with no job for the reaper to catch."""
    from api.state import REEL_TRANSITIONS
    from worker.tasks.enrich_context import enrich_context

    job = _make_job()
    reel = _make_reel()
    reel.status.value = "generating"   # the state the (patched) enrich transition leaves it in
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(80, [])),
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide") as mock_gen,
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
        patch("worker.tasks.common.transition") as mock_rollback,
    ):
        mock_gen.delay.side_effect = ConnectionError("broker down")
        with pytest.raises(ConnectionError):
            enrich_context(1)

    mock_rollback.assert_called_once_with(reel, "failed", REEL_TRANSITIONS)


def test_enrichment_failure_rolls_the_reel_back_from_enriching():
    from api.state import REEL_TRANSITIONS
    from worker.tasks.enrich_context import enrich_context

    job = _make_job()
    reel = _make_reel()   # status "enriching"
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", side_effect=ValueError("scoring blew up")),
        patch("worker.tasks.common.transition") as mock_rollback,
    ):
        with pytest.raises(ValueError, match="scoring blew up"):
            enrich_context(1)

    mock_rollback.assert_called_once_with(reel, "failed", REEL_TRANSITIONS)
