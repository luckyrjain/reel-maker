"""Shared helpers for Celery tasks.

is_transient_error / should_retry decide whether a failure is worth another
attempt. They stay pure functions (no Celery, no db) so the decision is
unit-testable without a broker or a task context.

job_task is the one place that owns a Job's lifecycle: idempotency guard,
running-stamp, atomic done-stamp, transient-retry reset and failure stamp with
owner rollback. Task modules supply only the domain work as a body function.
"""
import functools
import inspect
import subprocess
from collections.abc import Collection
from datetime import datetime, timezone

import httpx

from api import models
from api.db import SessionLocal
from api.state import CUT_TRANSITIONS, JOB_IN_FLIGHT, REEL_TRANSITIONS, transition

_TRANSIENT_TYPES = (
    httpx.TransportError,      # connect/read/write timeouts, connection errors
    TimeoutError,
    subprocess.TimeoutExpired,
    ConnectionError,
)
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def is_transient_error(exc: BaseException) -> bool:
    """True when re-running the same input could plausibly succeed."""
    if isinstance(exc, _TRANSIENT_TYPES):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    return False


def should_retry(exc: BaseException, retries: int, max_retries: int) -> bool:
    """Retry decision, split out from the Celery glue so it is unit-testable."""
    return retries < max_retries and is_transient_error(exc)


def heartbeat(db, job, progress: int) -> None:
    """Record progress and prove the worker is alive.

    reap_stuck_jobs fails any running job whose heartbeat_at goes stale for
    more than STALE_MINUTES, so every long task must call this at each milestone.
    """
    job.progress = progress
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()


_OWNER_MODELS = {"reel": models.Reel, "cut": models.Cut}
_OWNER_TRANSITIONS = {"reel": REEL_TRANSITIONS, "cut": CUT_TRANSITIONS}


def rollback_owner(db, job, kind: str, states: Collection[str]) -> None:
    """Fail the job's reel/cut when it is still in one of the given in-flight states.

    A row that already moved on (or was deleted) is left alone, and an invalid
    transition is swallowed — the failure handler must never mask the real error.
    """
    row_id = job.reel_id if kind == "reel" else job.cut_id
    if not row_id:
        return
    row = db.get(_OWNER_MODELS[kind], row_id)
    if row is None or row.status.value not in states:
        return
    try:
        transition(row, "failed", _OWNER_TRANSITIONS[kind])
    except ValueError:
        pass


_TASK_SIGNATURE = inspect.Signature([
    inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("job_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
])


def job_task(
    job_type: str,
    *,
    prepare=None,
    after_commit=None,
    after_commit_fail_owner: tuple[str, str] | None = None,
    start_progress: int = 5,
):
    """Wrap a task body in the Job lifecycle. Apply beneath ``@celery_app.task(bind=True, ...)``.

    The body is ``body(self, db, job, ctx) -> result``. It does the domain work,
    calls ``heartbeat()`` at milestones, and raises to fail. It must NOT commit
    after its last domain mutation: the done-stamp commit lands those mutations
    atomically with ``status = done``.

    Order of events:
      1. ``job is None`` or ``status in (done, running)`` -> return (redelivery no-op).
      2. ``prepare(db, job) -> ctx`` (optional) — load rows, null-guard, budget
         checks. Runs before the running-stamp, so a failure here never bumps
         ``attempts`` or sets ``started_at``.
      3. running-stamp, then the body.
      4. done-stamp + commit, then ``after_commit(result)`` (optional) — for work
         that must only happen once the job is durably done, e.g. enqueueing the
         next job. It gets the body's return value, never a session.

    Failure: roll back, then either reset to ``pending`` and ``self.retry()`` (transient
    error with retries left; ``attempts`` is NOT bumped — entry already did) or stamp
    ``failed`` and roll the owner back per ``JOB_IN_FLIGHT[job_type]``. A failure in
    ``after_commit`` never retries (the body already ran) and additionally rolls back
    ``after_commit_fail_owner`` — the owner state left behind when the follow-up
    work could not be enqueued.

    The wrapper's signature is forced to ``(self, job_id)``: functools.wraps would
    expose the body's signature to Celery, and ``.delay(job_id)`` then raises TypeError.
    """
    owner_kind, owner_state = JOB_IN_FLIGHT[job_type]

    def decorate(body):
        @functools.wraps(body)
        def run(self, job_id):
            db = SessionLocal()
            committed = False
            try:
                job = db.get(models.Job, job_id)
                if job is None:
                    return
                if job.status in (models.JobStatus.done, models.JobStatus.running):
                    return

                ctx = prepare(db, job) if prepare else None

                job.status = models.JobStatus.running
                job.started_at = datetime.now(timezone.utc)
                job.heartbeat_at = job.started_at
                job.attempts = (job.attempts or 0) + 1
                job.progress = start_progress
                db.commit()

                result = body(self, db, job, ctx)

                job.progress = 100
                job.heartbeat_at = datetime.now(timezone.utc)
                job.status = models.JobStatus.done
                job.error = None
                db.commit()
                committed = True

                if after_commit:
                    after_commit(result)

            except Exception as exc:
                db.rollback()
                if not committed and should_retry(exc, self.request.retries, self.max_retries):
                    job = db.get(models.Job, job_id)
                    if job:
                        # Reset to pending: the idempotency guard rejects `running`,
                        # so a retry that left the status alone would be a silent no-op.
                        job.status = models.JobStatus.pending
                        job.error = f"transient failure, retry {self.request.retries + 1}: {exc}"[:2000]
                        db.commit()
                    raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
                job = db.get(models.Job, job_id)
                if job:
                    job.status = models.JobStatus.failed
                    job.error = str(exc)[:2000]
                    rollback_owner(db, job, owner_kind, {owner_state})
                    if committed and after_commit_fail_owner:
                        rollback_owner(db, job, after_commit_fail_owner[0], {after_commit_fail_owner[1]})
                    db.commit()
                raise
            finally:
                db.close()

        del run.__wrapped__
        run.__signature__ = _TASK_SIGNATURE
        return run

    return decorate
