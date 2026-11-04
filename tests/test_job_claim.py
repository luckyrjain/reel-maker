"""Tests for ``worker.tasks.common.Claim`` -- the one immutable object that carries a run's fencing
identity (docs/specs/2026-11-job-claim-module-design.md, §6 "New tests").

Most docstrings name the mutant (M-number in that spec's table) they kill; the signature, AST-guard and positive-control
tests guard mutants outside that table. The plain helpers come from
``test_job_lifecycle`` (flat import, the same precedent ``test_r3_proposed.py`` follows); the fixtures are
defined here because the repo convention is one ``factory``/``file_factory`` per file (importing a fixture
by name makes ruff flag every test that takes it as a redefinition, F811).
"""
import ast
import dataclasses
import inspect
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy
from celery.exceptions import Reject, Retry
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.tasks.common as common
from api import models
from api.state import JOB_IN_FLIGHT
from test_job_lifecycle import _bump_claim_token, _claim_for, _make, _raises, _read, _set_job, _task
from worker.tasks.common import Claim, JobLost, _claim_of, fail_unenqueued, heartbeat, lock_job

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
    db = factory(expire_on_commit=False)     # as job_task's own sessions: nothing reloads the mirror below
    job = db.get(models.Job, job_id)
    assert not hasattr(job, "_claim"), "a Job loaded outside job_task carries no pinned claim"

    if fn == "heartbeat":
        heartbeat(db, job, 33)
        # heartbeat also mirrors the write onto the loaded Job (a body that reads job.progress afterwards
        # must not see a stale value, and expire_on_commit=False would keep one forever)
        assert job.progress == 33
        assert job.heartbeat_at is not None and job.heartbeat_at.replace(tzinfo=None) > _LONG_AGO
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

    assert not db.in_transaction(), "JobLost must have rolled the caller's session back"
    assert _heartbeat_at(factory, job_id) == _LONG_AGO, "the fenced call must not have written"
    db.close()


@pytest.mark.parametrize("job_type", [models.JobType.generate, models.JobType.render])
def test_for_loaded_job_equals_the_claim_a_real_run_captured(factory, job_type):
    """Non-tautological equality: the left side is built from the row, the right side by ``run()``. A render
    job too, so the owner pair is shown to come from ``job.type`` (cut/rendering), not a constant."""
    _make(factory, job_type)                 # a throwaway first, so the job id (2) differs from the token (1)
    job_id, reel_id, cut_id = _make(factory, job_type)
    assert job_id != 1
    seen = {}

    def body(self, db, job, ctx):
        seen["claim"] = job._claim

    _task("t.claim_equality", body, job_type.value)(job_id)

    db = factory()
    rebuilt = Claim.for_loaded_job(db.get(models.Job, job_id))
    db.close()
    kind, state = JOB_IN_FLIGHT[job_type.value]
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
    positional from_status/values and the claim as a keyword -- the shape the existing spy relies on."""
    calls = []

    def spy(db, jid, frm, values, *, claim):
        calls.append((jid, frm, values, claim.token))
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


# ── 1. no parameter that carries a fence has a default ────────────────────────────────────────

def _scanned_callables():
    """Every function defined in ``worker.tasks.common`` plus everything callable on ``Claim``. Lambdas and
    nested functions have no name to enumerate; they are protected by the required keyword (a TypeError at
    the first execution) and reached by the ledger scenarios below."""
    found = {name: obj for name, obj in vars(common).items()
             if inspect.isfunction(obj) and obj.__module__ == common.__name__}
    for name, obj in vars(Claim).items():
        if isinstance(obj, (classmethod, staticmethod)):
            obj = obj.__func__
        if callable(obj):                      # member descriptors (the slots), __match_args__ ... are not
            found[f"Claim.{name}"] = obj
    found["Claim.__init__"] = Claim.__init__
    return found


def _fence_parameters(fn):
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):            # a C-level slot wrapper with no introspectable signature
        return []
    return [p for p in parameters
            if "claim" in p.name.lower() or "token" in p.name.lower() or "Claim" in str(p.annotation)]


def test_no_fence_parameter_anywhere_in_common_has_a_default():
    """Pins the structural answer to "forgetting the token compiles and silently disables fencing": a
    parameter that is, is named like, or is typed as a Claim/token must be passed explicitly, so a forgotten
    fence is a TypeError, not an unfenced write. Mutants that give ``claim`` a default (=None) on _advance,
    _fail_job or _settle_failure are killed here."""
    scanned = _scanned_callables()
    assert {"_advance", "_fail_job", "_settle_failure", "_fail_interrupted", "_fail_rejected_retry",
            "_heartbeat_loop", "heartbeat", "Claim.advance", "Claim.for_loaded_job", "Claim.__init__"} <= set(scanned)
    seen = set()
    for name, fn in scanned.items():
        for parameter in _fence_parameters(fn):
            seen.add((name, parameter.name))
            assert parameter.default is inspect.Parameter.empty, (
                f"{name}({parameter.name}) has a default: a forgotten fence would compile and run unfenced")
    assert seen >= {(fn, "claim") for fn in ("_advance", "_fail_job", "_fail_interrupted", "_fail_rejected_retry",
                                             "_settle_failure", "_heartbeat_loop")} | {("Claim.__init__", "token")}, (
        "the scan must actually have seen the fence parameters")


def test_the_claim_dataclass_fields_have_no_defaults():
    names = [f.name for f in dataclasses.fields(Claim)]
    assert names == ["job_id", "token", "owner_kind", "owner_state"]
    for f in dataclasses.fields(Claim):
        assert f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING, f.name


@pytest.mark.parametrize("name", ["_advance", "_fail_job", "_fail_interrupted", "_fail_rejected_retry",
                                  "_settle_failure"])
def test_the_lifecycle_functions_take_claim_as_a_required_keyword(name):
    parameter = inspect.signature(getattr(common, name)).parameters["claim"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_the_heartbeat_loop_takes_the_claim_first_and_without_a_default():
    parameters = list(inspect.signature(common._heartbeat_loop).parameters.values())
    assert parameters[0].name == "claim" and parameters[0].default is inspect.Parameter.empty


@pytest.mark.parametrize("name", ["fail_unenqueued", "_fail_unclaimed", "_fail_job_keep_owner"])
def test_the_unfenced_entry_points_have_no_fence_parameter(name):
    """A write that is deliberately unfenced is a separately named function, not an optional argument."""
    assert _fence_parameters(getattr(common, name)) == []


# ── 2. the fence ledger: exactly which _advance writes are unfenced ───────────────────────────

_PENDING, _RUNNING, _DONE = models.JobStatus.pending, models.JobStatus.running, models.JobStatus.done
_CLAIM_WRITE = (_PENDING, frozenset({"status", "heartbeat_at", "claim_token"}))
_PENDING_TO_FAILED = (_PENDING, frozenset({"status", "error"}))
_DONE_TO_FAILED = (_DONE, frozenset({"status", "error"}))


class _Ledger:
    """Spies ``_advance`` ONLY (``_fail_job`` calls it, so spying both would double-count). Every call is
    recorded as (from_status, value keys, claim is None, claim token) BEFORE it is forwarded or made to
    raise, so a call that the scenario then fails is still on the ledger. Calls from the heartbeat thread
    are ignored."""

    def __init__(self, inject_at_claim_write=None):
        self.real = common._advance            # captured before the patch is installed
        self.inject = inject_at_claim_write
        self.records = []

    def __call__(self, db, job_id, from_status, values, *, claim):
        if not threading.current_thread().name.startswith("heartbeat-job-"):
            self.records.append((from_status, frozenset(values), claim is None,
                                 None if claim is None else claim.token, job_id))
        if self.inject is not None and (from_status, frozenset(values)) == _CLAIM_WRITE:
            raise self.inject
        return self.real(db, job_id, from_status, values, claim=claim)

    @property
    def unfenced(self):
        return Counter((frm, keys) for frm, keys, no_claim, _, _ in self.records if no_claim)

    @property
    def fenced_tokens(self):
        return [token for _, _, no_claim, token, _ in self.records if not no_claim]

    @property
    def fenced_job_ids(self):
        return [job_id for _, _, no_claim, _, job_id in self.records if not no_claim]


def _drive(task, job_id, expected_exc=None, retry=None):
    """Run a task; ``retry`` patches ``task.retry`` to raise that exception (Retry or Reject)."""
    with patch.object(task, "retry", side_effect=retry or Retry()):
        if expected_exc is None:
            task(job_id)
        else:
            with pytest.raises(expected_exc):
                task(job_id)


def _success(factory):
    job_id, *_ = _make(factory)
    _drive(_task("t.ledger.success", lambda self, db, job, ctx: None), job_id)


def _transient_retry(factory):
    job_id, *_ = _make(factory)
    _drive(_task("t.ledger.retry", _raises(ConnectionError("blip"))), job_id, Retry)


def _hard_failure(factory):
    job_id, *_ = _make(factory)
    _drive(_task("t.ledger.hard", _raises(ValueError("boom"))), job_id, ValueError)


def _shutdown(factory):
    job_id, *_ = _make(factory)
    _drive(_task("t.ledger.shutdown", _raises(SystemExit("bye"))), job_id, SystemExit)


def _reject_owned(factory):
    job_id, *_ = _make(factory)
    _drive(_task("t.ledger.reject_owned", _raises(ConnectionError("blip"))), job_id, Reject, retry=Reject())


def _reject_not_owned(factory):
    job_id, *_ = _make(factory)
    _drive(_task("t.ledger.reject_not_owned", _raises(ConnectionError("unused"))), job_id, Reject, retry=Reject())


def _pre_claim_failure(factory):
    job_id, *_ = _make(factory)
    _drive(_task("t.ledger.preclaim", _raises(ConnectionError("unused"))), job_id, ValueError)


def _unenqueued(factory):
    job_id, *_ = _make(factory)
    fail_unenqueued(factory(), job_id, models.JobType.generate.value, ConnectionError("broker down"))


def _raising_hook(result):
    raise ConnectionError("enqueue failed")


def _after_commit_failure(factory):
    job_id, *_ = _make(factory)
    task = _task("t.ledger.after_commit", lambda self, db, job, ctx: 1, after_commit=_raising_hook)
    _drive(task, job_id, ConnectionError)


def _shutdown_during_after_commit(factory):
    job_id, *_ = _make(factory)

    def hook(result):
        raise SystemExit("bye")

    task = _task("t.ledger.after_commit_exit", lambda self, db, job, ctx: 1, after_commit=hook)
    _drive(task, job_id, SystemExit)


def _plain_raising_cleanup_hook(factory):
    job_id, *_ = _make(factory)

    def cleanup(db, job, result):
        raise RuntimeError("cleanup bug")

    task = _task("t.ledger.cleanup_raises", lambda self, db, job, ctx: 1, after_commit=_raising_hook,
                 after_commit_failed=cleanup)
    _drive(task, job_id, ConnectionError)


def _cleanup_hook_with_a_failed_savepoint_rollback(factory):
    """The hook WRITES and raises while the SAVEPOINT rollback fails: the fail-stamp is redone, so the
    done -> failed CAS runs twice. Driven through run() (the sibling tests call _stamp_failed_and_run_cleanup)."""
    job_id, *_ = _make(factory)
    other_job_id, *_ = _make(factory)

    def cleanup(db, job, result):
        db.query(models.Job).filter(models.Job.id == other_job_id).update({"progress": 9})
        db.flush()
        raise RuntimeError("cleanup bug")

    def boom(*a, **k):
        raise OSError("simulated dead connection during ROLLBACK TO SAVEPOINT")

    task = _task("t.ledger.savepoint", lambda self, db, job, ctx: 1, after_commit=_raising_hook,
                 after_commit_failed=cleanup)
    with patch("sqlalchemy.dialects.sqlite.pysqlite.SQLiteDialect_pysqlite.do_rollback_to_savepoint", boom):
        _drive(task, job_id, ConnectionError)


def _heartbeat_job_lost(factory):
    job_id, *_ = _make(factory)

    def body(self, db, job, ctx):
        _set_job(factory, job_id, status=models.JobStatus.failed)    # a status change, not a token bump
        heartbeat(db, job, 50)

    _drive(_task("t.ledger.hb_lost", body), job_id, JobLost)


# name -> (scenario, error injected at the claim write, the exact multiset of UNFENCED _advance calls)
_LEDGER = {
    "success": (_success, None, {_CLAIM_WRITE: 1}),
    "transient_retry": (_transient_retry, None, {_CLAIM_WRITE: 1}),
    "hard_failure": (_hard_failure, None, {_CLAIM_WRITE: 1}),
    "shutdown": (_shutdown, None, {_CLAIM_WRITE: 1}),
    "reject_owned": (_reject_owned, None, {_CLAIM_WRITE: 1}),
    "reject_not_owned": (_reject_not_owned, ConnectionError("db down"), {_CLAIM_WRITE: 1, _PENDING_TO_FAILED: 1}),
    "pre_claim_failure": (_pre_claim_failure, ValueError("db down"), {_CLAIM_WRITE: 1, _PENDING_TO_FAILED: 1}),
    "fail_unenqueued": (_unenqueued, None, {_PENDING_TO_FAILED: 1}),
    "after_commit_failure": (_after_commit_failure, None, {_CLAIM_WRITE: 1, _DONE_TO_FAILED: 1}),
    "shutdown_during_after_commit": (_shutdown_during_after_commit, None, {_CLAIM_WRITE: 1, _DONE_TO_FAILED: 1}),
    "plain_raising_cleanup_hook": (_plain_raising_cleanup_hook, None, {_CLAIM_WRITE: 1, _DONE_TO_FAILED: 1}),
    "failed_savepoint_rollback": (_cleanup_hook_with_a_failed_savepoint_rollback, None,
                                  {_CLAIM_WRITE: 1, _DONE_TO_FAILED: 2}),
    "heartbeat_job_lost": (_heartbeat_job_lost, None, {_CLAIM_WRITE: 1}),
}


@pytest.mark.parametrize("name", list(_LEDGER))
def test_the_set_of_unfenced_advance_writes_is_exactly_the_documented_one(factory, name):
    """Kills any mutant that turns a fenced write into ``claim=None`` or makes an unfenced one fenced (M2-M9,
    M16 at the ledger level). Every OTHER _advance call must carry this run's claim, whose token is 1 (the
    post-claim value of _make's token: a constant, not read from the run or from the row)."""
    drive, inject, expected = _LEDGER[name]
    ledger = _Ledger(inject)
    _make(factory)             # a throwaway first, so the scenario's job id (2) differs from its token (1)
    with patch("worker.tasks.common._advance", ledger):
        drive(factory)
    assert ledger.records, "the spy must have seen the scenario's writes"
    assert ledger.unfenced == Counter(expected)
    assert set(ledger.fenced_tokens) <= {1}
    assert set(ledger.fenced_job_ids) <= {2}, "a fenced write must carry the claim of the run's own job"


# ── 7. a Claim that disagrees with the arguments next to it is refused, before any write ──────

def _two_jobs(factory, *, status=models.JobStatus.running):
    a, *_ = _make(factory, status=status)
    b, *_ = _make(factory, status=status)
    return a, b


def _status(factory, job_id):
    db = factory()
    try:
        return db.get(models.Job, job_id).status
    finally:
        db.close()


def _stub_task(retries=0, max_retries=2):
    return SimpleNamespace(request=SimpleNamespace(retries=retries), max_retries=max_retries)


def _mismatch(factory, mismatch, job_a, job_b):
    """(claim, job_id argument, owner pair arguments) where exactly one fact disagrees with the claim.
    For "job_id" the claim is a REAL claim on job B while the call is aimed at job A, so a missing check
    would write to B (or A) instead of raising."""
    kind, state = JOB_IN_FLIGHT["generate"]
    if mismatch == "job_id":
        return _claim_for(factory, job_b), job_a, (kind, state)
    token = _claim_for(factory, job_a).token
    if mismatch == "owner_kind":
        return Claim(job_a, token, "cut", state), job_a, (kind, state)
    return Claim(job_a, token, kind, "rendering"), job_a, (kind, state)


def test_advance_refuses_a_claim_for_another_job_and_writes_nothing(factory):
    """Kills M13 (the _advance job_id check removed)."""
    a, b = _two_jobs(factory)
    db = factory()
    with pytest.raises(ValueError, match="claim is for job"):
        common._advance(db, a, _RUNNING, {"progress": 7}, claim=_claim_for(factory, b))
    db.rollback()
    assert [_read(factory, j, 0, 0)[0].progress for j in (a, b)] == [0, 0]


def test_advance_accepts_a_matching_claim_and_claim_none(factory):
    a, b = _two_jobs(factory)
    db = factory()
    assert common._advance(db, a, _RUNNING, {"progress": 7}, claim=_claim_for(factory, a)) is True
    assert common._advance(db, b, _RUNNING, {"progress": 8}, claim=None) is True
    db.commit()


@pytest.mark.parametrize("owner", [("cut", "generating"), ("reel", "rendering")], ids=["kind", "state"])
def test_fail_job_refuses_a_claim_with_another_owner_pair_before_writing(factory, owner):
    """Kills M13 (the _fail_job owner-pair check removed, or weakened to compare only one of the two fields):
    job_id and token both match and exactly ONE owner field differs, so only this check can catch it."""
    a, _ = _two_jobs(factory)
    db = factory()
    claim = Claim(a, _claim_for(factory, a).token, *owner)
    with pytest.raises(ValueError, match="claim owns"):
        common._fail_job(db, a, _RUNNING, "m", "reel", "generating", claim=claim)
    db.rollback()
    assert _status(factory, a) == _RUNNING


def test_fail_job_refuses_a_claim_for_another_job_via_advance(factory):
    a, b = _two_jobs(factory)
    db = factory()
    with pytest.raises(ValueError, match="claim is for job"):
        common._fail_job(db, a, _RUNNING, "m", "reel", "generating", claim=_claim_for(factory, b))
    db.rollback()
    assert (_status(factory, a), _status(factory, b)) == (_RUNNING, _RUNNING)


def test_fail_job_accepts_a_matching_claim_and_claim_none(factory):
    a, b = _two_jobs(factory)
    db = factory()
    assert common._fail_job(db, a, _RUNNING, "m", "reel", "generating", claim=_claim_for(factory, a)) is True
    assert common._fail_job(db, b, _RUNNING, "m", "reel", "generating", claim=None) is True
    db.commit()


@pytest.mark.parametrize("committed", [False, True], ids=["retriable", "committed"])
@pytest.mark.parametrize("mismatch", ["job_id", "owner_kind", "owner_state"])
def test_settle_failure_refuses_an_inconsistent_claim_on_both_paths(factory, mismatch, committed):
    """Kills M13 for _settle_failure's two checks, including the variant that checks only inside the
    not-committed branch. On the retriable path the reset goes through ``claim.advance`` (``claim.job_id``),
    so nothing but its own check can see the mismatch; on the committed path ``_advance`` is never reached
    with the claim at all."""
    a, b = _two_jobs(factory, status=_DONE if committed else _RUNNING)
    claim, job_id, (kind, state) = _mismatch(factory, mismatch, a, b)
    db = factory()
    with pytest.raises(ValueError):
        common._settle_failure(_stub_task(), db, job_id, ConnectionError("blip"), claim=claim,
                               committed=committed, owner_kind=kind, owner_state=state, result=None,
                               after_commit_failed=None, max_runtime_s=60)
    db.rollback()
    untouched = _DONE if committed else _RUNNING
    assert (_status(factory, a), _status(factory, b)) == (untouched, untouched)


def test_settle_failure_accepts_a_matching_claim_and_claim_none(factory):
    a, b = _two_jobs(factory)
    kind, state = JOB_IN_FLIGHT["generate"]
    common_args = dict(committed=False, owner_kind=kind, owner_state=state, result=None,
                       after_commit_failed=None, max_runtime_s=60)
    db = factory()
    assert common._settle_failure(_stub_task(), db, a, ConnectionError("blip"), claim=_claim_for(factory, a),
                                  **common_args) is True
    assert _status(factory, a) == models.JobStatus.pending
    pending, _ = _two_jobs(factory, status=_PENDING)
    assert common._settle_failure(_stub_task(), factory(), pending, ConnectionError("blip"), claim=None,
                                  **common_args) is True


@pytest.mark.parametrize("fn_name,status", [("_fail_interrupted", _RUNNING), ("_fail_rejected_retry", _PENDING)])
@pytest.mark.parametrize("mismatch", ["job_id", "owner_kind", "owner_state"])
def test_the_terminal_recorders_refuse_an_inconsistent_claim_and_write_nothing(factory, fn_name, status, mismatch):
    """Kills M13 for the recorders: they reach _advance/_fail_job's checks with their own job_id/owner args."""
    a, b = _two_jobs(factory, status=status)
    claim, job_id, (kind, state) = _mismatch(factory, mismatch, a, b)
    exc = SystemExit(1) if fn_name == "_fail_interrupted" else ConnectionError("broker down")
    with pytest.raises(ValueError):
        getattr(common, fn_name)(factory(), job_id, exc, kind, state, claim=claim)
    assert (_status(factory, a), _status(factory, b)) == (status, status)


@pytest.mark.parametrize("fn_name,status", [("_fail_interrupted", _RUNNING), ("_fail_rejected_retry", _PENDING)])
def test_the_terminal_recorders_accept_a_matching_claim(factory, fn_name, status):
    job_id, _ = _two_jobs(factory, status=status)
    kind, state = JOB_IN_FLIGHT["generate"]
    exc = SystemExit(1) if fn_name == "_fail_interrupted" else ConnectionError("broker down")
    getattr(common, fn_name)(factory(), job_id, exc, kind, state, claim=_claim_for(factory, job_id))
    assert _status(factory, job_id) == models.JobStatus.failed


def test_fail_rejected_retry_with_claim_none_goes_through_the_unclaimed_path(factory):
    job_id, _ = _two_jobs(factory, status=_PENDING)
    kind, state = JOB_IN_FLIGHT["generate"]
    common._fail_rejected_retry(factory(), job_id, ConnectionError("broker down"), kind, state, claim=None)
    assert _status(factory, job_id) == models.JobStatus.failed


# ── 8. no write to Job rows bypasses _advance ─────────────────────────────────────────────────

_STATUS_WRITERS = {"_fail_job", "_fail_job_keep_owner", "run"}
_BULK_OK_IN = "_advance"


def _mentions_job(node):
    return any((isinstance(n, ast.Name) and n.id == "Job") or (isinstance(n, ast.Attribute) and n.attr == "Job")
               for n in ast.walk(node))


class _BypassFinder(ast.NodeVisitor):
    """Collects ways to write Job rows without going through ``_advance``. The enclosing scope is the
    INNERMOST ``def`` (``run`` is nested in ``decorate`` inside ``job_task``). Deliberately conservative: a
    legitimate future Job query outside ``_advance`` has to be allow-listed here, which is the point."""

    def __init__(self, bulk_ok_in=_BULK_OK_IN):
        self.scopes = ["<module>"]
        self.bulk_ok_in = bulk_ok_in
        self.found = []

    def _flag(self, node, what):
        self.found.append((self.scopes[-1], what))

    def visit_FunctionDef(self, node):
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and self.scopes[-1] != self.bulk_ok_in:
            arguments = [*node.args, *(k.value for k in node.keywords)]
            if func.attr == "query" and any(_mentions_job(a) for a in arguments):
                self._flag(node, "query(Job)")
            elif func.attr == "update" and any(k.arg == "synchronize_session" for k in node.keywords):
                self._flag(node, "bulk update")
            elif func.attr in ("merge", "execute"):
                self._flag(node, func.attr)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if self.scopes[-1] != self.bulk_ok_in:
            if node.attr == "bulk_update_mappings":
                self._flag(node, "bulk_update_mappings")
            if node.attr == "__table__" and _mentions_job(node.value):
                self._flag(node, "Job.__table__")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if (node.module or "").split(".")[0] == "sqlalchemy":
            for alias in node.names:
                if alias.name in ("update", "text", "delete"):
                    self._flag(node, f"import {alias.name}")
        self.generic_visit(node)

    def _check_targets(self, targets):
        for target in targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                self._check_targets(target.elts)
            elif isinstance(target, ast.Starred):
                self._check_targets([target.value])
            elif isinstance(target, ast.Attribute):
                if target.attr == "claim_token":
                    self._flag(target, "assigns .claim_token")
                elif target.attr == "status" and self.scopes[-1] not in _STATUS_WRITERS:
                    self._flag(target, "assigns .status")

    def visit_Assign(self, node):
        self._check_targets(node.targets)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._check_targets([node.target])
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._check_targets([node.target])
        self.generic_visit(node)


def _bypasses(source, **kwargs):
    finder = _BypassFinder(**kwargs)
    finder.visit(ast.parse(source))
    return finder.found


def test_nothing_in_common_writes_job_rows_around_advance():
    source = Path(common.__file__).read_text()
    assert _bypasses(source) == []


def test_the_bypass_finder_is_not_vacuous_on_the_real_file():
    """With _advance's own exemption removed it must find the one bulk update the file has, and only there."""
    found = _bypasses(Path(common.__file__).read_text(), bulk_ok_in=None)
    assert {scope for scope, _ in found} == {"_advance"}
    assert {what for _, what in found} >= {"query(Job)", "bulk update"}


@pytest.mark.parametrize("snippet,what", [
    ("def f(db, jid):\n    db.query(models.Job).filter(models.Job.id == jid).update({}, synchronize_session=False)",
     {"query(Job)", "bulk update"}),
    ("def f(s, jid):\n    return s.query(Job).filter(Job.id == jid).first()", {"query(Job)"}),
    ("def f(session):\n    return session.query(models.Reel, models.Job)", {"query(Job)"}),
    ("def f(db):\n    db.bulk_update_mappings(models.Job, [])", {"bulk_update_mappings"}),
    ("def f():\n    return models.Job.__table__", {"Job.__table__"}),
    ("def f(db, j):\n    db.merge(j)", {"merge"}),
    ("def f(db):\n    db.execute('UPDATE jobs SET status = 1')", {"execute"}),
    ("from sqlalchemy import update", {"import update"}),
    ("from sqlalchemy.sql import text, delete", {"import text", "import delete"}),
    ("def settle(job):\n    job.status = 'failed'", {"assigns .status"}),
    ("def settle(job):\n    job.status, other = 'failed', 1", {"assigns .status"}),
    ("def run(job):\n    job.claim_token = 5", {"assigns .claim_token"}),
    ("def _advance(job):\n    job.claim_token += 1", {"assigns .claim_token"}),
    ("def job_task():\n    def decorate():\n        job.status = 1", {"assigns .status"}),
    ("merge_it = lambda db, j: db.merge(j)", {"merge"}),
])
def test_the_bypass_finder_flags_synthetic_bypasses(snippet, what):
    assert {w for _, w in _bypasses(snippet)} >= what


@pytest.mark.parametrize("snippet", [
    "def _advance(db):\n    db.query(models.Job).update({}, synchronize_session=False)",
    "def f(db):\n    return db.query(models.Cut).all()",
    "def run(job):\n    job.status = 'done'",
    "def job_task():\n    def decorate():\n        def run():\n            job.status = 1",
    "def f(db):\n    return db.get(models.Job, 1)",
    "from sqlalchemy import exc as sa_exc",
])
def test_the_bypass_finder_allows_what_the_real_file_does(snippet):
    assert _bypasses(snippet) == []


def test_fail_rejected_retry_with_a_claim_recovers_when_its_own_connection_is_dead(factory):
    """The owned branch of ``_fail_rejected_retry`` (the production path for a Reject on a claimed run) must
    write through the session ``_finalize_or_reconnect`` hands it, so a dead caller connection is recovered on
    a fresh one -- the existing test of that recovery only covers the ``claim=None`` branch."""
    job_id, reel_id, cut_id = _make(factory)
    claim = _claim_for(factory, job_id)
    db = factory()

    def dead():
        raise sqlalchemy.exc.OperationalError("ROLLBACK", {}, Exception("gone"))

    with patch.object(db, "rollback", dead):
        common._fail_rejected_retry(db, job_id, ConnectionError("broker down"),
                                    *JOB_IN_FLIGHT["generate"], claim=claim)

    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed
    assert reel.status == models.ReelStatus.failed
