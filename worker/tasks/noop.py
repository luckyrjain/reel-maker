import time

from worker.celery_app import celery_app
from api.db import SessionLocal
from api import models
from api.state import transition, REEL_TRANSITIONS


@celery_app.task(bind=True)
def noop_job(self, job_id: int):
    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        job.status = models.JobStatus.running
        job.attempts = (job.attempts or 0) + 1
        db.commit()

        time.sleep(1)
        job.progress = 50
        db.commit()

        time.sleep(1)
        job.progress = 100
        job.status = models.JobStatus.done

        reel = db.get(models.Reel, job.reel_id)
        transition(reel, "guide_ready", REEL_TRANSITIONS)
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
            job.error = str(exc)
            db.commit()
        raise
    finally:
        db.close()
