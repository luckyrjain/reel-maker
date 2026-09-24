"""
Periodic maintenance tasks run by Celery beat.

reap_stuck_jobs fails three kinds of stalled Job:
  - `running` with no heartbeat update for STALE_MINUTES — worker killed mid-task, or a body
    wedged past its max_runtime_s (job_task stops beating it).
  - `pending` with no update for PENDING_STALE_MINUTES — never picked up at all
    (broker down when .delay() was called, or no worker consuming the queue).
    Keyed on updated_at, not created_at, so a job sitting in retry backoff is
    not reaped for being old.
  - `done` with no error, whose owner is STILL sitting in the in-flight state only that job was
    ever supposed to resolve, for DONE_ORPHAN_STALE_MINUTES — job_task's own `after_commit_failed`
    cleanup hook (worker/tasks/common.py::_stamp_failed_and_run_cleanup) ran into a compound
    failure recording its own recovery, so the fail-stamp that should have flipped it to `failed`
    never landed. See docs/roadmap.md's Phase 3.9 "one narrow gap remains" note for the full story
    — this is the mitigation that section says to add once a second `after_commit_failed` hook
    exists; `enrich_context`'s own hook (`_abandon_generate`) happens to self-heal without this,
    via its own orphaned follow-up Job.

Without the second case the owning reel sits in `enriching`/`generating` forever
and the UI polls it every 2 s until the tab is closed. Without the third, an owner can be stuck
in the same way with no Job left in the reaper's `running`/`pending` scan to ever surface it.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func

from api import models
from api.db import SessionLocal
from api.state import JOB_IN_FLIGHT
from worker.celery_app import celery_app
from worker.tasks.common import _OWNER_MODELS, rollback_owner

_log = logging.getLogger(__name__)

STALE_MINUTES = 5           # job_task beats every HEARTBEAT_INTERVAL_S (30 s), so >> that

# How long a job may sit `pending` before we conclude its message was lost. Deliberately long: a
# job legitimately queues behind others (render runs at concurrency 1 and one render can take an
# hour; four generation slots can each be busy for hours), and a reaped job is terminal, so
# reaping a merely-queued one silently drops it. Routers fail a job whose enqueue raised at once
# (fail_unenqueued), so this only covers a message lost AFTER a successful enqueue.
PENDING_STALE_MINUTES = 4 * 60

# How long a `done`+ownerless-error job may sit before its owner is assumed genuinely stuck, not
# just mid-completion. Generous relative to STALE_MINUTES: this state should never persist even
# briefly if job_task's own invariant holds (a job's done-stamp and its owner's transition commit
# together, atomically -- see job_task's docstring), so the margin here is purely to cover the
# reaper's own read racing a job that's genuinely still finishing, not a judgment call about how
# long is "too long".
DONE_ORPHAN_STALE_MINUTES = 15


def _last_beat():
    """When a running job last proved it was alive. COALESCE: a row from before heartbeat_at
    existed has it NULL, and `NULL < cutoff` is never true, so it would never be reaped."""
    return func.coalesce(models.Job.heartbeat_at, models.Job.started_at,
                         models.Job.updated_at, models.Job.created_at)


def _last_touched():
    return func.coalesce(models.Job.updated_at, models.Job.created_at)


def _done_orphan_candidates(db, done_orphan_stale):
    """`done` jobs whose owner is still sitting in the in-flight state only that job was ever
    supposed to resolve -- the on-disk signature of a lost after_commit_failed fail-stamp (see the
    module docstring). One query per job type: each maps to a different owner table and a
    different in-flight state, so this can't be expressed as a single filter.

    The owner-state join is load-bearing, not an optimization: without it, this would eventually
    match nearly every job the pipeline has ever completed, since a genuinely successful `done`
    job's `updated_at` is exactly as "stale" by this definition, forever, as a truly stuck one --
    the two are indistinguishable by the job's own columns alone. A job whose owner has already
    moved on (the overwhelming majority) never matches the join at all.
    """
    candidates = []
    for job_type, (owner_kind, owner_state) in JOB_IN_FLIGHT.items():
        owner_model = _OWNER_MODELS[owner_kind]
        owner_fk = models.Job.reel_id if owner_kind == "reel" else models.Job.cut_id
        reason = (
            f"after_commit_failed cleanup never completed for this job (it finished, but its "
            f"owner is still '{owner_state}'); the reaper marked it failed and rolled the owner "
            f"back -- see docs/roadmap.md's Phase 3.9 section"
        )
        stale_clause = and_(models.Job.error.is_(None), done_orphan_stale)
        rows = (
            db.query(models.Job.id)
            .join(owner_model, owner_fk == owner_model.id)
            .filter(
                models.Job.type == job_type,
                models.Job.status == models.JobStatus.done,
                stale_clause,
                owner_model.status == owner_state,
            )
            .all()
        )
        candidates += [(job_id, models.JobStatus.done, reason, stale_clause) for (job_id,) in rows]
    return candidates


@celery_app.task
def reap_stuck_jobs():
    now = datetime.now(timezone.utc)
    running_stale = _last_beat() < now - timedelta(minutes=STALE_MINUTES)
    pending_stale = _last_touched() < now - timedelta(minutes=PENDING_STALE_MINUTES)
    done_orphan_stale = _last_touched() < now - timedelta(minutes=DONE_ORPHAN_STALE_MINUTES)
    db = SessionLocal()
    try:
        # Snapshot (id, status) as plain values BEFORE the first commit: the session expires its
        # instances on commit, and a re-read status would defeat the status pin in _reap_one.
        candidates = [
            (j.id, j.status, f"Worker stopped responding (no heartbeat for {STALE_MINUTES} min).", running_stale)
            for j in db.query(models.Job)
            .filter(models.Job.status == models.JobStatus.running, running_stale)
            .all()
        ] + [
            (j.id, j.status, f"Job was never picked up by a worker within {PENDING_STALE_MINUTES} min.", pending_stale)
            for j in db.query(models.Job)
            .filter(models.Job.status == models.JobStatus.pending, pending_stale)
            .all()
        ] + _done_orphan_candidates(db, done_orphan_stale)
        for job_id, seen_status, reason, stale_clause in candidates:
            try:
                _reap_one(db, job_id, seen_status, reason, stale_clause)
            except Exception:
                # e.g. a deadlock victim: the next pass retries it, but say so.
                _log.warning("could not reap job %s; will retry on the next pass", job_id, exc_info=True)
                db.rollback()
    finally:
        db.close()


def _reap_one(db, job_id: int, seen_status, reason: str, stale_clause) -> bool:
    """Fail one stalled job and roll its owner back. False if it is no longer stale.

    Both the status the SELECT saw and the staleness are re-checked inside the UPDATE: the
    job may have finished, beaten, or been reset for a retry since. Losing that race means
    leave it alone.
    """
    claimed = (
        db.query(models.Job)
        .filter(models.Job.id == job_id, models.Job.status == seen_status, stale_clause)
        .update({"status": models.JobStatus.failed, "error": reason}, synchronize_session=False)
    )
    if claimed == 0:
        db.rollback()
        return False
    job = db.get(models.Job, job_id)
    job.status = models.JobStatus.failed
    job.error = reason
    _revert_owner(db, job)
    db.commit()
    return True


def _revert_owner(db, job: models.Job) -> None:
    """Roll the reel/cut back to a state where the operator can retry.

    Only the state this job's type owns is rolled back (JOB_IN_FLIGHT), the same rule as
    the task's own failure path — a stale job must never flip an owner that has moved on.
    """
    kind, state = JOB_IN_FLIGHT[job.type.value]
    rollback_owner(db, job, kind, {state})
