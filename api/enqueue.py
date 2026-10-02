"""The one place a router turns a freshly built Job into an enqueued Celery task.

The sequence is a correctness policy, not boilerplate: the owner-state change and the Job row
commit together, and NOTHING stays open across the broker call (a slow broker failure would outlive
an idle-in-transaction timeout and the row lock the router took). If the broker call fails, the
Job is failed and its owner freed at once, otherwise the cut/reel sits in flight answering every
retry with 409 until the reaper's multi-hour pending threshold.

Deliberately not shared with the worker-side enqueue sites: ``enrich_context`` enqueues
``generate_guide`` after its own done-stamp and cleans up via ``after_commit_failed``, and the
reaper's resume re-enqueues after commit and leaves a failed enqueue ``pending`` for its own
pending-stale sweep. Different failure policies, no HTTP.
"""
from fastapi import HTTPException

from worker.tasks.common import fail_unenqueued


def enqueue_job(db, job, task, *, what: str) -> None:
    """Add, commit and enqueue ``job``; on a broker failure fail it and raise a 503.

    The caller does its owner-state mutation (``transition(...)``) first and must not commit:
    the Job and that mutation land in one transaction here. ``job`` is an un-added ``Job``;
    ``task`` is the Celery task object (``task.delay(job.id)`` is called). ``what`` names the thing
    in the 503 text ("render", "publish", "job"). ``job`` is refreshed on a successful return,
    since the commit expired it.
    """
    db.add(job)
    db.flush()
    # Plain values captured BEFORE the commit: router sessions use expire_on_commit=True, so reading
    # job.id / job.type afterwards would lazy-load on a session whose transaction may be dead.
    job_id = job.id
    job_type = job.type.value
    db.commit()

    try:
        task.delay(job_id)
    except Exception as exc:
        fail_unenqueued(db, job_id, job_type, exc)
        raise HTTPException(status_code=503, detail=f"Could not queue the {what} — try again") from exc
    db.refresh(job)
