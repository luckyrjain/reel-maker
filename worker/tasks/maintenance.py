"""
Periodic maintenance tasks run by Celery beat.

reap_stuck_jobs fails two kinds of stalled Job:
  - `running` with no heartbeat update for STALE_MINUTES — worker killed mid-task, or a body
    wedged past its max_runtime_s (job_task stops beating it).
  - `pending` with no update for PENDING_STALE_MINUTES[job type] — never picked up at all
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

# How long a job may sit `pending` before we conclude its message was lost. Longer than the
# longest a job can legitimately queue: render runs at concurrency 1 and each render can take
# an hour (job_task's max_runtime_s), so a cut queued behind a few others waits hours. Reaping
# it early would silently drop the render, because a failed job is terminal.
PENDING_STALE_MINUTES = {"enrich": 30, "generate": 30, "render": 4 * 60, "publish": 60}


@celery_app.task
def reap_stuck_jobs():
    now = datetime.now(timezone.utc)
    running_cutoff = now - timedelta(minutes=STALE_MINUTES)
    db = SessionLocal()
    try:
        # Snapshot (id, status, ...) as plain values BEFORE the first commit: the session
        # expires its instances on commit, and a re-read status would defeat the status pin below.
        candidates = [
            (j.id, j.status, f"Worker stopped responding (no heartbeat for {STALE_MINUTES} min).",
             models.Job.heartbeat_at < running_cutoff)
            for j in db.query(models.Job)
            .filter(models.Job.status == models.JobStatus.running,
                    models.Job.heartbeat_at < running_cutoff)
            .all()
        ]
        for job_type, minutes in PENDING_STALE_MINUTES.items():
            cutoff = now - timedelta(minutes=minutes)
            candidates += [
                (j.id, j.status, f"Job was never picked up by a worker within {minutes} min.",
                 models.Job.updated_at < cutoff)
                for j in db.query(models.Job)
                .filter(models.Job.status == models.JobStatus.pending,
                        models.Job.type == models.JobType(job_type),
                        models.Job.updated_at < cutoff)
                .all()
            ]
        for job_id, seen_status, reason, stale_clause in candidates:
            try:
                _reap_one(db, job_id, seen_status, reason, stale_clause)
            except Exception:
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
