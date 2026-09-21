"""
Periodic maintenance tasks run by Celery beat.

reap_stuck_jobs fails two kinds of stalled Job:
  - `running` with no heartbeat update for STALE_MINUTES — worker killed mid-task.
  - `pending` with no update for PENDING_STALE_MINUTES — never picked up at all
    (broker down when .delay() was called, or no worker consuming the queue).
    Keyed on updated_at, not created_at, so a job sitting in retry backoff is
    not reaped for being old.

Without the second case the owning reel sits in `enriching`/`generating` forever
and the UI polls it every 2 s until the tab is closed.
"""
from datetime import datetime, timedelta, timezone

from api import models
from api.db import SessionLocal
from api.state import JOB_IN_FLIGHT
from worker.celery_app import celery_app
from worker.tasks.common import rollback_owner

STALE_MINUTES = 5           # job_task beats every HEARTBEAT_INTERVAL_S (30 s), so >> that
PENDING_STALE_MINUTES = 30  # > the longest a job may legitimately queue behind a render


@celery_app.task
def reap_stuck_jobs():
    now = datetime.now(timezone.utc)
    running_cutoff = now - timedelta(minutes=STALE_MINUTES)
    pending_cutoff = now - timedelta(minutes=PENDING_STALE_MINUTES)
    db = SessionLocal()
    try:
        stuck = (
            db.query(models.Job)
            .filter(
                models.Job.status == models.JobStatus.running,
                models.Job.heartbeat_at < running_cutoff,
            )
            .all()
        )
        never_started = (
            db.query(models.Job)
            .filter(
                models.Job.status == models.JobStatus.pending,
                models.Job.updated_at < pending_cutoff,
            )
            .all()
        )
        for job, reason, stale_clause in (
            [(j, f"Worker stopped responding (no heartbeat for {STALE_MINUTES} min).",
              models.Job.heartbeat_at < running_cutoff) for j in stuck]
            + [(j, f"Job was never picked up by a worker within {PENDING_STALE_MINUTES} min.",
                models.Job.updated_at < pending_cutoff) for j in never_started]
        ):
            try:
                _reap_one(db, job, reason, stale_clause)
            except Exception:
                db.rollback()
    finally:
        db.close()


def _reap_one(db, job: models.Job, reason: str, stale_clause) -> bool:
    """Fail one stalled job and roll its owner back. False if it is no longer stale.

    The staleness is re-checked inside the UPDATE itself: the job may have finished, or
    beaten, between the SELECT that found it and now. Losing that race means leave it alone.
    """
    claimed = (
        db.query(models.Job)
        .filter(models.Job.id == job.id, models.Job.status == job.status, stale_clause)
        .update({"status": models.JobStatus.failed, "error": reason}, synchronize_session=False)
    )
    if claimed == 0:
        db.rollback()
        return False
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
