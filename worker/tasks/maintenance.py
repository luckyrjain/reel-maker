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
from api.state import IN_FLIGHT_STATES
from worker.celery_app import celery_app
from worker.tasks.common import rollback_owner

STALE_MINUTES = 5           # > the longest gap between heartbeat updates in any task
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
        for job, reason in (
            [(j, f"Worker stopped responding (no heartbeat for {STALE_MINUTES} min).") for j in stuck]
            + [(j, f"Job was never picked up by a worker within {PENDING_STALE_MINUTES} min.") for j in never_started]
        ):
            try:
                job.status = models.JobStatus.failed
                job.error = reason
                _revert_owner(db, job)
                db.commit()
            except Exception:
                db.rollback()
    finally:
        db.close()


def _revert_owner(db, job: models.Job) -> None:
    """Roll the reel/cut back to a state where the operator can retry.

    A stalled job of any type may own either kind, so this rolls back the union
    of every in-flight state. Each task's own failure path uses the narrower
    per-job-type entry in JOB_IN_FLIGHT (see worker/tasks/common.py::job_task).
    """
    for kind, states in IN_FLIGHT_STATES.items():
        rollback_owner(db, job, kind, states)
