"""Shared helpers for Celery tasks.

is_transient_error / should_retry decide whether a failure is worth another
attempt. They stay pure functions (no Celery, no db session) so the decision is
unit-testable without a broker or a task context.

job_task is the one place that owns a Job's lifecycle: atomic claim, running
heartbeat, fenced done-stamp, transient-retry reset and failure stamp with owner
rollback. Task modules supply only the domain work as a body function.
"""
import functools
import inspect
import logging
import subprocess
import threading
from collections.abc import Collection
from datetime import datetime, timezone

import httpx
from sqlalchemy import exc as sa_exc

from api import models
from api.db import SessionLocal
from api.state import CUT_TRANSITIONS, JOB_IN_FLIGHT, REEL_TRANSITIONS, transition

_log = logging.getLogger(__name__)

_TRANSIENT_TYPES = (
    httpx.TransportError,      # connect/read/write timeouts, connection errors
    TimeoutError,
    subprocess.TimeoutExpired,
    ConnectionError,
    sa_exc.OperationalError,   # DB connection dropped / failover
    sa_exc.InterfaceError,
)
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}

# reap_stuck_jobs fails a running job whose heartbeat_at is older than
# STALE_MINUTES (5). Bodies make blocking calls far longer than that (a 360 s LLM
# call, a 10+ min render), so job_task keeps heartbeat_at fresh from a background
# thread instead of relying on every body to call heartbeat() often enough.
HEARTBEAT_INTERVAL_S = 30


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def heartbeat(db, job, progress: int) -> None:
    """Record progress and prove the worker is alive.

    reap_stuck_jobs fails any running job whose heartbeat_at goes stale for
    more than STALE_MINUTES. job_task also beats from a background thread, so
    this is mainly for progress reporting.
    """
    job.progress = progress
    job.heartbeat_at = _now()
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
    if row is None:
        return
    # transition() leaves a plain str in .status until the next expire/commit.
    if getattr(row.status, "value", row.status) not in states:
        return
    try:
        transition(row, "failed", _OWNER_TRANSITIONS[kind])
    except ValueError:
        pass


def _error_text(exc: BaseException) -> str:
    # Postgres rejects NUL in text columns; that would fail the very commit that records the failure.
    return str(exc).replace("\x00", "")[:2000]


def _advance(db, job_id, from_status, values: dict) -> bool:
    """Compare-and-set the Job's status; False when the row is no longer in from_status.

    Every lifecycle transition goes through this, so a worker that lost the job
    (reaped, or claimed by a sibling) never overwrites the winner's state.
    """
    updated = (
        db.query(models.Job)
        .filter(models.Job.id == job_id, models.Job.status == from_status)
        .update(values, synchronize_session=False)
    )
    return updated != 0


def _heartbeat_loop(job_id: int, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_INTERVAL_S):
        try:
            db = SessionLocal()
            try:
                _advance(db, job_id, models.JobStatus.running, {"heartbeat_at": _now()})
                db.commit()
            finally:
                db.close()
        except Exception:
            _log.warning("heartbeat write failed for job %s", job_id, exc_info=True)


_TASK_SIGNATURE = inspect.Signature([
    inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("job_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
])


def job_task(
    job_type: str,
    *,
    prepare=None,
    after_commit=None,
    after_commit_failed=None,
    start_progress: int = 5,
):
    """Wrap a task body in the Job lifecycle. Apply beneath ``@celery_app.task(bind=True, ...)``.

    The body is ``body(self, db, job, ctx) -> result``. It does the domain work,
    calls ``heartbeat()`` at milestones, and raises to fail. It must NOT commit
    after its last domain mutation: the done-stamp commit lands those mutations
    atomically with ``status = done``. (Persisting an irreversible external side
    effect the moment it happens is the one reason to commit early.)

    Order of events:
      1. ``job is None`` or ``status != pending`` -> return. ``done``/``running`` are a
         redelivery no-op; ``failed`` is terminal (every retry the operator triggers
         creates a new Job).
      2. Atomic claim: ``UPDATE ... WHERE status = 'pending'`` -> ``running``. Losing the
         race returns without running the body. A background thread then refreshes
         ``heartbeat_at`` until the run ends.
      3. ``prepare(db, job) -> ctx`` (optional) — load rows, null-guard, budget checks.
         A failure here fails the job but never bumps ``attempts`` or sets ``started_at``.
      4. ``attempts`` bump and ``started_at``, then the body.
      5. Fenced done-stamp: ``UPDATE ... WHERE status = 'running'``. If the reaper already
         failed the job, the body's uncommitted mutations are rolled back instead.
      6. ``after_commit(result)`` (optional) — work that must only happen once the job is
         durably done, e.g. enqueueing the next job. It gets the body's return value.

    Failure: roll back, then either reset to ``pending`` and ``self.retry()`` (transient
    error with retries left; ``attempts`` is NOT bumped) or stamp ``failed`` and roll the
    owner back per ``JOB_IN_FLIGHT[job_type]``. Every write is a compare-and-set, so a
    worker that lost the job never touches it or its owner. A failure in ``after_commit``
    never retries (the body already ran); the job is flipped ``done -> failed`` and
    ``after_commit_failed(db, job, result)`` runs to clean up what the body left behind.
    Errors while recording a failure are logged and never mask the original exception.
    A ``BaseException`` (SystemExit, KeyboardInterrupt) releases the job back to
    ``pending`` so the redelivered message is not rejected by step 1.

    The wrapper's signature is forced to ``(self, job_id)``: functools.wraps would
    expose the body's signature to Celery, and ``.delay(job_id)`` then raises TypeError.
    """
    owner_kind, owner_state = JOB_IN_FLIGHT[job_type]

    def decorate(body):
        @functools.wraps(body)
        def run(self, job_id):
            # expire_on_commit=False: with the default, reading reel.id after each commit
            # opens a new transaction that stays idle-in-transaction through every long
            # LLM / ffmpeg / upload call. Rows are owned by this run; done-stamp fencing
            # (step 5) protects against a stale view.
            db = SessionLocal(expire_on_commit=False)
            owned = committed = False
            result = None
            stop = threading.Event()
            beat = None
            try:
                job = db.get(models.Job, job_id)
                if job is None or job.status != models.JobStatus.pending:
                    return

                if not _advance(db, job_id, models.JobStatus.pending, {
                    "status": models.JobStatus.running, "heartbeat_at": _now(),
                }):
                    db.rollback()
                    return
                job.status = models.JobStatus.running
                job.heartbeat_at = _now()
                db.commit()
                owned = True

                beat = threading.Thread(
                    target=_heartbeat_loop, args=(job_id, stop), name=f"heartbeat-job-{job_id}", daemon=True,
                )
                beat.start()

                ctx = prepare(db, job) if prepare else None

                job.started_at = _now()
                job.attempts = (job.attempts or 0) + 1
                job.progress = start_progress
                db.commit()

                result = body(self, db, job, ctx)

                done_at = _now()
                if not _advance(db, job_id, models.JobStatus.running, {
                    "status": models.JobStatus.done, "progress": 100,
                    "heartbeat_at": done_at, "error": None,
                }):
                    # Reaped (or superseded) mid-run: the owner was already rolled back.
                    db.rollback()
                    _log.warning("job %s (%s) was no longer running at completion; result discarded",
                                 job_id, job_type)
                    return
                job.status = models.JobStatus.done
                job.progress = 100
                job.heartbeat_at = done_at
                job.error = None
                db.commit()
                committed = True

                if after_commit:
                    after_commit(result)

            except Exception as exc:
                retry = False
                try:
                    db.rollback()
                    message = _error_text(exc)
                    if not owned:
                        # Never claimed: nothing of ours to stamp. Only a transient error is worth a retry.
                        retry = should_retry(exc, self.request.retries, self.max_retries)
                    elif not committed and should_retry(exc, self.request.retries, self.max_retries):
                        retry = _advance(db, job_id, models.JobStatus.running, {
                            "status": models.JobStatus.pending,
                            "error": f"transient failure, retry {self.request.retries + 1}: {message}"[:2000],
                        })
                        db.commit()
                    else:
                        was = models.JobStatus.done if committed else models.JobStatus.running
                        if _advance(db, job_id, was, {"status": models.JobStatus.failed, "error": message}):
                            failed_job = db.get(models.Job, job_id)
                            failed_job.status = models.JobStatus.failed   # keep the ORM object coherent
                            failed_job.error = message
                            rollback_owner(db, failed_job, owner_kind, {owner_state})
                            if committed and after_commit_failed:
                                after_commit_failed(db, failed_job, result)
                        db.commit()
                except Exception:
                    # Never let a bookkeeping failure replace the real error.
                    _log.exception("could not record failure of job %s (%s)", job_id, job_type)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                if retry:
                    # Reset to pending above: the guard rejects `running`, so a retry that
                    # left the status alone would be a silent no-op.
                    raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
                raise

            except BaseException:
                # SystemExit / KeyboardInterrupt (worker cold shutdown): the message will be
                # redelivered, so hand the job back rather than leaving it `running`.
                if owned and not committed:
                    try:
                        db.rollback()
                        _advance(db, job_id, models.JobStatus.running, {"status": models.JobStatus.pending})
                        db.commit()
                    except Exception:
                        _log.exception("could not release job %s on shutdown", job_id)
                raise
            finally:
                stop.set()
                if beat is not None:
                    beat.join(timeout=5)
                db.close()

        del run.__wrapped__
        run.__signature__ = _TASK_SIGNATURE
        run.job_type = job_type   # exposed so tests can pin each task's wiring
        return run

    return decorate
