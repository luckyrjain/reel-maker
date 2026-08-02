"""
Periodic maintenance tasks run by Celery beat.

reap_stuck_jobs: fails any Job that has been in `running` state without a
heartbeat update for longer than STALE_MINUTES. This prevents the UI from
polling forever when a worker is killed mid-task.
"""
from datetime import datetime, timedelta, timezone

from api import models
from api.db import SessionLocal
from api.state import REEL_TRANSITIONS, CUT_TRANSITIONS, transition
from worker.celery_app import celery_app

STALE_MINUTES = 5   # > the longest gap between heartbeat updates in any task


@celery_app.task
def reap_stuck_jobs():
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    db = SessionLocal()
    try:
        stuck = (
            db.query(models.Job)
            .filter(
                models.Job.status == models.JobStatus.running,
                models.Job.heartbeat_at < cutoff,
            )
            .all()
        )
        for job in stuck:
            try:
                job.status = models.JobStatus.failed
                job.error = f"Worker stopped responding (no heartbeat for {STALE_MINUTES} min)."
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
        if reel and reel.status.value == "generating":
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
