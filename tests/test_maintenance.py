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

_MINUTE = timedelta(minutes=1)

_LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=6)


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
    # cut_status must reflect where a genuinely successful render leaves its cut (in_review), not
    # the default `rendering` -- a `done` job whose cut is still `rendering` is exactly the
    # done-orphan signature reap_stuck_jobs now also looks for, and _make's default cut_status
    # exists to represent an in-flight owner, not a completed one.
    finished = _make(factory, models.JobType.render, job_status=models.JobStatus.done,
                     cut_status=models.CutStatus.in_review)
    reap_stuck_jobs()
    assert _state(factory, *running)[0].status == models.JobStatus.running
    assert _state(factory, *pending)[0].status == models.JobStatus.pending
    assert _state(factory, *finished)[0].status == models.JobStatus.done


# ── done-orphan reaping: a lost after_commit_failed fail-stamp ─────────────────

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
def test_reaps_a_done_job_whose_owner_is_still_in_flight(factory, job_type, reel_status, cut_status,
                                                          reel_after, cut_after):
    """The on-disk signature of a lost after_commit_failed fail-stamp: `done`, no error, and the
    owner still sitting exactly where only this job type was ever responsible for moving it on
    from. Mirrors test_revert_owner_fails_only_what_the_job_type_owns's per-type matrix, since the
    owner-rollback half of this is the same call, just reached through the done-orphan path."""
    job_id, reel_id, cut_id = _make(factory, job_type, job_status=models.JobStatus.done,
                                    reel_status=reel_status, cut_status=cut_status)
    reap_stuck_jobs()
    job, reel, cut = _state(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed
    assert "after_commit_failed" in job.error
    assert (reel.status, cut.status) == (reel_after, cut_after)


@pytest.mark.parametrize("job_type, reel_status, cut_status", [
    (models.JobType.enrich, models.ReelStatus.generating, models.CutStatus.draft),
    (models.JobType.generate, models.ReelStatus.guide_ready, models.CutStatus.draft),
    (models.JobType.render, models.ReelStatus.guide_ready, models.CutStatus.in_review),
    (models.JobType.publish, models.ReelStatus.guide_ready, models.CutStatus.published),
])
def test_does_not_reap_a_done_job_whose_owner_already_moved_on(factory, job_type, reel_status, cut_status):
    """The overwhelming common case: a genuinely successful `done` job whose owner transitioned
    forward as part of that same success, atomically with the done-stamp. Its `updated_at` is
    exactly as "stale" as an orphan's by this definition, forever after -- only the owner-state
    join tells the two apart."""
    job_id, reel_id, cut_id = _make(factory, job_type, job_status=models.JobStatus.done,
                                    reel_status=reel_status, cut_status=cut_status)
    reap_stuck_jobs()
    job, reel, cut = _state(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.done
    assert (reel.status, cut.status) == (reel_status, cut_status)


def test_does_not_reap_a_done_job_that_already_has_an_error(factory):
    """A `done` job with a non-NULL error is not this signature (a `done` job's error is always
    None on genuine success -- see job_task's own done-stamp write) -- whatever put an error there,
    it isn't the failure mode this reap path exists for, and re-marking it failed a second time,
    with a misleading reason, would be worse than leaving it exactly as some other process left it."""
    job_id, reel_id, cut_id = _make(factory, models.JobType.enrich, job_status=models.JobStatus.done,
                                    reel_status=models.ReelStatus.enriching)
    db = factory()
    # updated_at must be pinned back to _LONG_AGO too (onupdate would otherwise stamp it "now" and
    # the job would be excluded from candidates for being fresh, not for having an error -- see
    # _make's own comment on this exact onupdate gotcha).
    db.query(models.Job).filter(models.Job.id == job_id).update(
        {"error": "some other note", "updated_at": _LONG_AGO}, synchronize_session=False
    )
    db.commit()
    reap_stuck_jobs()
    job, reel, _ = _state(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.done
    assert job.error == "some other note"
    assert reel.status == models.ReelStatus.enriching


def test_does_not_reap_a_recently_completed_done_job(factory):
    """A job that finished moments ago, with its owner (so far) still in-flight, must not be
    treated as stuck -- DONE_ORPHAN_STALE_MINUTES exists exactly to give job_task's own
    done-stamp-plus-owner-transition commit room to have landed just before the reaper's read,
    not to declare a fresh completion broken."""
    now = datetime.now(timezone.utc)
    job_id, reel_id, cut_id = _make(factory, models.JobType.enrich, job_status=models.JobStatus.done,
                                    reel_status=models.ReelStatus.enriching, updated_at=now)
    reap_stuck_jobs()
    job, reel, _ = _state(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.done
    assert reel.status == models.ReelStatus.enriching


def test_done_orphan_threshold_is_fifteen_minutes(factory):
    from worker.tasks.maintenance import DONE_ORPHAN_STALE_MINUTES
    assert DONE_ORPHAN_STALE_MINUTES == 15

    now = datetime.now(timezone.utc)
    just_inside = _make(factory, models.JobType.enrich, job_status=models.JobStatus.done,
                        reel_status=models.ReelStatus.enriching,
                        updated_at=now - _MINUTE * (DONE_ORPHAN_STALE_MINUTES - 1))
    just_outside = _make(factory, models.JobType.generate, job_status=models.JobStatus.done,
                         reel_status=models.ReelStatus.generating,
                         updated_at=now - _MINUTE * (DONE_ORPHAN_STALE_MINUTES + 1))
    reap_stuck_jobs()
    assert _state(factory, *just_inside)[0].status == models.JobStatus.done
    assert _state(factory, *just_outside)[0].status == models.JobStatus.failed


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
    worker = factory()
    worker.query(models.Job).filter(models.Job.id == job_id).update(
        {"heartbeat_at": datetime.now(timezone.utc)}, synchronize_session=False)
    worker.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    assert _reap_one(factory(), job_id, models.JobStatus.running, "stale", models.Job.heartbeat_at < cutoff) is False

    job, _, cut = _state(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.running
    assert cut.status == models.CutStatus.rendering


def test_reap_one_does_not_overwrite_a_job_that_finished_after_it_was_selected(factory):
    job_id, reel_id, cut_id = _make(factory, models.JobType.render, job_status=models.JobStatus.running)
    worker = factory()
    worker.query(models.Job).filter(models.Job.id == job_id).update(
        {"status": models.JobStatus.done}, synchronize_session=False)
    worker.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    assert _reap_one(factory(), job_id, models.JobStatus.running, "stale", models.Job.heartbeat_at < cutoff) is False
    assert _state(factory, job_id, reel_id, cut_id)[0].status == models.JobStatus.done


def test_the_status_pin_is_the_select_snapshot_not_a_later_re_read(factory):
    """A job whose worker recovered and failed/reset it between the SELECT and the reaper's turn keeps
    its real state, even though its heartbeat is still old."""
    _make(factory, models.JobType.generate, job_status=models.JobStatus.running)
    second = _make(factory, models.JobType.generate, job_status=models.JobStatus.running)
    real = _reap_one
    calls = []

    def racing(db, job_id, seen_status, reason, clause):
        if not calls:   # after the first reap commits, the second job's worker fails it for real
            other = factory()
            other.query(models.Job).filter(models.Job.id == second[0]).update(
                {"status": models.JobStatus.failed, "error": "REAL ERROR: ffmpeg exit 1"},
                synchronize_session=False)
            other.commit()
        calls.append(job_id)
        return real(db, job_id, seen_status, reason, clause)

    with patch("worker.tasks.maintenance._reap_one", side_effect=racing):
        reap_stuck_jobs()

    assert len(calls) == 2
    assert "REAL ERROR" in _state(factory, *second)[0].error


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


@pytest.mark.parametrize("job_type, age_minutes, reaped", [
    (models.JobType.render, 120, False),     # queued behind other hour-long renders (concurrency 1)
    (models.JobType.generate, 120, False),   # four generation slots can each be busy for hours
    (models.JobType.publish, 120, False),
    (models.JobType.render, 5 * 60, True),
    (models.JobType.generate, 5 * 60, True),
    (models.JobType.enrich, 5 * 60, True),
])
def test_a_job_queued_for_hours_is_not_mistaken_for_a_lost_message(factory, job_type, age_minutes, reaped):
    """A reaped job is terminal, so reaping a merely-queued one silently drops it."""
    job_id, reel_id, cut_id = _make(
        factory, job_type, job_status=models.JobStatus.pending,
        updated_at=datetime.now(timezone.utc) - age_minutes * _MINUTE,
    )
    reap_stuck_jobs()
    status = _state(factory, job_id, reel_id, cut_id)[0].status
    assert (status == models.JobStatus.failed) is reaped


def test_the_pending_threshold_outlasts_a_queue_of_capped_renders():
    from worker.celery_app import celery_app
    celery_app.loader.import_default_modules()
    render_cap = celery_app.tasks["worker.tasks.render.render_cut"].run.max_runtime_s
    # A queued render waits behind at most a few renders that each run up to their cap.
    assert PENDING_STALE_MINUTES * 60 >= 3 * render_cap


def test_a_running_job_with_no_heartbeat_at_all_is_still_reaped(factory):
    """Rows from before heartbeat_at existed have it NULL; `NULL < cutoff` is never true."""
    job_id, reel_id, cut_id = _make(factory, models.JobType.generate, job_status=models.JobStatus.running)
    db = factory()
    db.query(models.Job).filter(models.Job.id == job_id).update(
        {"heartbeat_at": None, "started_at": None, "updated_at": _LONG_AGO}, synchronize_session=False)
    db.commit()
    reap_stuck_jobs()
    assert _state(factory, job_id, reel_id, cut_id)[0].status == models.JobStatus.failed


def test_a_pending_job_with_no_updated_at_is_judged_by_created_at(factory):
    job_id, reel_id, cut_id = _make(factory, models.JobType.generate, job_status=models.JobStatus.pending)
    db = factory()
    db.query(models.Job).filter(models.Job.id == job_id).update(
        {"updated_at": None, "created_at": _LONG_AGO}, synchronize_session=False)
    db.commit()
    reap_stuck_jobs()
    assert _state(factory, job_id, reel_id, cut_id)[0].status == models.JobStatus.failed


# ── hardening found by mutation testing ───────────────────────────────────────

def test_a_job_whose_owner_rollback_fails_is_left_running_and_untouched(factory):
    """If rolling the owner back blows up, the job's failed stamp must not be committed by the NEXT
    job's commit, leaving the reel stuck in `generating` forever."""
    first = _make(factory, models.JobType.generate, job_status=models.JobStatus.running)
    second = _make(factory, models.JobType.generate, job_status=models.JobStatus.running)
    real, seen = _revert_owner, []

    def flaky(db, job):
        seen.append(job.id)
        if len(seen) == 1:
            raise RuntimeError("boom")
        real(db, job)

    with patch("worker.tasks.maintenance._revert_owner", side_effect=flaky):
        reap_stuck_jobs()

    bad = first if seen[0] == first[0] else second
    good = second if bad == first else first
    bad_job, bad_reel, _ = _state(factory, *bad)
    good_job, good_reel, _ = _state(factory, *good)
    assert bad_job.status == models.JobStatus.running          # retried on the next pass
    assert bad_reel.status == models.ReelStatus.generating
    assert good_job.status == models.JobStatus.failed and good_reel.status == models.ReelStatus.failed


def test_the_running_threshold_is_five_minutes(factory):
    now = datetime.now(timezone.utc)
    stale = _make(factory, models.JobType.render, job_status=models.JobStatus.running,
                  heartbeat_at=now - 10 * _MINUTE, updated_at=now - 10 * _MINUTE)
    fresh = _make(factory, models.JobType.render, job_status=models.JobStatus.running,
                  heartbeat_at=now - 2 * _MINUTE, updated_at=now - 2 * _MINUTE)
    reap_stuck_jobs()
    assert _state(factory, *stale)[0].status == models.JobStatus.failed
    assert _state(factory, *fresh)[0].status == models.JobStatus.running


def test_a_running_job_is_judged_by_heartbeat_at_not_updated_at(factory):
    now = datetime.now(timezone.utc)
    ids = _make(factory, models.JobType.render, job_status=models.JobStatus.running,
                heartbeat_at=now, updated_at=now - timedelta(hours=2))
    reap_stuck_jobs()
    assert _state(factory, *ids)[0].status == models.JobStatus.running
