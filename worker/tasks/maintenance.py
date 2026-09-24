"""
Periodic maintenance tasks run by Celery beat.

reap_stuck_jobs fails two kinds of stalled Job:
  - `running` with no heartbeat update for STALE_MINUTES — worker killed mid-task, or a body
    wedged past its max_runtime_s (job_task stops beating it).
  - `pending` with no update for PENDING_STALE_MINUTES — never picked up at all
    (broker down when .delay() was called, or no worker consuming the queue).
    Keyed on updated_at, not created_at, so a job sitting in retry backoff is
    not reaped for being old.

Without the second case the owning reel sits in `enriching`/`generating` forever
and the UI polls it every 2 s until the tab is closed.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from api import models
from api.db import SessionLocal
from api.state import JOB_IN_FLIGHT
from worker.celery_app import celery_app
from worker.tasks.common import rollback_owner

_log = logging.getLogger(__name__)

STALE_MINUTES = 5           # job_task beats every HEARTBEAT_INTERVAL_S (30 s), so >> that

# How long a job may sit `pending` before we conclude its message was lost. Deliberately long: a
# job legitimately queues behind others (render runs at concurrency 1 and one render can take an
# hour; four generation slots can each be busy for hours), and a reaped job is terminal, so
# reaping a merely-queued one silently drops it. Routers fail a job whose enqueue raised at once
# (fail_unenqueued), so this only covers a message lost AFTER a successful enqueue.
PENDING_STALE_MINUTES = 4 * 60


def _last_beat():
    """When a running job last proved it was alive. COALESCE: a row from before heartbeat_at
    existed has it NULL, and `NULL < cutoff` is never true, so it would never be reaped."""
    return func.coalesce(models.Job.heartbeat_at, models.Job.started_at,
                         models.Job.updated_at, models.Job.created_at)


def _last_touched():
    return func.coalesce(models.Job.updated_at, models.Job.created_at)


@celery_app.task
def reap_stuck_jobs():
    now = datetime.now(timezone.utc)
    running_stale = _last_beat() < now - timedelta(minutes=STALE_MINUTES)
    pending_stale = _last_touched() < now - timedelta(minutes=PENDING_STALE_MINUTES)
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
        ]
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
