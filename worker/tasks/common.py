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
from celery.exceptions import Reject, SoftTimeLimitExceeded
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
# kills the worker process after a grace period, which is the only thing that frees a hung slot.
# This is best-effort, not a guarantee: it's a race against _fail_interrupted's own DB write, and
# Celery escalates SIGTERM->SIGKILL, so a plain SIGKILL (or losing that race) skips this path
# entirely. The reaper remains the actual backstop, not a rarely-needed fallback.
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
# psycopg2 puts the offending row in the exception text ("DETAIL:  Failing row contains (...)",
# "Key (col)=(value) already exists"), which hide_parameters does not cover. DETAIL/CONTEXT are
# always the trailing part of the message, and libpq wraps a long "Failing row contains (...)" onto
# a second physical line -- redact to the end of the string, not just to the end of the first line,
# or the wrapped continuation (which can itself hold column values) leaks past a [^\n]* bound.
_DB_DETAIL = re.compile(r"(?ms)^[ \t]*(?:DETAIL|CONTEXT):.*")
_MAX_ERROR_INPUT = 20_000


def _error_text(exc: BaseException) -> str:
    """The exception text as safe to store in job.error (persisted and shown in the UI).

    Linear time on any input: it is capped before the regexes run, and neither backtracks (each
    redacts from a fixed marker to the end of its line). Never empty: the status templates show
    an error and a Retry button only when job.error is set.
    """
    try:
        text = str(exc)
    except Exception:
        text = f"<{type(exc).__name__}: message could not be rendered>"
    text = text[:_MAX_ERROR_INPUT]
    text = _PARAMETERS.sub("[parameters: <redacted>]", text)   # SQLAlchemy echoes bound values
    text = _DB_DETAIL.sub("<database detail redacted>", text)
    text = text.replace("\x00", "")                            # Postgres rejects NUL in text
    text = text.encode("utf-8", "replace").decode("utf-8")     # ...and lone surrogates (psycopg2)
    return text.strip()[:2000] or f"{type(exc).__name__} (no message)"


def _describe(exc: BaseException, max_runtime_s: float) -> str:
    """What to store in job.error: the scrubbed text, or a clear reason for a timeout."""
    if isinstance(exc, SoftTimeLimitExceeded):
        return (f"Exceeded its {max_runtime_s:g} s runtime limit (Celery soft time limit) and was "
                "stopped. The work is not resumed: retry it, or check what it was waiting on.")
    return _error_text(exc)


def _retry_budget(task, owned: bool) -> int:
    """How many retries this run may still make. A run that never claimed its job has done nothing
    yet, so it may retry a few times even when the task itself never retries (enrich, publish)."""
    return task.max_retries if owned else max(task.max_retries, PRECLAIM_MAX_RETRIES)


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


def _fail_job(db, job_id, from_status, message: str, owner_kind: str, owner_state: str) -> bool:
    """Compare-and-set the Job to failed and free its owner (no commit). False if the job had moved on.

    The one place a job is failed from a state we hold no claim on: a sibling that claimed it,
    or the reaper that already failed it, makes the CAS lose and leaves job and owner alone.
    """
    if not _advance(db, job_id, from_status, {"status": models.JobStatus.failed, "error": message[:2000]}):
        return False
    job = db.get(models.Job, job_id)
    job.status = models.JobStatus.failed   # mirror the CAS onto the loaded object
    job.error = message[:2000]
    rollback_owner(db, job, owner_kind, {owner_state})
    return True


def _settle_failure(self, db, job_id, exc, *, owned, committed, owner_kind, owner_state,
                    result, after_commit_failed, max_runtime_s) -> bool:
    """Record a failure. True when the caller should raise ``self.retry()``.

    Every write is a compare-and-set on Job.status, so a run that lost its job (reaped, or
    claimed by a sibling) leaves both the job and its owner alone.
    """
    db.rollback()
    db.expire_all()   # rollback() is a no-op when no transaction is open; never trust cached rows
    message = _describe(exc, max_runtime_s)
    retriable = should_retry(exc, self.request.retries, _retry_budget(self, owned))
    if not owned:
        if retriable:
            return True
        # Never claimed and never retried again: no message will come back for this job, so fail it
        # now instead of leaving it pending (and its owner in flight) for the reaper. If a sibling
        # did claim it, the CAS loses and it is left alone.
        _fail_job(db, job_id, models.JobStatus.pending, f"could not start: {message}", owner_kind, owner_state)
        db.commit()
        return False
    if not committed and retriable:
        ok = _advance(db, job_id, models.JobStatus.running, {
            "status": models.JobStatus.pending,
            "error": f"transient failure, retry {self.request.retries + 1}: {message}"[:2000],
        })
        db.commit()
        return ok              # only after the reset is durable
    if committed:
        # The body already ran and its owner state moved on; only the hook knows what to undo.
        if _fail_job_keep_owner(db, job_id, message) and after_commit_failed:
            after_commit_failed(db, db.get(models.Job, job_id), result)
    else:
        _fail_job(db, job_id, models.JobStatus.running, message, owner_kind, owner_state)
    db.commit()
    return False


def _fail_job_keep_owner(db, job_id, message: str) -> bool:
    """done -> failed after a failed after_commit; the hook owns cleanup of the owner."""
    if not _advance(db, job_id, models.JobStatus.done, {"status": models.JobStatus.failed, "error": message[:2000]}):
        return False
    job = db.get(models.Job, job_id)
    job.status = models.JobStatus.failed
    job.error = message[:2000]
    return True


def _finalize_or_reconnect(db, job_id: int, write) -> None:
    """Roll back, run ``write(session, job_id)``, commit. These are last-resort, one-shot failure
    recorders with no other net: if this doesn't land, the job is stuck until the reaper (hours).

    The connection `db` holds may itself be the thing that died — a server-side kill, or the
    database's idle_in_transaction_session_timeout, can land at exactly the moment this runs, since
    it is often called BECAUSE something just went wrong. A dead socket can't be rolled back into a
    clean state, so `db.rollback()` itself can raise. When that (or the write, or the commit) fails
    with a connection-level error, retry the whole sequence once on a brand-new session rather than
    silently losing the failure this call exists to record. Any other exception (a real bug in
    `write`) is not retried and propagates as-is.
    """
    def attempt(session):
        session.rollback()
        session.expire_all()
        write(session, job_id)
        session.commit()

    try:
        attempt(db)
    except (sa_exc.OperationalError, sa_exc.InterfaceError):
        fresh = SessionLocal()
        try:
            attempt(fresh)
        finally:
            fresh.close()


def _fail_interrupted(db, job_id, exc: BaseException, owner_kind: str, owner_state: str) -> None:
    """SystemExit / KeyboardInterrupt while the job was running (worker shut down or killed by a
    Celery hard time limit): fail it now. Do NOT hand it back to pending: Celery has already
    acked or dropped the message, so nothing would ever pick a pending job up again."""
    message = (f"Worker was shut down while the job was running ({type(exc).__name__}). "
               "The work was not finished; retry it.")
    _finalize_or_reconnect(db, job_id, lambda s, jid: _fail_job(
        s, jid, models.JobStatus.running, message, owner_kind, owner_state))


def _fail_rejected_retry(db, job_id, exc, owner_kind, owner_state) -> None:
    """The broker refused the retry message (Reject): it is dropped, so fail the job now
    rather than leaving it pending until the reaper's pending threshold."""
    message = f"could not schedule retry: {_error_text(exc)}"
    _finalize_or_reconnect(db, job_id, lambda s, jid: _fail_job(
        s, jid, models.JobStatus.pending, message, owner_kind, owner_state))


def fail_unenqueued(db, job, exc: BaseException) -> None:
    """A router created a Job but could not enqueue it: fail it and free its owner now.

    Otherwise the cut/reel sits in flight, refusing every retry with 409, until the reaper's
    pending threshold (hours) decides the message was lost.
    """
    kind, state = JOB_IN_FLIGHT[job.type.value]
    message = f"could not enqueue: {_error_text(exc)}"
    _finalize_or_reconnect(db, job.id, lambda s, jid: _fail_job(s, jid, models.JobStatus.pending, message, kind, state))


def job_task(
    job_type: str,
    *,
    prepare=None,
    after_commit=None,
    after_commit_failed=None,
    max_runtime_s: float,
    start_progress: int = 5,
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
    worker that lost the job never changes its status or its owner. ``started_at``, ``attempts``,
    ``job.meta`` and the ``start_progress`` write are plain; ``heartbeat()`` and the done-stamp write
    ``progress`` inside a compare-and-set. If the broker refuses the retry message the job is failed at
    once. A failure in ``after_commit`` never retries (the body already ran): the job is flipped
    ``done -> failed`` and ``after_commit_failed(db, job, result)`` alone cleans up what the body left
    behind, including the owner. A run that never claimed the job (e.g. a DB error at the guard) has
    done nothing, so a transient error is retried up to PRECLAIM_MAX_RETRIES even for a task with
    ``max_retries=0``; once that is used up, or for a non-transient error, the still-pending job is
    failed and its owner freed (no message will come back for it). Errors while recording a failure
    are logged and never mask the original exception. ``job.error`` is never empty, has database row
    detail and bound parameters redacted, and says so plainly when the cause was the soft time limit.

    ``max_runtime_s`` (required) is the longest a body may run. Pass the same number to
    ``time_limits()`` on ``@celery_app.task`` so Celery raises SoftTimeLimitExceeded in the body
    and, failing that, kills the worker process: stopping the heartbeat alone only lets the
    reaper fail the job record, it does not free a hung worker slot. A body that swallows
    ``Exception`` around its blocking calls also swallows the soft limit (do not: re-raise it);
    the hard limit then ends the run.

    A ``BaseException`` (SystemExit, KeyboardInterrupt) means the worker is shutting down or the hard
    time limit is killing the process. The job is FAILED at once and its owner freed. It is never handed
    back to ``pending``: Celery has already acked or dropped the message, so nothing would pick a pending
    job up again (the reaper would only notice hours later). A child killed with SIGKILL never gets here;
    its job stays ``running`` and the reaper fails it after STALE_MINUTES.

    ``del run.__wrapped__`` is what keeps ``.delay(job_id)`` working: functools.wraps would
    otherwise expose the body's signature to Celery, which rejects the call with TypeError.

    Adding a new job type takes several edits, not one: a JobType value, its ``JOB_IN_FLIGHT`` entry
    (api/state.py), a ``task_routes`` entry (worker/celery_app.py), ``**time_limits(...)`` on the
    ``@celery_app.task`` and a ``max_runtime_s``. Always set ``max_retries`` on the task: Celery's
    default of 3 would silently enable retries. ``run.job_type`` and ``run.max_runtime_s`` exist so
    tests can pin each task's wiring (a test fails for any registered task that lacks time limits).
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
                    try:
                        after_commit(result)
                    except Exception:
                        raise   # unchanged: the except Exception branch below runs the same cleanup
                    except BaseException as hook_exc:
                        # SystemExit/KeyboardInterrupt specifically: the job already completed
                        # (committed=True), so only after_commit_failed can undo what after_commit
                        # left half-done (e.g. an un-enqueued follow-up job). The except BaseException
                        # branch further down only acts when not committed, so without this a shutdown
                        # exactly here would skip cleanup entirely, falling back to the reaper's much
                        # slower sweep to notice at all.
                        try:
                            message = _describe(hook_exc, max_runtime_s)
                            if _fail_job_keep_owner(db, job_id, message) and after_commit_failed:
                                after_commit_failed(db, db.get(models.Job, job_id), result)
                            db.commit()
                        except Exception:
                            _log.exception("could not record after_commit failure for job %s", job_id)
                        raise

            except Exception as exc:
                retry = False
                try:
                    retry = _settle_failure(
                        self, db, job_id, exc, owned=owned, committed=committed,
                        owner_kind=owner_kind, owner_state=owner_state,
                        result=result, after_commit_failed=after_commit_failed,
                        max_runtime_s=max_runtime_s,
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
                            max_retries=_retry_budget(self, owned),
                        )
                    except Reject:
                        # Safe whether or not this run ever claimed the job: _fail_rejected_retry
                        # CASes off `pending`, which is the job's real status in both cases — owned
                        # jobs were just reset to pending above, and an unclaimed job never left it.
                        # The same "no message is coming back" reasoning _settle_failure applies to
                        # an unclaimed job with no retries left applies here too.
                        try:
                            _fail_rejected_retry(db, job_id, exc, owner_kind, owner_state)
                        except Exception:
                            _log.exception("could not fail job %s after a refused retry", job_id)
                        raise
                raise

            except BaseException as exc:
                # SystemExit / KeyboardInterrupt: the worker is shutting down, or a Celery hard time
                # limit is killing this process. Fail the job now (see _fail_interrupted).
                if owned and not committed:
                    try:
                        _fail_interrupted(db, job_id, exc, owner_kind, owner_state)
                    except Exception:
                        _log.exception("could not fail job %s on shutdown", job_id)
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
        run.max_runtime_s = max_runtime_s
        return run

    return decorate
