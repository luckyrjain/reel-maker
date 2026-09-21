"""Tests for reap_stuck_jobs — stalled-job detection and reel/cut rollback."""
from unittest.mock import MagicMock, patch

from api import models
from api.state import CUT_TRANSITIONS, REEL_TRANSITIONS
from worker.tasks.maintenance import _revert_owner, reap_stuck_jobs


def _job(reel_id=None, cut_id=None):
    job = MagicMock()
    job.reel_id = reel_id
    job.cut_id = cut_id
    return job


def _reel(status):
    reel = MagicMock()
    reel.status.value = status
    return reel


def _cut(status):
    cut = MagicMock()
    cut.status.value = status
    return cut


# ── _revert_owner ─────────────────────────────────────────────────────────────

def test_revert_owner_fails_enriching_reel():
    """A reel stalled in 'enriching' must be rolled back, not only 'generating'."""
    db = MagicMock()
    db.get.return_value = _reel("enriching")
    with patch("worker.tasks.common.transition") as mock_transition:
        _revert_owner(db, _job(reel_id=5))
    mock_transition.assert_called_once_with(db.get.return_value, "failed", REEL_TRANSITIONS)


def test_revert_owner_fails_generating_reel():
    db = MagicMock()
    db.get.return_value = _reel("generating")
    with patch("worker.tasks.common.transition") as mock_transition:
        _revert_owner(db, _job(reel_id=5))
    mock_transition.assert_called_once_with(db.get.return_value, "failed", REEL_TRANSITIONS)


def test_revert_owner_leaves_finished_reel_alone():
    db = MagicMock()
    db.get.return_value = _reel("guide_ready")
    with patch("worker.tasks.common.transition") as mock_transition:
        _revert_owner(db, _job(reel_id=5))
    mock_transition.assert_not_called()


def test_revert_owner_fails_rendering_cut():
    db = MagicMock()
    db.get.return_value = _cut("rendering")
    with patch("worker.tasks.common.transition") as mock_transition:
        _revert_owner(db, _job(cut_id=9))
    mock_transition.assert_called_once_with(db.get.return_value, "failed", CUT_TRANSITIONS)


def test_revert_owner_fails_publishing_cut():
    """A publish worker killed mid-upload must not leave the cut stuck forever."""
    db = MagicMock()
    db.get.return_value = _cut("publishing")
    with patch("worker.tasks.common.transition") as mock_transition:
        _revert_owner(db, _job(cut_id=9))
    mock_transition.assert_called_once_with(db.get.return_value, "failed", CUT_TRANSITIONS)


def test_revert_owner_leaves_finished_cut_alone():
    db = MagicMock()
    db.get.return_value = _cut("published")
    with patch("worker.tasks.common.transition") as mock_transition:
        _revert_owner(db, _job(cut_id=9))
    mock_transition.assert_not_called()


# ── reap_stuck_jobs ───────────────────────────────────────────────────────────

def _run_reaper(stuck=(), never_started=()):
    """Drive reap_stuck_jobs with canned query results; returns the transition mock."""
    db = MagicMock()
    db.query.return_value.filter.return_value.all.side_effect = [list(stuck), list(never_started)]
    db.get.return_value = _reel("enriching")
    with (
        patch("worker.tasks.maintenance.SessionLocal", return_value=db),
        patch("worker.tasks.common.transition") as mock_transition,
    ):
        reap_stuck_jobs()
    return mock_transition


def test_reaps_running_job_without_heartbeat():
    job = _job(reel_id=7)
    mock_transition = _run_reaper(stuck=[job])
    assert job.status == models.JobStatus.failed
    assert "heartbeat" in job.error
    mock_transition.assert_called_once()


def test_reaps_pending_job_that_was_never_picked_up():
    """A job whose .delay() never reached a worker leaves the reel stuck forever."""
    job = _job(reel_id=7)
    mock_transition = _run_reaper(never_started=[job])
    assert job.status == models.JobStatus.failed
    assert "never picked up" in job.error
    mock_transition.assert_called_once()


def test_healthy_queue_reaps_nothing():
    mock_transition = _run_reaper()
    mock_transition.assert_not_called()


def test_pending_branch_filters_on_updated_at_not_created_at():
    """A job that went back to pending for a retry must not be reaped mid-backoff.

    Filtering on created_at would reap an old long-running job the moment it
    entered retry backoff, because created_at never moves.
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.all.side_effect = [[], []]

    with patch("worker.tasks.maintenance.SessionLocal", return_value=db):
        reap_stuck_jobs()

    filter_calls = db.query.return_value.filter.call_args_list
    assert len(filter_calls) == 2, "expected one running query and one pending query"
    pending_clause = str(filter_calls[1][0][1])
    assert "updated_at" in pending_clause
    assert "created_at" not in pending_clause
