"""Shared helpers for Celery tasks.

is_transient_error / should_retry decide whether a failure is worth another
attempt. They stay pure functions (no Celery, no db session) so the decision is
unit-testable without a broker or a task context.

job_task is the one place that owns a Job's lifecycle: atomic claim, running
heartbeat, fenced done-stamp, transient-retry reset and failure stamp with owner
rollback. Task modules supply only the domain work as a body function.
"""
import functools
import logging
import re
import subprocess
import threading
import time
from collections.abc import Collection
from datetime import datetime, timezone

import httpx
from celery.exceptions import Reject
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

# The heartbeat thread proves the process is alive, not that the body is making progress. Each
# task therefore declares a max_runtime_s, used twice: the thread stops beating past it (so the
# reaper fails a wedged job's record), and the task registers it as a Celery soft_time_limit
# (see time_limits()), which raises SoftTimeLimitExceeded inside the body and, if that is ignored,
# kills the worker process after a grace period - the only thing that frees a hung slot.
TIME_LIMIT_GRACE_S = 120

# A DB error before the job was claimed means nothing has run yet, so retrying is always safe,
# even for tasks that must never retry automatically once they have started (enrich, publish).
PRECLAIM_MAX_RETRIES = 3


def time_limits(max_runtime_s: float) -> dict:
    """Celery task options that enforce max_runtime_s: pass to ``celery_app.task(**time_limits(n))``."""
    return {"soft_time_limit": max_runtime_s, "time_limit": max_runtime_s + TIME_LIMIT_GRACE_S}


class JobLost(RuntimeError):
    """This run no longer owns its Job (the reaper failed it, or a sibling took it)."""


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


def _advance(db, job_id, from_status, values: dict) -> bool:
    """Compare-and-set the Job's status; False when the row is no longer in from_status.

    Every status transition goes through this, so a worker that lost the job
    (reaped, or claimed by a sibling) never overwrites the winner's state.
    """
    updated = (
        db.query(models.Job)
        .filter(models.Job.id == job_id, models.Job.status == from_status)
        .update(values, synchronize_session=False)
    )
    return updated != 0


def heartbeat(db, job, progress: int) -> None:
    """Record progress, prove the worker is alive, and commit.

    Fenced: raises JobLost when the job is no longer running (the reaper gave up on it), which
    aborts a zombie body at its next milestone instead of letting it run on and write. Commits
    the body's pending mutations, so call it before the final mutations, never after.
    """
    now = _now()
    if not _advance(db, job.id, models.JobStatus.running,
                    {"progress": progress, "heartbeat_at": now}):
        db.rollback()
        raise JobLost(f"job {job.id} is no longer running")
    job.progress = progress
    job.heartbeat_at = now
    db.commit()


def lock_job(db, job) -> None:
    """Take the Job's row lock (no commit) before touching a reel/cut row in the same transaction.

    The reaper locks the job row first and then the owner; a body that locks the owner first and
    the job at its done-stamp can deadlock against it. Raises JobLost if the job is gone.
    """
    if not _advance(db, job.id, models.JobStatus.running, {"heartbeat_at": _now()}):
        db.rollback()
        raise JobLost(f"job {job.id} is no longer running")


_OWNER_MODELS = {"reel": models.Reel, "cut": models.Cut}
_OWNER_TRANSITIONS = {"reel": REEL_TRANSITIONS, "cut": CUT_TRANSITIONS}


def rollback_owner(db, job, kind: str, states: Collection[str]) -> None:
    """Fail the job's reel/cut when it is still in one of the given in-flight states.

    The row is re-read under a row lock (FOR UPDATE), so the state check and the write
    cannot race another writer, and the session's cached copy is never trusted. A row that
    already moved on (or was deleted) is left alone, and an invalid transition is swallowed:
    the failure handler must never mask the real error.
    """
    row_id = job.reel_id if kind == "reel" else job.cut_id
    if not row_id:
        return
    row = db.get(_OWNER_MODELS[kind], row_id)
    if row is None:
        return
    db.refresh(row, with_for_update=True)
    # transition() leaves a plain str in .status until the next expire/commit.
    if getattr(row.status, "value", row.status) not in states:
        return
    try:
        transition(row, "failed", _OWNER_TRANSITIONS[kind])
    except ValueError:
        pass


_PARAMETERS = re.compile(r"\[parameters:[^\n]*")
_MAX_ERROR_INPUT = 20_000


def _error_text(exc: BaseException) -> str:
    """The exception text as safe to store in job.error (persisted and shown in the UI).

    Linear time on any input: it is capped before the regex runs, and the regex has no
    backtracking (it redacts from "[parameters:" to the end of that line).
    """
    try:
        text = str(exc)
    except Exception:
        text = f"<{type(exc).__name__}: message could not be rendered>"
    text = text[:_MAX_ERROR_INPUT]
    text = _PARAMETERS.sub("[parameters: <redacted>]", text)   # SQLAlchemy echoes bound values
    text = text.replace("\x00", "")                            # Postgres rejects NUL in text
    text = text.encode("utf-8", "replace").decode("utf-8")     # ...and lone surrogates (psycopg2)
    return text[:2000]


def _heartbeat_loop(job_id: int, stop: threading.Event, max_runtime_s: float) -> None:
    started = time.monotonic()
    while not stop.wait(HEARTBEAT_INTERVAL_S):
        if time.monotonic() - started > max_runtime_s:
            _log.error("job %s ran longer than %s s; no longer beating so the reaper can fail it",
                       job_id, max_runtime_s)
            return
        try:
            db = SessionLocal()
            try:
                _advance(db, job_id, models.JobStatus.running, {"heartbeat_at": _now()})
                db.commit()
            finally:
                db.close()
        except Exception:
            _log.warning("heartbeat write failed for job %s", job_id, exc_info=True)


def _settle_failure(self, db, job_id, exc, *, owned, committed, owner_kind, owner_state,
                    result, after_commit_failed) -> bool:
    """Record a failure. True when the caller should raise ``self.retry()``.

    Every write is a compare-and-set on Job.status, so a run that lost its job (reaped, or
    claimed by a sibling) leaves both the job and its owner alone.
    """
    db.rollback()
    db.expire_all()   # rollback() is a no-op when no transaction is open; never trust cached rows
    message = _error_text(exc)
    if not owned:
        # Never claimed: nothing has run and there is nothing of ours to stamp, so a retry is safe
        # whatever the task's own budget.
        return should_retry(exc, self.request.retries, max(self.max_retries, PRECLAIM_MAX_RETRIES))
    retriable = should_retry(exc, self.request.retries, self.max_retries)
    if not committed and retriable:
        ok = _advance(db, job_id, models.JobStatus.running, {
            "status": models.JobStatus.pending,
            "error": f"transient failure, retry {self.request.retries + 1}: {message}"[:2000],
        })
        db.commit()
        return ok              # only after the reset is durable
    was = models.JobStatus.done if committed else models.JobStatus.running
    if _advance(db, job_id, was, {"status": models.JobStatus.failed, "error": message}):
        job = db.get(models.Job, job_id)
        job.status = models.JobStatus.failed   # mirror the CAS onto the loaded object
        job.error = message
        if committed:
            # The body already ran and its owner state moved on; only the hook knows what to undo.
            if after_commit_failed:
                after_commit_failed(db, job, result)
        else:
            rollback_owner(db, job, owner_kind, {owner_state})
    db.commit()
    return False


def _fail_rejected_retry(db, job_id, exc, owner_kind, owner_state) -> None:
    """The broker refused the retry message (Reject): it is dropped, so fail the job now
    rather than leaving it pending until the reaper's pending threshold."""
    db.rollback()
    db.expire_all()
    if _advance(db, job_id, models.JobStatus.pending, {
        "status": models.JobStatus.failed,
        "error": f"could not schedule retry: {_error_text(exc)}"[:2000],
    }):
        rollback_owner(db, db.get(models.Job, job_id), owner_kind, {owner_state})
    db.commit()


def fail_unenqueued(db, job, exc: BaseException) -> None:
    """A router created a Job but could not enqueue it: fail it and free its owner now.

    Otherwise the cut/reel sits in flight, refusing every retry with 409, until the reaper's
    pending threshold (hours) decides the message was lost.
    """
    kind, state = JOB_IN_FLIGHT[job.type.value]
    if _advance(db, job.id, models.JobStatus.pending, {
        "status": models.JobStatus.failed,
        "error": f"could not enqueue: {_error_text(exc)}"[:2000],
    }):
        rollback_owner(db, job, kind, {state})
    db.commit()


def job_task(
    job_type: str,
    *,
    prepare=None,
    after_commit=None,
    after_commit_failed=None,
    max_runtime_s: float,
    start_progress: int = 5,
    release_on_shutdown: bool = True,
):
    """Wrap a task body in the Job lifecycle. Apply beneath ``@celery_app.task(bind=True, ...)``.

    The body is ``body(self, db, job, ctx) -> result``. It does the domain work, calls
    ``heartbeat()`` at milestones, and raises to fail. ``heartbeat()`` COMMITS (and raises
    ``JobLost`` if the reaper already failed the job), so it must come before the body's
    last mutations: the done-stamp commit lands those atomically with ``status = done``.
    The one reason to commit early is an irreversible external side effect.

    Order of events:
      1. ``job is None`` or ``status != pending`` -> return. ``done``/``running`` are a
         redelivery no-op; ``failed`` is terminal (every retry the operator triggers
         creates a new Job).
      2. Atomic claim: ``UPDATE ... WHERE status = 'pending'`` -> ``running``. Losing the
         race returns without running the body. A background thread then refreshes
         ``heartbeat_at`` until the run ends or ``max_runtime_s`` passes.
      3. ``prepare(db, job) -> ctx`` (optional) — load rows, null-guard, budget checks.
         A failure here fails the job but never bumps ``attempts`` or sets ``started_at``.
      4. ``attempts`` bump and ``started_at``, then the body.
      5. Fenced done-stamp: ``UPDATE ... WHERE status = 'running'``. If the reaper already
         failed the job, the body's uncommitted mutations are rolled back instead.
      6. ``after_commit(result)`` (optional) — work that must only happen once the job is
         durably done, e.g. enqueueing the next job. It gets the body's return value.

    Failure: roll back, then either reset to ``pending`` and ``self.retry()`` (transient
    error with retries left; ``attempts`` is NOT bumped) or stamp ``failed`` and roll the
    owner back per ``JOB_IN_FLIGHT[job_type]``. Every STATUS write is a compare-and-set, so a
    worker that lost the job never changes its status or its owner. ``progress``, ``attempts``,
    ``started_at`` and ``job.meta`` are plain writes. If the broker refuses the retry message
    the job is failed at once. A failure in ``after_commit`` never retries (the body already
    ran): the job is flipped ``done -> failed`` and ``after_commit_failed(db, job, result)``
    alone cleans up what the body left behind, including the owner. If the run never claimed
    the job (e.g. a DB error at the guard), nothing has run, so a transient error is retried up
    to PRECLAIM_MAX_RETRIES even for a task with ``max_retries=0``. Errors while recording a
    failure are logged and never mask the original exception.

    ``max_runtime_s`` (required) is the longest a body may run. Pass the same number to
    ``time_limits()`` on ``@celery_app.task`` so Celery raises SoftTimeLimitExceeded in the body
    and, failing that, kills the worker process: stopping the heartbeat alone only lets the
    reaper fail the job record, it does not free a hung worker slot.

    A ``BaseException`` (SystemExit, KeyboardInterrupt) releases the job back to ``pending`` so the
    redelivered message passes step 1 - unless ``release_on_shutdown=False``, for tasks whose side
    effects are not safe to repeat (publish): the job then stays ``running`` and the reaper fails
    it. Note this path only runs where Python unwinds (solo/threads pools, Ctrl-C); a prefork
    child killed by SIGTERM/SIGKILL (the documented ``pkill`` restart) never reaches it, and the
    job is failed by the reaper after STALE_MINUTES instead.

    ``del run.__wrapped__`` is what keeps ``.delay(job_id)`` working: functools.wraps would
    otherwise expose the body's signature to Celery, which rejects the call with TypeError.

    Adding a new job type takes three edits, not one: a JobType value, its ``JOB_IN_FLIGHT``
    entry (api/state.py), and a ``max_runtime_s``. Always set ``max_retries`` on the task:
    Celery's default of 3 would silently enable retries. ``run.job_type``, ``run.max_runtime_s``
    and ``run.release_on_shutdown`` exist so tests can pin each task's wiring.
    """
    owner_kind, owner_state = JOB_IN_FLIGHT[job_type]

    def decorate(body):
        @functools.wraps(body)
        def run(self, job_id):
            # expire_on_commit=False: with the default, reading reel.id after each commit
            # opens a new transaction that stays idle-in-transaction through every long
            # LLM / ffmpeg / upload call. Rows are owned by this run; the done-stamp fence
            # protects against a stale view.
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

                thread = threading.Thread(
                    target=_heartbeat_loop, args=(job_id, stop, max_runtime_s),
                    name=f"heartbeat-job-{job_id}", daemon=True,
                )
                thread.start()
                beat = thread   # only once started: join() on an unstarted thread raises

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
                    retry = _settle_failure(
                        self, db, job_id, exc, owned=owned, committed=committed,
                        owner_kind=owner_kind, owner_state=owner_state,
                        result=result, after_commit_failed=after_commit_failed,
                    )
                except Exception:
                    # Never let a bookkeeping failure replace the real error.
                    _log.exception("could not record failure of job %s (%s)", job_id, job_type)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                if retry:
                    # The job was reset to pending above: the claim rejects `running`, so a retry
                    # that left the status alone would be a silent no-op.
                    try:
                        raise self.retry(
                            exc=exc, countdown=30 * 2 ** self.request.retries,
                            max_retries=self.max_retries if owned else max(self.max_retries, PRECLAIM_MAX_RETRIES),
                        )
                    except Reject:
                        try:
                            if owned:
                                _fail_rejected_retry(db, job_id, exc, owner_kind, owner_state)
                        except Exception:
                            _log.exception("could not fail job %s after a refused retry", job_id)
                        raise
                raise

            except BaseException:
                # SystemExit / KeyboardInterrupt (worker cold shutdown).
                if release_on_shutdown and owned and not committed:
                    try:
                        db.rollback()
                        _advance(db, job_id, models.JobStatus.running, {"status": models.JobStatus.pending})
                        db.commit()
                    except Exception:
                        _log.exception("could not release job %s on shutdown", job_id)
                raise
            finally:
                stop.set()
                try:
                    if beat is not None:
                        beat.join(timeout=5)
                finally:
                    db.close()

        del run.__wrapped__
        run.job_type = job_type   # exposed so tests can pin each task's wiring
        run.release_on_shutdown = release_on_shutdown
        run.max_runtime_s = max_runtime_s
        return run

    return decorate
