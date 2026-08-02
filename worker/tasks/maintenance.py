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
from api.state import REEL_TRANSITIONS, CUT_TRANSITIONS, transition
from worker.celery_app import celery_app

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
    """Roll the reel/cut back to a state where the operator can retry."""
    if job.reel_id:
        reel = db.get(models.Reel, job.reel_id)
        if reel and reel.status.value in ("enriching", "generating"):
            try:
                transition(reel, "failed", REEL_TRANSITIONS)
            except ValueError:
                pass
    if job.cut_id:
        cut = db.get(models.Cut, job.cut_id)
        if cut and cut.status.value == "rendering":
            try:
                transition(cut, "failed", CUT_TRANSITIONS)
            except ValueError:
                pass
