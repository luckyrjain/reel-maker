"""Tests for reap_stuck_jobs — stalled-job detection and reel/cut rollback.

Runs against in-memory SQLite: the reaper's correctness lives in its compare-and-set
UPDATEs, which a mocked session cannot exercise.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from worker.tasks.maintenance import (
    PENDING_STALE_MINUTES, STALE_MINUTES, _reap_one, _revert_owner, reap_stuck_jobs,
)

_LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=2)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False)
    with patch("worker.tasks.maintenance.SessionLocal", session_factory):
        yield session_factory


def _make(factory, job_type, *, job_status, reel_status=models.ReelStatus.generating,
          cut_status=models.CutStatus.rendering, heartbeat_at=_LONG_AGO, updated_at=_LONG_AGO):
    db = factory()
    reel = models.Reel(context="ctx", status=reel_status)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=cut_status)
    db.add(cut)
    db.flush()
    job = models.Job(type=job_type, reel_id=reel.id, cut_id=cut.id, status=job_status)
    db.add(job)
    db.commit()
    # An explicit value overrides the column's onupdate, so the row really is old.
    db.query(models.Job).filter(models.Job.id == job.id).update(
        {"heartbeat_at": heartbeat_at, "updated_at": updated_at}, synchronize_session=False
    )
    db.commit()
    ids = (job.id, reel.id, cut.id)
    db.close()
    return ids


def _state(factory, job_id, reel_id, cut_id):
    db = factory()
    return db.get(models.Job, job_id), db.get(models.Reel, reel_id), db.get(models.Cut, cut_id)


# ── _revert_owner: per job type ───────────────────────────────────────────────

@pytest.mark.parametrize("job_type, reel_status, cut_status, reel_after, cut_after", [
    (models.JobType.enrich, models.ReelStatus.enriching, models.CutStatus.draft,
     models.ReelStatus.failed, models.CutStatus.draft),
    (models.JobType.generate, models.ReelStatus.generating, models.CutStatus.draft,
     models.ReelStatus.failed, models.CutStatus.draft),
    (models.JobType.render, models.ReelStatus.guide_ready, models.CutStatus.rendering,
     models.ReelStatus.guide_ready, models.CutStatus.failed),
    (models.JobType.publish, models.ReelStatus.guide_ready, models.CutStatus.publishing,
     models.ReelStatus.guide_ready, models.CutStatus.failed),
])
def test_revert_owner_fails_only_what_the_job_type_owns(factory, job_type, reel_status, cut_status,
                                                        reel_after, cut_after):
    job_id, reel_id, cut_id = _make(factory, job_type, job_status=models.JobStatus.running,
                                    reel_status=reel_status, cut_status=cut_status)
    db = factory()
    _revert_owner(db, db.get(models.Job, job_id))
    db.commit()
    _, reel, cut = _state(factory, job_id, reel_id, cut_id)
    assert (reel.status, cut.status) == (reel_after, cut_after)


@pytest.mark.parametrize("job_type, reel_status, cut_status", [
    (models.JobType.generate, models.ReelStatus.guide_ready, models.CutStatus.rendering),
    (models.JobType.render, models.ReelStatus.generating, models.CutStatus.published),
    (models.JobType.render, models.ReelStatus.guide_ready, models.CutStatus.publishing),   # a stale render job
    (models.JobType.publish, models.ReelStatus.guide_ready, models.CutStatus.published),
])
def test_revert_owner_leaves_an_owner_that_moved_on_alone(factory, job_type, reel_status, cut_status):
    job_id, reel_id, cut_id = _make(factory, job_type, job_status=models.JobStatus.running,
                                    reel_status=reel_status, cut_status=cut_status)
    db = factory()
    _revert_owner(db, db.get(models.Job, job_id))
    db.commit()
    _, reel, cut = _state(factory, job_id, reel_id, cut_id)
    assert (reel.status, cut.status) == (reel_status, cut_status)


# ── reap_stuck_jobs ───────────────────────────────────────────────────────────

def test_reaps_running_job_without_heartbeat_and_rolls_back_its_owner(factory):
    job_id, reel_id, cut_id = _make(factory, models.JobType.enrich, job_status=models.JobStatus.running,
                                    reel_status=models.ReelStatus.enriching)
    reap_stuck_jobs()
    job, reel, _ = _state(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed
    assert "heartbeat" in job.error
    assert reel.status == models.ReelStatus.failed


def test_reaps_pending_job_that_was_never_picked_up(factory):
    """A job whose .delay() never reached a worker leaves the reel stuck forever."""
    job_id, reel_id, cut_id = _make(factory, models.JobType.generate, job_status=models.JobStatus.pending)
    reap_stuck_jobs()
    job, reel, _ = _state(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed
    assert "never picked up" in job.error
    assert reel.status == models.ReelStatus.failed


def test_healthy_jobs_are_left_alone(factory):
    now = datetime.now(timezone.utc)
    running = _make(factory, models.JobType.render, job_status=models.JobStatus.running,
                    heartbeat_at=now - timedelta(seconds=30), updated_at=now)
    pending = _make(factory, models.JobType.render, job_status=models.JobStatus.pending, updated_at=now)
    finished = _make(factory, models.JobType.render, job_status=models.JobStatus.done)
    reap_stuck_jobs()
    assert _state(factory, *running)[0].status == models.JobStatus.running
    assert _state(factory, *pending)[0].status == models.JobStatus.pending
    assert _state(factory, *finished)[0].status == models.JobStatus.done


def test_pending_branch_keys_on_updated_at_not_created_at(factory):
    """A job that went back to pending for a retry must not be reaped mid-backoff.

    created_at never moves, so keying on it would reap an old long-running job the moment it
    entered retry backoff.
    """
    job_id, reel_id, cut_id = _make(
        factory, models.JobType.render, job_status=models.JobStatus.pending,
        updated_at=datetime.now(timezone.utc),
    )
    db = factory()
    db.query(models.Job).filter(models.Job.id == job_id).update(
        {"created_at": _LONG_AGO}, synchronize_session=False)
    db.commit()
    reap_stuck_jobs()
    assert _state(factory, job_id, reel_id, cut_id)[0].status == models.JobStatus.pending


def test_reap_one_backs_off_when_the_job_beat_after_it_was_selected(factory):
    """The reaper SELECTs a stale job, then the worker beats; the UPDATE must not fail it."""
    job_id, reel_id, cut_id = _make(factory, models.JobType.render, job_status=models.JobStatus.running,
                                    cut_status=models.CutStatus.rendering, reel_status=models.ReelStatus.guide_ready)
    reaper = factory()
    job = reaper.get(models.Job, job_id)             # the reaper's (now stale) snapshot

    worker = factory()
    worker.query(models.Job).filter(models.Job.id == job_id).update(
        {"heartbeat_at": datetime.now(timezone.utc)}, synchronize_session=False)
    worker.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    assert _reap_one(reaper, job, "stale", models.Job.heartbeat_at < cutoff) is False

    job, _, cut = _state(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.running
    assert cut.status == models.CutStatus.rendering


def test_reap_one_does_not_overwrite_a_job_that_finished_after_it_was_selected(factory):
    job_id, reel_id, cut_id = _make(factory, models.JobType.render, job_status=models.JobStatus.running)
    reaper = factory()
    job = reaper.get(models.Job, job_id)

    worker = factory()
    worker.query(models.Job).filter(models.Job.id == job_id).update(
        {"status": models.JobStatus.done}, synchronize_session=False)
    worker.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    assert _reap_one(reaper, job, "stale", models.Job.heartbeat_at < cutoff) is False
    assert _state(factory, job_id, reel_id, cut_id)[0].status == models.JobStatus.done


def test_one_bad_job_does_not_stop_the_rest(factory):
    first = _make(factory, models.JobType.generate, job_status=models.JobStatus.running)
    second = _make(factory, models.JobType.generate, job_status=models.JobStatus.running)
    real = _revert_owner
    calls = []

    def flaky(db, job):
        calls.append(job.id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        real(db, job)

    with patch("worker.tasks.maintenance._revert_owner", side_effect=flaky):
        reap_stuck_jobs()

    assert len(calls) == 2
    states = {_state(factory, *ids)[0].status for ids in (first, second)}
    assert models.JobStatus.failed in states


def test_stale_thresholds_leave_room_for_the_heartbeat_thread():
    from worker.tasks import common
    assert STALE_MINUTES * 60 >= 4 * common.HEARTBEAT_INTERVAL_S
    assert PENDING_STALE_MINUTES > STALE_MINUTES
