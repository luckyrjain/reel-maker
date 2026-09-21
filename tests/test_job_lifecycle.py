"""Tests for worker/tasks/common.py::job_task — the shared Job lifecycle.

Runs dummy tasks against an in-memory SQLite database (StaticPool, so every
session the decorator opens sees the same data). Domain behaviour lives in the
per-task suites; this file owns the claim / stamp / retry / failure / owner
rollback contract once, for every task.
"""
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery import Celery
from celery.exceptions import Retry
from sqlalchemy import create_engine
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.state import JOB_IN_FLIGHT
from worker.tasks.common import job_task

app = Celery("lifecycle_tests", broker="memory://", backend="cache+memory://")
app.conf.update(task_always_eager=True, task_eager_propagates=True)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False)
    with patch("worker.tasks.common.SessionLocal", session_factory):
        yield session_factory


def _make(factory, job_type=models.JobType.generate, *, reel_status=models.ReelStatus.generating,
          cut_status=models.CutStatus.rendering, status=models.JobStatus.pending):
    db = factory()
    reel = models.Reel(context="ctx", status=reel_status)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=cut_status)
    db.add(cut)
    db.flush()
    job = models.Job(type=job_type, reel_id=reel.id, cut_id=cut.id, status=status)
    db.add(job)
    db.commit()
    ids = (job.id, reel.id, cut.id)
    db.close()
    return ids


def _read(factory, job_id, reel_id, cut_id):
    db = factory()
    out = (db.get(models.Job, job_id), db.get(models.Reel, reel_id), db.get(models.Cut, cut_id))
    return out


def _set_job(factory, job_id, **values):
    """Simulate another writer (the reaper, a sibling worker) changing the Job row."""
    db = factory()
    for key, value in values.items():
        setattr(db.get(models.Job, job_id), key, value)
    db.commit()
    db.close()


def _task(name, body, job_type="generate", max_retries=2, **kwargs):
    # Celery builds a signature header from the function name, so lambdas must be renamed.
    body.__name__ = body.__qualname__ = name.replace(".", "_")
    return app.task(bind=True, max_retries=max_retries, name=name)(job_task(job_type, **kwargs)(body))


def _raises(exc):
    def body(self, db, job, ctx):
        raise exc
    return body


# ── guard and atomic claim ────────────────────────────────────────────────────

def test_missing_job_is_a_no_op(factory):
    ran = []
    task = _task("t.missing", lambda self, db, job, ctx: ran.append(1))
    assert task(999) is None
    assert ran == []


@pytest.mark.parametrize("status", [models.JobStatus.done, models.JobStatus.running, models.JobStatus.failed])
def test_only_a_pending_job_runs(factory, status):
    """done/running are redelivery no-ops; failed is terminal — a late redelivery of a job
    the reaper already failed must not run (for publish that would post the video)."""
    job_id, *_ = _make(factory, status=status)
    ran = []
    task = _task(f"t.guard.{status.value}", lambda self, db, job, ctx: ran.append(1))
    task(job_id)
    assert ran == []


def test_losing_the_claim_race_does_not_run_the_body(factory):
    """Two deliveries both read `pending`; only the one whose UPDATE matches runs."""
    job_id, reel_id, cut_id = _make(factory)
    ran = []
    task = _task("t.race", lambda self, db, job, ctx: ran.append(1))
    with patch("worker.tasks.common._advance", return_value=False):
        task(job_id)
    assert ran == []
    job, *_ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.pending, "the loser must not stamp the job"


def test_sessions_do_not_expire_on_commit(factory):
    """expire_on_commit=True would leave a transaction idle across every long external call."""
    job_id, *_ = _make(factory)
    task = _task("t.expire", lambda self, db, job, ctx: None)
    with patch("worker.tasks.common.SessionLocal", wraps=factory) as session_local:
        task(job_id)
    session_local.assert_any_call(expire_on_commit=False)


# ── success path ──────────────────────────────────────────────────────────────

def test_success_stamps_running_then_done_and_commits_body_mutations_atomically(factory):
    job_id, reel_id, cut_id = _make(factory)
    _set_job(factory, job_id, error="stale error from a retried attempt")
    seen = {}

    def body(self, db, job, ctx):
        seen["status"] = job.status
        seen["attempts"] = job.attempts
        seen["started_at"] = job.started_at
        # Left uncommitted on purpose: the done-stamp commit must carry it.
        db.get(models.Reel, reel_id).status = models.ReelStatus.guide_ready

    _task("t.success", body)(job_id)

    assert seen["status"] == models.JobStatus.running
    assert seen["attempts"] == 1
    assert seen["started_at"] is not None
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.done
    assert job.progress == 100
    assert job.error is None, "a retried-then-successful job must not leave an error in the UI"
    assert job.heartbeat_at is not None
    assert reel.status == models.ReelStatus.guide_ready


def test_start_progress_is_configurable(factory):
    job_id, *_ = _make(factory)
    seen = {}
    _task("t.progress", lambda self, db, job, ctx: seen.update(p=job.progress), start_progress=10)(job_id)
    assert seen["p"] == 10


def test_prepare_result_reaches_the_body_as_ctx(factory):
    job_id, *_ = _make(factory)
    seen = {}
    _task("t.ctx", lambda self, db, job, ctx: seen.update(ctx=ctx),
          prepare=lambda db, job: {"loaded": job.reel_id})(job_id)
    assert seen["ctx"]["loaded"] is not None


def test_done_stamp_is_fenced_when_the_reaper_already_failed_the_job(factory):
    """A live worker the reaper gave up on must not commit its result or resurrect the job."""
    job_id, reel_id, cut_id = _make(factory)
    after = []

    def body(self, db, job, ctx):
        _set_job(factory, job_id, status=models.JobStatus.failed, error="reaped")   # the reaper
        db.get(models.Reel, reel_id).status = models.ReelStatus.guide_ready

    _task("t.fenced", body, after_commit=after.append)(job_id)

    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed
    assert job.error == "reaped"
    assert reel.status == models.ReelStatus.generating, "the body's mutations must be rolled back"
    assert after == []


def test_heartbeat_thread_keeps_heartbeat_fresh_during_a_long_body(tmp_path):
    """Bodies block for minutes (LLM call, ffmpeg); the reaper only sees heartbeat_at."""
    engine = create_engine(f"sqlite:///{tmp_path}/hb.db")
    models.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False)
    job_id, reel_id, cut_id = _make(session_factory)
    seen = {}

    def body(self, db, job, ctx):
        def heartbeat_at():
            other = session_factory()
            try:
                return other.get(models.Job, job_id).heartbeat_at
            finally:
                other.close()
        seen["before"] = heartbeat_at()
        time.sleep(0.6)
        seen["after"] = heartbeat_at()

    with (
        patch("worker.tasks.common.SessionLocal", session_factory),
        patch("worker.tasks.common.HEARTBEAT_INTERVAL_S", 0.1),
    ):
        _task("t.beat", body)(job_id)

    assert seen["after"] > seen["before"]


# ── prepare failures ──────────────────────────────────────────────────────────

def test_prepare_failure_fails_the_job_without_bumping_attempts_or_starting(factory):
    job_id, reel_id, cut_id = _make(factory)

    def prepare(db, job):
        raise ValueError("Reel 1 no longer exists")

    task = _task("t.prepare", lambda self, db, job, ctx: None, prepare=prepare)
    with patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(ValueError, match="no longer exists"):
            task(job_id)

    retry.assert_not_called()
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed
    assert job.attempts == 0
    assert job.started_at is None
    assert "no longer exists" in job.error
    assert reel.status == models.ReelStatus.failed  # generate job owns "generating"


# ── retry / failure ───────────────────────────────────────────────────────────

def test_transient_failure_resets_to_pending_and_retries_without_bumping_attempts(factory):
    job_id, reel_id, cut_id = _make(factory)
    task = _task("t.transient", _raises(httpx.ConnectTimeout("down")))

    with patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(Retry):
            task(job_id)

    retry.assert_called_once()
    assert retry.call_args.kwargs["countdown"] == 30
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.pending
    assert job.attempts == 1, "entry already incremented attempts; the retry branch must not"
    assert job.error.startswith("transient failure, retry 1")
    assert reel.status == models.ReelStatus.generating, "a retry must not roll the owner back"


def test_a_retried_job_can_be_claimed_again(factory):
    """The reset to pending is what lets the redelivered message pass the guard."""
    job_id, reel_id, cut_id = _make(factory)
    calls = []

    def flaky(self, db, job, ctx):
        calls.append(job.attempts)
        if len(calls) == 1:
            raise ConnectionError("blip")

    task = _task("t.flaky", flaky)
    with patch.object(task, "retry", side_effect=Retry()):
        with pytest.raises(Retry):
            task(job_id)
    task(job_id)   # the redelivered message

    job, *_ = _read(factory, job_id, reel_id, cut_id)
    assert calls == [1, 2]
    assert job.status == models.JobStatus.done


def test_retry_backoff_doubles_with_each_retry(factory):
    job_id, *_ = _make(factory)
    task = _task("t.backoff", _raises(ConnectionError("x")))
    task.push_request(retries=1)
    try:
        with patch.object(task, "retry", side_effect=Retry()) as retry:
            with pytest.raises(Retry):
                task(job_id)
    finally:
        task.pop_request()
    assert retry.call_args.kwargs["countdown"] == 60


def test_transient_failure_with_no_retries_left_fails(factory):
    job_id, reel_id, cut_id = _make(factory)
    task = _task("t.exhausted", _raises(ConnectionError("x")))
    task.push_request(retries=2)
    try:
        with patch.object(task, "retry", side_effect=Retry()) as retry:
            with pytest.raises(ConnectionError):
                task(job_id)
    finally:
        task.pop_request()
    retry.assert_not_called()
    job, *_ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed


def test_max_retries_zero_never_retries(factory):
    job_id, *_ = _make(factory)
    task = _task("t.noretry", _raises(ConnectionError("x")), max_retries=0)
    with patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(ConnectionError):
            task(job_id)
    retry.assert_not_called()


def test_deterministic_failure_fails_once_and_truncates_the_error(factory):
    job_id, reel_id, cut_id = _make(factory)
    task = _task("t.deterministic", _raises(ValueError("x" * 5000)))
    with patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(ValueError):
            task(job_id)
    retry.assert_not_called()
    job, *_ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed
    assert len(job.error) == 2000


def test_nul_bytes_are_stripped_from_the_recorded_error(factory):
    """Postgres rejects NUL in text; that would fail the commit that records the failure."""
    job_id, reel_id, cut_id = _make(factory)
    with pytest.raises(ValueError):
        _task("t.nul", _raises(ValueError("bad\x00byte")))(job_id)
    job, *_ = _read(factory, job_id, reel_id, cut_id)
    assert job.error == "badbyte"


def test_a_failure_after_the_reaper_took_the_job_leaves_it_and_its_owner_alone(factory):
    job_id, reel_id, cut_id = _make(factory)

    def body(self, db, job, ctx):
        _set_job(factory, job_id, status=models.JobStatus.failed, error="reaped")
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _task("t.reaped_fail", body)(job_id)
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.error == "reaped"
    assert reel.status == models.ReelStatus.generating


def test_a_bookkeeping_error_never_masks_the_original_exception(factory):
    job_id, *_ = _make(factory)
    task = _task("t.masked", _raises(ValueError("the real error")))
    with patch("worker.tasks.common.rollback_owner", side_effect=RuntimeError("bookkeeping blew up")):
        with pytest.raises(ValueError, match="the real error"):
            task(job_id)


# ── database errors ───────────────────────────────────────────────────────────

def test_db_error_before_the_claim_retries_without_stamping_a_job_that_never_ran(factory):
    job_id, reel_id, cut_id = _make(factory)
    ran = []
    task = _task("t.db_down", lambda self, db, job, ctx: ran.append(1))
    with (
        patch("worker.tasks.common._advance", side_effect=sa_exc.OperationalError("SELECT", {}, Exception("gone"))),
        patch.object(task, "retry", side_effect=Retry()) as retry,
    ):
        with pytest.raises(Retry):
            task(job_id)
    retry.assert_called_once()
    assert ran == []
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.pending
    assert reel.status == models.ReelStatus.generating


def test_non_transient_error_before_the_claim_does_not_touch_the_job(factory):
    job_id, reel_id, cut_id = _make(factory)
    task = _task("t.preclaim", lambda self, db, job, ctx: None)
    with patch("worker.tasks.common._advance", side_effect=ValueError("weird")):
        with pytest.raises(ValueError, match="weird"):
            task(job_id)
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.pending
    assert reel.status == models.ReelStatus.generating


# ── worker shutdown ───────────────────────────────────────────────────────────

def test_shutdown_releases_the_job_so_the_redelivered_message_can_run(factory):
    job_id, reel_id, cut_id = _make(factory)
    task = _task("t.shutdown", _raises(SystemExit(1)))
    with pytest.raises(SystemExit):
        task(job_id)
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.pending
    assert reel.status == models.ReelStatus.generating, "a shutdown is not a failure"

    ran = []
    _task("t.shutdown2", lambda self, db, job, ctx: ran.append(1))(job_id)
    assert ran == [1]


# ── owner rollback, per job type ──────────────────────────────────────────────

@pytest.mark.parametrize("job_type, kind, in_flight", [
    ("enrich", "reel", models.ReelStatus.enriching),
    ("generate", "reel", models.ReelStatus.generating),
    ("render", "cut", models.CutStatus.rendering),
    ("publish", "cut", models.CutStatus.publishing),
])
def test_failure_rolls_back_only_the_owner_state_of_its_own_job_type(factory, job_type, kind, in_flight):
    kwargs = {"reel_status": in_flight} if kind == "reel" else {"cut_status": in_flight}
    if kind == "cut":
        kwargs["reel_status"] = models.ReelStatus.guide_ready
    job_id, reel_id, cut_id = _make(factory, models.JobType[job_type], **kwargs)
    task = _task(f"t.owner.{job_type}", _raises(ValueError("boom")), job_type=job_type)
    with pytest.raises(ValueError):
        task(job_id)
    _, reel, cut = _read(factory, job_id, reel_id, cut_id)
    if kind == "reel":
        assert reel.status == models.ReelStatus.failed
    else:
        assert cut.status == models.CutStatus.failed
        assert reel.status == models.ReelStatus.guide_ready, "a cut job must not touch a finished reel"


def test_render_failure_leaves_a_publishing_cut_alone(factory):
    """A stale render job failing must not flip a cut that has since moved on."""
    job_id, reel_id, cut_id = _make(
        factory, models.JobType.render,
        reel_status=models.ReelStatus.guide_ready, cut_status=models.CutStatus.publishing,
    )
    task = _task("t.stale", _raises(ValueError("boom")), job_type="render")
    with pytest.raises(ValueError):
        task(job_id)
    _, _, cut = _read(factory, job_id, reel_id, cut_id)
    assert cut.status == models.CutStatus.publishing


def test_owner_rollback_ignores_a_reel_in_guide_ready(factory):
    job_id, reel_id, cut_id = _make(factory, reel_status=models.ReelStatus.guide_ready)
    task = _task("t.guide_ready", _raises(ValueError("boom")))
    with pytest.raises(ValueError):
        task(job_id)
    _, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert reel.status == models.ReelStatus.guide_ready


def test_rollback_owner_survives_a_second_call_on_the_same_row(factory):
    """transition() leaves a plain str in .status until the next expire."""
    from worker.tasks.common import rollback_owner
    job_id, reel_id, cut_id = _make(factory, reel_status=models.ReelStatus.enriching)
    db = factory()
    job = db.get(models.Job, job_id)
    rollback_owner(db, job, "reel", {"enriching"})
    rollback_owner(db, job, "reel", {"enriching", "generating"})   # would raise AttributeError on str.value
    assert db.get(models.Reel, reel_id).status == "failed"


# ── after_commit ──────────────────────────────────────────────────────────────

def test_after_commit_runs_after_the_done_commit_with_the_body_result(factory):
    job_id, reel_id, cut_id = _make(factory)
    observed = {}

    def after_commit(result):
        job, _, _ = _read(factory, job_id, reel_id, cut_id)
        observed["status"] = job.status
        observed["result"] = result

    _task("t.after", lambda self, db, job, ctx: 42, after_commit=after_commit)(job_id)
    assert observed == {"status": models.JobStatus.done, "result": 42}


def test_after_commit_does_not_run_when_the_body_fails(factory):
    job_id, *_ = _make(factory)
    calls = []
    task = _task("t.after_skip", _raises(ValueError("boom")), after_commit=calls.append)
    with pytest.raises(ValueError):
        task(job_id)
    assert calls == []


def test_after_commit_failure_fails_the_job_runs_the_cleanup_hook_and_never_retries(factory):
    job_id, reel_id, cut_id = _make(factory, models.JobType.enrich, reel_status=models.ReelStatus.generating)
    cleaned = []

    def after_commit(result):
        raise ConnectionError("broker down")   # transient, but the body already ran

    def after_commit_failed(db, job, result):
        cleaned.append((job.id, result))
        from worker.tasks.common import rollback_owner
        rollback_owner(db, job, "reel", {"generating"})

    task = _task("t.after_fail", lambda self, db, job, ctx: 7, job_type="enrich",
                 after_commit=after_commit, after_commit_failed=after_commit_failed)
    with patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(ConnectionError):
            task(job_id)

    retry.assert_not_called()
    assert cleaned == [(job_id, 7)]
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed
    assert reel.status == models.ReelStatus.failed


def test_a_failing_cleanup_hook_does_not_mask_the_after_commit_error(factory):
    job_id, *_ = _make(factory, models.JobType.enrich)

    def hook(db, job, result):
        raise RuntimeError("cleanup blew up")

    task = _task("t.hook_fail", lambda self, db, job, ctx: 1, job_type="enrich",
                 after_commit=MagicMock(side_effect=ConnectionError("broker down")),
                 after_commit_failed=hook)
    with pytest.raises(ConnectionError, match="broker down"):
        task(job_id)


def test_a_body_failure_does_not_run_the_cleanup_hook(factory):
    job_id, *_ = _make(factory, models.JobType.enrich)
    hook = MagicMock()
    task = _task("t.body_fail", _raises(ValueError("boom")), job_type="enrich", after_commit_failed=hook)
    with pytest.raises(ValueError):
        task(job_id)
    hook.assert_not_called()


# ── Celery integration ────────────────────────────────────────────────────────

def test_delay_and_apply_async_accept_a_single_job_id(factory):
    """Regression: functools.wraps exposes the body's (db, job, ctx) signature to
    Celery, and .delay(job_id) then raises TypeError. Direct calls hide it."""
    job_id, *_ = _make(factory)
    task = _task("t.delay", lambda self, db, job, ctx: None)
    task.delay(job_id).get()

    job_id2, *_ = _make(factory)
    task.apply_async(args=(job_id2,)).get()

    db = factory()
    assert db.get(models.Job, job_id).status == models.JobStatus.done
    assert db.get(models.Job, job_id2).status == models.JobStatus.done


def test_every_real_task_is_wired_with_the_right_job_type_and_retry_budget():
    """A copy-pasted job type silently rolls back the wrong owner (or none) on failure."""
    from worker.celery_app import celery_app
    celery_app.loader.import_default_modules()
    expected = {
        "worker.tasks.enrich_context.enrich_context": ("enrich", 0),
        "worker.tasks.generate.generate_guide": ("generate", 2),
        "worker.tasks.render.render_cut": ("render", 2),
        # publish is not idempotent on the platform side: a retry after a transient error that
        # arrived post-upload would post twice.
        "worker.tasks.publish.publish_cut": ("publish", 0),
    }
    actual = {
        name: (celery_app.tasks[name].run.job_type, celery_app.tasks[name].max_retries)
        for name in expected
    }
    assert actual == expected


def test_every_beat_task_routes_to_a_queue_a_documented_worker_consumes():
    """reap_stuck_jobs had no route, so beat sent it to the default queue nobody consumes."""
    from worker.celery_app import celery_app
    consumed = {"generation", "rendering"}
    for entry in celery_app.conf.beat_schedule.values():
        route = celery_app.conf.task_routes.get(entry["task"])
        assert route and route["queue"] in consumed, f"{entry['task']} is not routed to a consumed queue"


# ── state table ───────────────────────────────────────────────────────────────

def test_in_flight_table_covers_every_job_type_and_never_names_a_finished_state():
    assert set(JOB_IN_FLIGHT) == {t.value for t in models.JobType}
    for _, state in JOB_IN_FLIGHT.values():
        assert state not in ("guide_ready", "in_review")
