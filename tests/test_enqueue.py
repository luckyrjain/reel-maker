"""Direct tests of api/enqueue.py::enqueue_job — the commit-before-delay policy every job-creating
route shares. Real sessions with the default expire_on_commit=True (what the routers use), not
mocks of the session, since the policy is about transaction and expiry state."""
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.enqueue import enqueue_job
from api.state import CUT_TRANSITIONS, transition


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _cut(db, status=models.CutStatus.draft):
    reel = models.Reel(context="x", status=models.ReelStatus.draft)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
        target_length_s=45.0, status=status,
    )
    db.add(cut)
    db.commit()
    return cut


def _render_job(cut):
    return models.Job(
        type=models.JobType.render, reel_id=cut.reel_id, cut_id=cut.id,
        status=models.JobStatus.pending, progress=0,
    )


def test_success_commits_the_job_enqueues_it_and_leaves_it_refreshed(session_factory):
    db = session_factory()
    cut = _cut(db)
    task = MagicMock()
    job = _render_job(cut)

    enqueue_job(db, job, task, what="render")

    # Read the expiry state BEFORE touching any attribute: touching one lazily reloads the row and
    # would hide a missing refresh.
    expired = set(inspect(job).expired_attributes)
    assert not {"id", "status", "progress"} & expired, "job must be refreshed: the commit expired it"
    task.delay.assert_called_once_with(job.id)
    other = session_factory()
    assert other.get(models.Job, job.id).status == models.JobStatus.pending


def test_the_owner_mutation_and_the_job_land_in_one_commit(session_factory):
    db = session_factory()
    cut = _cut(db)
    transition(cut, "rendering", CUT_TRANSITIONS)

    enqueue_job(db, _render_job(cut), MagicMock(), what="render")

    other = session_factory()
    assert other.get(models.Cut, cut.id).status == models.CutStatus.rendering
    assert other.query(models.Job).filter_by(cut_id=cut.id).count() == 1


def test_everything_is_committed_before_delay_runs(session_factory):
    db = session_factory()
    cut = _cut(db)
    events = []
    event.listen(db, "after_commit", lambda s: events.append("commit"))
    task = MagicMock()
    task.delay.side_effect = lambda jid: events.append("delay")

    enqueue_job(db, _render_job(cut), task, what="render")

    assert "commit" in events[: events.index("delay")]


def test_no_transaction_is_open_while_delay_runs(session_factory):
    """A slow broker failure must not outlive an idle-in-transaction timeout, so the session has to
    be untouched between the commit and delay — including not lazily reloading the expired job."""
    db = session_factory()
    cut = _cut(db)
    seen = {}
    task = MagicMock()
    task.delay.side_effect = lambda jid: seen.update(open=db.in_transaction())

    enqueue_job(db, _render_job(cut), task, what="render")

    assert seen["open"] is False


@pytest.mark.parametrize("what", ["render", "publish", "job"])
def test_a_broker_failure_raises_a_503_naming_the_thing(session_factory, what):
    db = session_factory()
    cut = _cut(db)
    task = MagicMock()
    task.delay.side_effect = ConnectionError("broker down")

    with pytest.raises(HTTPException) as exc:
        enqueue_job(db, _render_job(cut), task, what=what)

    assert exc.value.status_code == 503
    assert exc.value.detail == f"Could not queue the {what} — try again"
    assert isinstance(exc.value.__cause__, ConnectionError)


def test_a_broker_failure_fails_the_job_and_frees_the_owner(session_factory):
    db = session_factory()
    cut = _cut(db)
    transition(cut, "rendering", CUT_TRANSITIONS)
    task = MagicMock()
    task.delay.side_effect = ConnectionError("broker down")

    with pytest.raises(HTTPException):
        enqueue_job(db, _render_job(cut), task, what="render")

    other = session_factory()
    job = other.query(models.Job).filter_by(cut_id=cut.id).one()
    assert job.status == models.JobStatus.failed and "could not enqueue" in job.error
    assert other.get(models.Cut, cut.id).status == models.CutStatus.failed


def test_a_broker_failure_does_not_refresh_or_double_enqueue(session_factory):
    db = session_factory()
    cut = _cut(db)
    task = MagicMock()
    task.delay.side_effect = ConnectionError("broker down")

    with pytest.raises(HTTPException):
        enqueue_job(db, _render_job(cut), task, what="render")

    task.delay.assert_called_once()
