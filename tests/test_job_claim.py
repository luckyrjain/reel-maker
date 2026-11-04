"""Tests for ``worker.tasks.common.Claim`` -- the one immutable object that carries a run's fencing
identity (docs/specs/2026-11-job-claim-module-design.md, §6 "New tests").

Each docstring names the mutant (M-number in that spec's table) it kills. The plain helpers come from
``test_job_lifecycle`` (flat import, the same precedent ``test_r3_proposed.py`` follows); the fixtures are
defined here because the repo convention is one ``factory``/``file_factory`` per file (importing a fixture
by name makes ruff flag every test that takes it as a redefinition, F811).
"""
import dataclasses
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy
from celery.exceptions import Retry
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.tasks.common as common
from api import models
from api.state import JOB_IN_FLIGHT
from test_job_lifecycle import _bump_claim_token, _make, _raises, _read, _set_job, _task  # noqa: F401
from worker.tasks.common import Claim, JobLost, _claim_of, heartbeat, lock_job

_LONG_AGO = datetime(2000, 1, 1)   # SQLite hands datetimes back naive
_REPO_ROOT = Path(__file__).resolve().parent.parent
# A bare ``pytest`` from a worktree imports the editable install's tree (the main checkout), so every
# "mutation" and every run would silently test other code. Fail loudly instead.
assert Path(common.__file__).resolve().is_relative_to(_REPO_ROOT), (
    f"worker.tasks.common was imported from {common.__file__}, outside {_REPO_ROOT}: "
    "run `python -m pytest` from the repository root"
)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False)
    with patch("worker.tasks.common.SessionLocal", session_factory):
        yield session_factory


@pytest.fixture
def file_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/claim.db")
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


def test_these_tests_run_against_this_checkouts_common_module():
    assert Path(common.__file__).resolve().is_relative_to(_REPO_ROOT)


def _running_job(factory, token):
    """A `running` Job row at a distinctive token, as a run that already claimed it would leave it."""
    job_id, reel_id, cut_id = _make(factory, status=models.JobStatus.running)
    _set_job(factory, job_id, claim_token=token, heartbeat_at=_LONG_AGO)
    return job_id



def _heartbeat_at(factory, job_id):
    db = factory()
    try:
        return db.get(models.Job, job_id).heartbeat_at
    finally:
        db.close()


# ── 3. the unpinned fallback fences ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", ["heartbeat", "lock_job"])
def test_an_unpinned_job_uses_its_live_token_and_succeeds_while_it_is_current(factory, fn):
    """Control for M12: nothing bumps the token, so the call must succeed and really write."""
    job_id = _running_job(factory, token=7)
    db = factory()
    job = db.get(models.Job, job_id)
    assert not hasattr(job, "_claim"), "a Job loaded outside job_task carries no pinned claim"

    if fn == "heartbeat":
        heartbeat(db, job, 33)
    else:
        lock_job(db, job)
        db.commit()

    assert _heartbeat_at(factory, job_id) > _LONG_AGO
    db.close()


@pytest.mark.parametrize("fn", ["heartbeat", "lock_job"])
def test_an_unpinned_job_is_fenced_by_its_loaded_token(factory, fn):
    """Kills M12 (``for_loaded_job`` building token=0 or a constant, or the fallback branch yielding a
    token-less write): the token is 7, another session bumps it to 8, so the call must raise. Fresh
    session and loaded job per function, because JobLost rolls the session back and expires the object."""
    job_id = _running_job(factory, token=7)
    db = factory()
    job = db.get(models.Job, job_id)
    assert job.claim_token == 7
    assert _bump_claim_token(factory, job_id) == 8

    with pytest.raises(JobLost):
        (heartbeat(db, job, 33) if fn == "heartbeat" else lock_job(db, job))

    assert _heartbeat_at(factory, job_id) == _LONG_AGO, "the fenced call must not have written"
    db.close()


def test_for_loaded_job_equals_the_claim_a_real_run_captured(factory):
    """Non-tautological equality: the left side is built from the row, the right side by ``run()``."""
    job_id, reel_id, cut_id = _make(factory)
    seen = {}

    def body(self, db, job, ctx):
        seen["claim"] = job._claim

    _task("t.claim_equality", body)(job_id)

    db = factory()
    rebuilt = Claim.for_loaded_job(db.get(models.Job, job_id))
    db.close()
    kind, state = JOB_IN_FLIGHT["generate"]
    assert seen["claim"] == Claim(job_id, 1, kind, state)
    assert rebuilt == seen["claim"]


# ── 4. the value and its one fenced operation ─────────────────────────────────────────────────

def test_a_claim_is_frozen_and_hashable():
    claim = Claim(1, 2, "reel", "generating")
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.token = 3
    assert hash(claim) == hash(Claim(1, 2, "reel", "generating"))
    assert {claim, Claim(1, 2, "reel", "generating"), Claim(1, 3, "reel", "generating")} == {
        claim, Claim(1, 3, "reel", "generating")}
    assert not hasattr(claim, "__dict__"), "slots: no stray attributes can be attached"


def test_advance_writes_only_while_its_token_is_current(factory):
    job_id = _running_job(factory, token=3)
    claim = Claim(job_id, 3, "reel", "generating")

    db = factory()
    assert claim.advance(db, models.JobStatus.running, {"progress": 11}) is True
    db.commit()
    assert factory().get(models.Job, job_id).progress == 11

    _bump_claim_token(factory, job_id)          # a fresher claim lands
    db = factory()
    assert claim.advance(db, models.JobStatus.running, {"progress": 99}) is False
    db.commit()
    row = factory().get(models.Job, job_id)
    assert row.progress == 11, "a stale claim must leave the row untouched"
    assert row.claim_token == 4


def test_advance_still_requires_the_from_status(factory):
    job_id = _running_job(factory, token=3)
    claim = Claim(job_id, 3, "reel", "generating")
    db = factory()
    assert claim.advance(db, models.JobStatus.pending, {"progress": 50}) is False
    db.commit()
    assert factory().get(models.Job, job_id).progress != 50


def test_advance_resolves_the_module_level_advance_at_call_time(factory):
    """Tests patch ``common._advance`` (14 sites): ``Claim.advance`` must go through that global, with
    positional from_status/values and the token as a keyword -- the shape the existing spy relies on."""
    calls = []

    def spy(db, jid, frm, values, token=None):
        calls.append((jid, frm, values, token))
        return True

    with patch("worker.tasks.common._advance", spy):
        assert Claim(5, 9, "cut", "rendering").advance(None, models.JobStatus.running, {"progress": 1}) is True
    assert calls == [(5, models.JobStatus.running, {"progress": 1}, 9)]


def test_claim_of_returns_the_pin_without_reading_anything_else_from_the_job():
    """Kills M10/M11 on a plain object: every attribute that is not the pin raises when read."""
    pinned = Claim(4, 6, "reel", "generating")

    class Strict:
        _claim = pinned

        @property
        def claim_token(self):
            raise AssertionError("the mapped claim_token was read")

        @property
        def id(self):
            raise AssertionError("the mapped id was read")

        @property
        def type(self):
            raise AssertionError("the mapped type was read")

    assert _claim_of(Strict()) is pinned


# ── 5. the pin is honored by both functions ───────────────────────────────────────────────────

def _expire(db, how):
    db.query(models.Cut).all()               # opens a transaction (a bare rollback would expire nothing)
    if how == "rollback":
        db.rollback()
    else:
        db.expire_all()


@pytest.mark.parametrize("how", ["rollback", "expire_all"])
@pytest.mark.parametrize("fn", ["heartbeat", "lock_job"])
def test_both_functions_honor_the_pin_after_the_mapped_token_is_expired(factory, fn, how):
    """Kills M10 and M15 (either function reading the mapped column when pinned): the mapped
    ``job.claim_token`` is expired, the row is bumped, and the function is called with NO read or refresh
    of ``job`` in between (a refresh there would reload the old token and let a mapped-column mutant still
    raise JobLost)."""
    job_id, reel_id, cut_id = _make(factory)
    seen = {}

    def zombie_body(self, db, job, ctx):
        _expire(db, how)
        seen["resumed_token"] = _bump_claim_token(factory, job_id)
        (heartbeat(db, job, 50) if fn == "heartbeat" else lock_job(db, job))

    with pytest.raises(JobLost):
        _task(f"t.pin.{fn}.{how}", zombie_body)(job_id)

    job, *_ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.running
    assert job.claim_token == seen["resumed_token"]


def test_the_pin_survives_rollback_expire_and_refresh_as_the_same_object(factory):
    job_id, *_ = _make(factory)
    seen = {}

    def body(self, db, job, ctx):
        pinned = job._claim
        db.query(models.Cut).all()
        db.rollback()
        seen["after_rollback"] = job._claim is pinned
        db.expire_all()
        seen["after_expire_all"] = job._claim is pinned
        db.refresh(job)
        seen["after_refresh"] = job._claim is pinned
        seen["claim"] = pinned

    _task("t.pin_identity", body)(job_id)
    assert seen.pop("claim") == Claim(job_id, 1, *JOB_IN_FLIGHT["generate"])
    assert seen == {"after_rollback": True, "after_expire_all": True, "after_refresh": True}


def test_the_heartbeat_thread_receives_the_very_object_the_job_carries(factory):
    """The thread is created before the body runs, so the spy's record is complete inside the body.
    Selected by type: the thread's other positional arguments (stop event, max_runtime_s) are not Claims."""
    job_id, *_ = _make(factory)
    created = []
    real = threading.Thread
    seen = {}

    def spy(*a, **k):
        created.append(k)
        return real(*a, **k)

    def body(self, db, job, ctx):
        claim_args = [a for k in created for a in k.get("args", ()) if isinstance(a, Claim)]
        seen["n"] = len(claim_args)
        seen["same"] = bool(claim_args) and claim_args[0] is job._claim
        seen["named"] = [k.get("name") for k in created]

    with patch("worker.tasks.common.threading.Thread", spy):
        _task("t.thread_claim", body)(job_id)

    assert seen == {"n": 1, "same": True, "named": [f"heartbeat-job-{job_id}"]}


# ── 6. the heartbeat loop is fenced by the same claim ─────────────────────────────────────────

def test_the_heartbeat_loop_beats_while_its_claim_is_current_and_stops_once_it_is_not(file_factory):
    """Kills M7 (the loop's write token-less). Positive control first: while the claim is current
    heartbeat_at advances, so "stopped" below cannot be the loop simply not running. The "before" value is
    read only after the bump, which has committed by then, so an in-flight tick cannot move it later."""
    job_id, *_ = _make(file_factory)
    seen = {}

    def body(self, db, job, ctx):
        seen["t0"] = _heartbeat_at(file_factory, job_id)
        time.sleep(0.5)
        seen["t1"] = _heartbeat_at(file_factory, job_id)
        _bump_claim_token(file_factory, job_id)
        seen["before"] = _heartbeat_at(file_factory, job_id)
        time.sleep(0.5)
        seen["after"] = _heartbeat_at(file_factory, job_id)

    with patch("worker.tasks.common.SessionLocal", file_factory), \
            patch("worker.tasks.common.HEARTBEAT_INTERVAL_S", 0.1):
        _task("t.beat_claim", body)(job_id)

    assert seen["t1"] > seen["t0"], "the loop must beat while its claim is current"
    assert seen["after"] == seen["before"], "a superseded loop must not advance the resumed run's row"


# ── 9. the two deliberate SQL deltas in heartbeat()/lock_job() ────────────────────────────────

class _Trace:
    def __init__(self, engine):
        self.engine = engine
        self.statements: list[str] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._on)

    @property
    def verbs(self):
        return [s.lstrip().split()[0].upper() for s in self.statements]


def _expire_for_real(db, job, how):
    _expire(db, how)
    assert "id" not in sqlalchemy.inspect(job).dict, "precondition: the instance must really be expired"


@pytest.mark.parametrize("how", ["rollback", "expire_all"])
def test_heartbeat_on_an_expired_pinned_job_starts_with_its_update(factory, how):
    """Kills M14 (``job.id`` read for the UPDATE): the old code's first statement was a refresh SELECT."""
    job_id, *_ = _make(factory)
    seen = {}

    def body(self, db, job, ctx):
        _expire_for_real(db, job, how)
        with _Trace(factory.kw["bind"]) as trace:
            heartbeat(db, job, 21)
        seen["verbs"] = trace.verbs

    _task("t.sql_hb_ok", body)(job_id)
    assert seen["verbs"][0] == "UPDATE"
    assert seen["verbs"].count("UPDATE") == 2, "the advance, then the flush at commit"


@pytest.mark.parametrize("how", ["rollback", "expire_all"])
def test_lock_job_on_an_expired_pinned_job_issues_one_update_and_no_select(factory, how):
    """Kills M14 and the eager-``getattr`` variant of M15 for lock_job (the existing refresh-event test
    covers heartbeat only)."""
    job_id, *_ = _make(factory)
    seen = {}

    def body(self, db, job, ctx):
        _expire_for_real(db, job, how)
        with _Trace(factory.kw["bind"]) as trace:
            lock_job(db, job)
        seen["verbs"] = trace.verbs

    _task("t.sql_lock_ok", body)(job_id)
    assert seen["verbs"] == ["UPDATE"]


@pytest.mark.parametrize("how", ["rollback", "expire_all"])
@pytest.mark.parametrize("fn", ["heartbeat", "lock_job"])
def test_a_lost_claim_on_an_expired_pinned_job_raises_without_any_select(factory, fn, how):
    """Kills M14 for the message (``job.id`` in the JobLost text refreshes an expired instance, and on a
    dead connection would raise OperationalError instead of JobLost). JobLost is caught inside the body so
    run()'s own settle statements do not pollute the trace."""
    job_id, *_ = _make(factory)
    seen = {}

    def body(self, db, job, ctx):
        _expire_for_real(db, job, how)
        _bump_claim_token(factory, job_id)
        with _Trace(factory.kw["bind"]) as trace:
            try:
                (heartbeat(db, job, 21) if fn == "heartbeat" else lock_job(db, job))
            except JobLost as lost:
                seen["message"] = str(lost)
        seen["verbs"] = trace.verbs

    _task(f"t.sql_lost.{fn}.{how}", body)(job_id)
    assert seen["verbs"] == ["UPDATE"]
    assert seen["message"] == f"job {job_id} is no longer running (or was resumed by a fresher claim)"


# ── 10. the captured claim is what the end-of-run writes use ──────────────────────────────────

def _zombie_endings():
    def finish(self, db, job, ctx):
        return None

    return [
        ("return", finish, None),
        ("system_exit", _raises(SystemExit("shutdown")), SystemExit),
        ("value_error", _raises(ValueError("boom")), ValueError),
        ("connection_error", _raises(ConnectionError("blip")), ConnectionError),
    ]


@pytest.mark.parametrize("name,ending,expected", _zombie_endings(), ids=[e[0] for e in _zombie_endings()])
def test_a_zombie_with_an_expired_job_cannot_touch_the_resumed_runs_row_at_any_ending(factory, name, ending, expected):
    """Kills M16: an end-of-run write (done-stamp, _fail_interrupted, _settle_failure's fail-CAS or
    retry-reset) that builds its token from the live mapped ``job.claim_token`` instead of the captured
    Claim. The existing zombie tests bump the token without expiring ``job`` (expire_on_commit=False keeps
    the cached value correct), so a live-read mutant survives them; here the mapped token is expired first,
    so a live read would see the RESUMED run's token and wrongly match.

    ConnectionError is transient: on correct code the retry-reset loses its CAS, so ``retry`` is NOT
    called and the raw ConnectionError propagates (the patch matters only for the mutant)."""
    job_id, reel_id, cut_id = _make(factory)

    def body(self, db, job, ctx):
        db.query(models.Cut).all()
        db.rollback()                                   # expires job.claim_token
        _bump_claim_token(factory, job_id)              # a fresher claim lands: the row is at 2
        return ending(self, db, job, ctx)

    task = _task(f"t.zombie_end.{name}", body)
    with patch.object(task, "retry", side_effect=Retry()) as retry:
        if expected is None:
            task(job_id)
        else:
            with pytest.raises(expected):
                task(job_id)

    retry.assert_not_called()
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.running, "a zombie must not touch the resumed run's row"
    assert job.claim_token == 2
    assert reel.status == models.ReelStatus.generating
