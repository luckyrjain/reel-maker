"""Round-4 proposed tests: each one kills a mutant that survived the existing suite."""
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery import Celery
from celery.exceptions import Reject, Retry
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.db import get_db
from api.main import app
from engine.publish.base import PublishResult
from worker.tasks import common
from worker.tasks.common import JobLost, heartbeat, job_task

celery_test_app = Celery("r4", broker="memory://", backend="cache+memory://")
celery_test_app.conf.update(task_always_eager=True, task_eager_propagates=True)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False)
    with patch("worker.tasks.common.SessionLocal", sf):
        yield sf


@pytest.fixture
def file_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/r4.db")
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


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
    return db.get(models.Job, job_id), db.get(models.Reel, reel_id), db.get(models.Cut, cut_id)


def _set_job(factory, job_id, **values):
    db = factory()
    for k, v in values.items():
        setattr(db.get(models.Job, job_id), k, v)
    db.commit()
    db.close()


def _task(name, body, job_type="generate", max_retries=2, **kwargs):
    body.__name__ = body.__qualname__ = name.replace(".", "_")
    kwargs.setdefault("max_runtime_s", 60)
    unique = f"{name}.{uuid.uuid4().hex[:8]}"
    return celery_test_app.task(bind=True, max_retries=max_retries, name=unique)(job_task(job_type, **kwargs)(body))


def _raises(exc):
    def body(self, db, job, ctx):
        raise exc
    return body


# ── heartbeat ────────────────────────────────────────────────────────────────

def test_heartbeat_commits_for_other_connections(file_factory):
    """StaticPool tests share one connection, so a missing commit is invisible to them. The reaper reads
    from its own connection: an uncommitted beat is no beat (and holds the row lock)."""
    job_id, reel_id, cut_id = _make(file_factory)
    seen = {}

    def body(self, db, job, ctx):
        job.meta = {"x": 1}                  # a pending body mutation heartbeat() promises to commit
        heartbeat(db, job, 55)
        assert not db.in_transaction()
        other = file_factory()
        j = other.get(models.Job, job_id)
        seen["progress"], seen["meta"] = j.progress, j.meta
        other.close()

    with patch("worker.tasks.common.SessionLocal", file_factory):
        _task("r4.hb_commit", body)(job_id)
    assert seen == {"progress": 55, "meta": {"x": 1}}


# ── failure paths must not commit a failed body's flushed writes ─────────────

def test_failure_discards_writes_the_body_already_flushed(factory):
    job_id, reel_id, cut_id = _make(factory)

    def body(self, db, job, ctx):
        db.get(models.Cut, cut_id).caption = "half-done"
        db.flush()                           # autoflush is off, but resolve_beat_assets flushes for real
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _task("r4.flushed", body)(job_id)
    assert _read(factory, job_id, reel_id, cut_id)[2].caption is None


def test_shutdown_failure_discards_writes_the_body_already_flushed(factory):
    """Round 4: a shutdown now fails the job at once instead of releasing it to pending
    (no message would ever come back for a released job under prefork's SIGTERM handling)."""
    job_id, reel_id, cut_id = _make(factory)

    def body(self, db, job, ctx):
        db.get(models.Cut, cut_id).caption = "half-done"
        db.flush()
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _task("r4.flushed_kbd", body)(job_id)
    job, _, cut = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed and cut.caption is None


def test_a_reaped_job_hit_by_a_transient_error_is_not_retried(factory):
    """The reaper failed it (terminal). Raising self.retry() would queue a message the guard rejects."""
    job_id, reel_id, cut_id = _make(factory)

    def body(self, db, job, ctx):
        _set_job(factory, job_id, status=models.JobStatus.failed, error="reaped")
        raise httpx.ConnectTimeout("down")

    task = _task("r4.reaped_transient", body)
    with patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(httpx.ConnectTimeout):
            task(job_id)
    retry.assert_not_called()
    assert _read(factory, job_id, reel_id, cut_id)[0].error == "reaped"


def test_a_commit_failure_at_the_done_stamp_is_a_failed_run_not_a_committed_one(factory):
    """committed must only become true once the done commit returned; otherwise a dropped connection
    at that commit leaves the job `running` with neither a retry nor a failure stamp."""
    job_id, reel_id, cut_id = _make(factory)

    class Flaky(Session):
        armed = True

        def commit(self):
            if Flaky.armed and any(isinstance(o, models.Job) and o.status == models.JobStatus.done
                                   for o in self.identity_map.values()):
                Flaky.armed = False
                raise sa_exc.OperationalError("COMMIT", {}, Exception("connection lost"))
            return super().commit()

    flaky = sessionmaker(bind=factory.kw["bind"], class_=Flaky, autoflush=False)
    task = _task("r4.commit_fail", lambda self, db, job, ctx: None)
    with patch("worker.tasks.common.SessionLocal", flaky), patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(Retry):
            task(job_id)
    retry.assert_called_once()
    assert _read(factory, job_id, reel_id, cut_id)[0].status == models.JobStatus.pending


def test_default_start_progress_is_5(factory):
    job_id, *_ = _make(factory)
    seen = {}

    def body(self, db, job, ctx):
        seen["p"] = job.progress

    _task("r4.progress", body)(job_id)
    assert seen["p"] == 5


# ── refused retry ────────────────────────────────────────────────────────────

def test_fail_rejected_retry_leaves_a_job_a_sibling_already_claimed_alone(factory):
    job_id, reel_id, cut_id = _make(factory, models.JobType.render, status=models.JobStatus.running,
                                    cut_status=models.CutStatus.rendering)
    db = factory()
    common._fail_rejected_retry(db, job_id, ConnectionError("broker down"), "cut", "rendering")
    job, _, cut = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.running and cut.status == models.CutStatus.rendering


def test_fail_rejected_retry_records_why(factory):
    job_id, reel_id, cut_id = _make(factory, models.JobType.render, cut_status=models.CutStatus.rendering)
    common._fail_rejected_retry(factory(), job_id, ConnectionError("broker down"), "cut", "rendering")
    job, _, cut = _read(factory, job_id, reel_id, cut_id)
    assert "could not schedule retry" in job.error and "broker down" in job.error
    assert cut.status == models.CutStatus.failed


def test_a_pre_claim_job_is_failed_when_the_retry_message_is_refused(factory):
    """Round 5: a never-claimed job is retriable, but if the retry MESSAGE itself is then refused by
    the broker, no message is coming back for it either -- the same reasoning _settle_failure already
    applies to an unclaimed job with no retries left. It must not be left pending indefinitely."""
    job_id, reel_id, cut_id = _make(factory)
    real, calls = common._advance, {"n": 0}

    def fail_only_the_claim(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sa_exc.OperationalError("UPDATE", {}, Exception("gone"))
        return real(*a, **k)

    task = _task("r4.preclaim_reject", lambda self, db, job, ctx: None)
    with (
        patch("worker.tasks.common._advance", side_effect=fail_only_the_claim),
        patch.object(task, "retry", side_effect=Reject(ConnectionError("broker down"), requeue=False)),
    ):
        with pytest.raises(Reject):
            task(job_id)
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed and "could not schedule retry" in job.error
    assert reel.status == models.ReelStatus.failed


def test_a_failing_refusal_handler_does_not_replace_the_reject(factory):
    job_id, *_ = _make(factory)
    task = _task("r4.reject_masked", _raises(ConnectionError("blip")))
    with (
        patch.object(task, "retry", side_effect=Reject(ConnectionError("broker down"), requeue=False)),
        patch("worker.tasks.common._fail_rejected_retry", side_effect=RuntimeError("db down")),
    ):
        with pytest.raises(Reject):
            task(job_id)


def test_a_failing_shutdown_release_does_not_replace_the_shutdown(factory):
    job_id, *_ = _make(factory)
    real, calls = common._advance, {"n": 0}

    def fail_the_release(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sa_exc.OperationalError("UPDATE", {}, Exception("gone"))
        return real(*a, **k)

    task = _task("r4.release_fails", _raises(SystemExit(1)))
    with patch("worker.tasks.common._advance", side_effect=fail_the_release):
        with pytest.raises(SystemExit):
            task(job_id)


# ── rollback_owner takes a row lock ──────────────────────────────────────────

def test_rollback_owner_locks_the_row_it_checks(factory):
    job_id, reel_id, cut_id = _make(factory)
    seen = []
    real = Session.refresh

    def spy(self, instance, *a, **kw):
        seen.append(kw.get("with_for_update"))
        return real(self, instance, *a, **kw)

    db = factory()
    with patch.object(Session, "refresh", spy):
        common.rollback_owner(db, db.get(models.Job, job_id), "reel", {"generating"})
    assert seen and all(seen)


# ── heartbeat thread and reaper close their sessions ─────────────────────────

def test_the_heartbeat_thread_closes_every_session_it_opens(file_factory):
    job_id, *_ = _make(file_factory)
    opened, closed = [], []

    class Tracking(Session):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            if threading.current_thread().name.startswith("heartbeat-job-"):
                opened.append(self)

        def close(self):
            if self in opened:
                closed.append(self)
            return super().close()

    tracked = sessionmaker(bind=file_factory.kw["bind"], class_=Tracking, autoflush=False)

    def body(self, db, job, ctx):
        time.sleep(0.5)

    with patch("worker.tasks.common.SessionLocal", tracked), patch("worker.tasks.common.HEARTBEAT_INTERVAL_S", 0.05):
        _task("r4.hb_close", body)(job_id)
    assert opened and len(opened) == len(closed)


def test_the_reaper_closes_its_session(factory):
    from worker.tasks.maintenance import reap_stuck_jobs
    closed = []

    class Tracking(Session):
        def close(self):
            closed.append(1)
            return super().close()

    tracked = sessionmaker(bind=factory.kw["bind"], class_=Tracking, autoflush=False)
    with patch("worker.tasks.maintenance.SessionLocal", tracked):
        reap_stuck_jobs()
    assert closed


# ── reaper details ───────────────────────────────────────────────────────────

def test_a_pending_job_reset_for_retry_after_the_select_is_not_reaped(factory):
    """Pending candidates are re-checked against the PENDING clock (updated_at), not the running one
    (heartbeat_at, which a retried job still carries from its last run)."""
    from worker.tasks import maintenance
    from worker.tasks.maintenance import reap_stuck_jobs
    job_id, reel_id, cut_id = _make(factory, models.JobType.render, cut_status=models.CutStatus.rendering)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    db = factory()
    db.query(models.Job).filter(models.Job.id == job_id).update(
        {"heartbeat_at": long_ago, "updated_at": long_ago}, synchronize_session=False)
    db.commit()
    real = maintenance._reap_one

    def racing(db_, jid, seen, reason, clause):
        other = factory()          # between the SELECT and the UPDATE: claimed, failed transiently, reset
        other.query(models.Job).filter(models.Job.id == jid).update(
            {"updated_at": datetime.now(timezone.utc)}, synchronize_session=False)
        other.commit()
        return real(db_, jid, seen, reason, clause)

    with patch("worker.tasks.maintenance.SessionLocal", factory), patch("worker.tasks.maintenance._reap_one", side_effect=racing):
        reap_stuck_jobs()
    assert _read(factory, job_id, reel_id, cut_id)[0].status == models.JobStatus.pending


def test_the_reaper_records_why_it_failed_a_running_job(factory):
    from worker.tasks.maintenance import reap_stuck_jobs
    long_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    job_id, reel_id, cut_id = _make(factory, models.JobType.render, status=models.JobStatus.running,
                                    cut_status=models.CutStatus.rendering)
    db = factory()
    db.query(models.Job).filter(models.Job.id == job_id).update(
        {"heartbeat_at": long_ago, "updated_at": long_ago}, synchronize_session=False)
    db.commit()
    with patch("worker.tasks.maintenance.SessionLocal", factory):
        reap_stuck_jobs()
    assert "Worker stopped responding" in _read(factory, job_id, reel_id, cut_id)[0].error


# ── celery wiring ────────────────────────────────────────────────────────────

def test_the_reaper_shares_the_generation_queue_not_the_single_render_slot():
    from worker.celery_app import celery_app
    assert celery_app.conf.task_routes["worker.tasks.maintenance.reap_stuck_jobs"]["queue"] == "generation"


def test_job_task_relies_on_late_acks_and_requeue_on_worker_loss():
    from worker.celery_app import celery_app
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


# ── generate / render / enrich / publish success paths ───────────────────────

def _real_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


def test_generate_success_saves_the_guide_moves_the_reel_and_records_the_score():
    from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
    from worker.tasks.generate import generate_guide
    sf = _real_factory()
    db = sf()
    reel = models.Reel(context="plain prose about football", niche="football", status=models.ReelStatus.generating)
    db.add(reel)
    db.flush()
    db.add(models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, target_length_s=30.0,
                      status=models.CutStatus.draft))
    job = models.Job(type=models.JobType.generate, reel_id=reel.id, status=models.JobStatus.pending, meta={})
    db.add(job)
    db.commit()
    job_id, reel_id = job.id, reel.id
    db.close()

    def beat(i, t):
        return Beat(index=i, type=t, duration_s=5, visual_direction="v", on_screen_text=["t"], vo_script="vo")

    guide = MasterGuide(title="t", niche="football", cuts=[PlatformGuide(
        platform="youtube_shorts", target_length_s=30, caption="the caption", hashtags=list("abcde"),
        beats=[beat(0, "hook"), beat(1, "body"), beat(2, "cta")])])
    llm = MagicMock()
    llm.complete.return_value = guide.model_dump_json()
    llm.last_usage = {}
    with (
        patch("worker.tasks.common.SessionLocal", sf),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider", return_value=llm),
        patch("worker.tasks.generate.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.generate._enrich_standard_path_guide"),
        patch("worker.tasks.generate.score_guide", return_value=(90, [])),
        patch("worker.tasks.generate._combined_score", return_value=(90, [])),
        patch("worker.tasks.generate.record_stage"),
    ):
        generate_guide(job_id)
    db = sf()
    assert db.get(models.Reel, reel_id).status == models.ReelStatus.guide_ready
    cut = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).one()
    assert cut.guide["caption"] == "the caption" and cut.caption == "the caption"
    job = db.get(models.Job, job_id)
    assert job.status == models.JobStatus.done and job.progress == 100 and job.meta["quality_score"] == 90


def test_render_success_moves_the_cut_to_in_review_and_records_the_video():
    from worker.tasks.render import render_cut
    from tests.test_render_task import _GUIDE
    sf = _real_factory()
    db = sf()
    reel = models.Reel(context="c", voiceover_mode="silent", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.rendering,
                     guide=_GUIDE)
    db.add(cut)
    db.flush()
    job = models.Job(type=models.JobType.render, reel_id=reel.id, cut_id=cut.id, status=models.JobStatus.pending)
    db.add(job)
    db.commit()
    job_id, cut_id = job.id, cut.id
    db.close()
    with (
        patch("worker.tasks.common.SessionLocal", sf),
        patch("worker.tasks.render.get_asset_sourcer"), patch("worker.tasks.render.get_wiki_sourcer"),
        patch("worker.tasks.render.get_hf_sourcer"), patch("worker.tasks.render.get_hf_video_sourcer"),
        patch("worker.tasks.render.get_tts_provider"),
        patch("worker.tasks.render.resolve_or_reuse", return_value=[(MagicMock(), None)]),
        patch("worker.tasks.render.record_stage"),
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"], None)),
    ):
        render_cut(job_id)
    cut = sf().get(models.Cut, cut_id)
    assert cut.status == models.CutStatus.in_review
    assert cut.video_path.endswith("youtube_shorts.mp4") and cut.duration_s == 18.0


def test_enrich_hands_generate_a_pending_job_that_keeps_the_chosen_generation_path():
    """A generate job created `running` is dropped by the claim guard (chain stalls); a dropped
    generation_path silently turns an explicit 'structured'/'standard' choice back into 'auto'."""
    from worker.tasks.enrich_context import enrich_context
    sf = _real_factory()
    db = sf()
    reel = models.Reel(context="c", status=models.ReelStatus.enriching)
    db.add(reel)
    db.flush()
    job = models.Job(type=models.JobType.enrich, reel_id=reel.id, status=models.JobStatus.pending,
                     meta={"generation_path": "structured"})
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()
    with (
        patch("worker.tasks.common.SessionLocal", sf),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(80, [])),
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.record_stage"),
        patch("worker.tasks.enrich_context.generate_guide") as generate,
    ):
        enrich_context(job_id)
    (gid,), _ = generate.delay.call_args
    gen = sf().get(models.Job, gid)
    assert gen.status == models.JobStatus.pending
    assert gen.meta["generation_path"] == "structured"


def _publish_setup(sf):
    db = sf()
    reel = models.Reel(context="c", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
                     status=models.CutStatus.publishing, video_path="/v.mp4", caption="cap")
    db.add(cut)
    db.flush()
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    job = models.Job(type=models.JobType.publish, reel_id=reel.id, cut_id=cut.id, status=models.JobStatus.pending)
    db.add(job)
    db.commit()
    ids = (job.id, cut.id)
    db.close()
    return ids


def test_a_publish_whose_job_was_reaped_before_the_upload_never_uploads(factory):
    from worker.tasks.publish import publish_cut
    job_id, cut_id = _publish_setup(factory)

    def reaper(db, cid):
        _set_job(factory, job_id, status=models.JobStatus.failed, error="reaped")

    with (
        patch("worker.tasks.publish.assert_safe_to_publish", side_effect=reaper),
        patch("worker.tasks.publish.get_publisher") as get_publisher,
        patch("worker.tasks.publish.record_stage"),
    ):
        with pytest.raises(JobLost):
            publish_cut(job_id)
    get_publisher.return_value.publish.assert_not_called()


def test_no_transaction_is_open_during_the_upload(factory):
    from worker.tasks.publish import publish_cut
    job_id, cut_id = _publish_setup(factory)
    seen = []

    def upload(cut, credential, db, caption):
        seen.append(db.in_transaction())
        return PublishResult(platform_post_id="yt-1", url="u")

    with patch("worker.tasks.publish.get_publisher") as get_publisher, patch("worker.tasks.publish.record_stage"):
        get_publisher.return_value.publish.side_effect = upload
        publish_cut(job_id)
    assert seen == [False]


# ── routers ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    sf = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _override():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        c._session_factory = sf
        yield c
    app.dependency_overrides.clear()


def _cut_row(sf, status, *, post_id=None, guide=True, video="/v.mp4"):
    db = sf()
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=status,
                     video_path=video, platform_post_id=post_id, guide={"beats": []} if guide else None)
    db.add(cut)
    db.commit()
    ids = (cut.id, reel.id)
    db.close()
    return ids


def test_the_job_is_committed_before_the_message_is_sent(client):
    """A worker that receives the message before the row is visible finds no job and drops it."""
    cut_id, _ = _cut_row(client._session_factory, models.CutStatus.draft)
    events = []
    listener = lambda session: events.append("commit")
    event.listen(Session, "after_commit", listener)
    try:
        with patch("api.routers.cuts.render_cut") as task:
            task.delay.side_effect = lambda jid: events.append("delay")
            client.post(f"/api/cuts/{cut_id}/render")
    finally:
        event.remove(Session, "after_commit", listener)
    assert "commit" in events[:events.index("delay")]


def _card(client, status, **kw):
    cut_id, reel_id = _cut_row(client._session_factory, status, **kw)
    return cut_id, client.get(f"/api/reels/{reel_id}").text


def test_failed_unposted_cut_renders_both_retry_buttons_and_no_posted_claim(client):
    cut_id, html = _card(client, models.CutStatus.failed)
    assert f'hx-post="/api/cuts/{cut_id}/render"' in html
    assert f'hx-post="/api/cuts/{cut_id}/publish"' in html
    assert "was posted" not in html


def test_failed_posted_cut_renders_the_publish_button_only_and_no_double_post_warning(client):
    cut_id, html = _card(client, models.CutStatus.failed, post_id="yt-live")
    assert f'hx-post="/api/cuts/{cut_id}/publish"' in html
    assert f'hx-post="/api/cuts/{cut_id}/render"' not in html
    assert "check the platform first" not in html


def test_failed_cut_without_a_video_has_no_publish_button_and_no_shipping_hint(client):
    cut_id, html = _card(client, models.CutStatus.failed, video=None)
    assert f'hx-post="/api/cuts/{cut_id}/publish"' not in html
    assert "ships the last successfully" not in html


def test_a_prepare_that_commits_and_then_fails_still_does_not_bump_attempts(factory):
    """The documented contract (prepare failure never bumps attempts/started_at) must hold even if a
    future prepare commits (e.g. a budget check that records something)."""
    job_id, reel_id, cut_id = _make(factory)

    def prepare(db, job):
        db.commit()
        raise ValueError("no budget")

    with pytest.raises(ValueError):
        _task("r4.prepare_commits", lambda self, db, job, ctx: None, prepare=prepare)(job_id)
    job, *_ = _read(factory, job_id, reel_id, cut_id)
    assert job.attempts in (0, None) and job.started_at is None


def test_the_paid_call_budget_is_enforced_again_before_each_generation_attempt():
    from worker.tasks.generate import generate_guide
    sf = _real_factory()
    db = sf()
    reel = models.Reel(context="plain prose", niche="football", status=models.ReelStatus.generating)
    db.add(reel)
    db.flush()
    db.add(models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, target_length_s=30.0,
                      status=models.CutStatus.draft))
    job = models.Job(type=models.JobType.generate, reel_id=reel.id, status=models.JobStatus.pending, meta={})
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()
    llm = MagicMock()
    with (
        patch("worker.tasks.common.SessionLocal", sf),
        patch("worker.tasks.generate.paid_call_count", side_effect=[0, 10**6]),   # entry ok, first attempt over cap
        patch("worker.tasks.generate.get_llm_provider", return_value=llm),
        patch("worker.tasks.generate.record_stage"),
    ):
        with pytest.raises(ValueError, match="budget"):
            generate_guide(job_id)
    llm.complete.assert_not_called()
    assert sf().get(models.Job, job_id).status == models.JobStatus.failed


def test_owner_rollback_does_not_trust_a_cached_row_when_no_transaction_is_open(factory):
    """After a milestone heartbeat the session has no transaction, so rollback() expires nothing and the
    reel cached before it is stale if another writer moved it on. The existing stale-owner test opens a
    transaction first, so the rollback alone masks a missing refresh/expire_all."""
    job_id, reel_id, cut_id = _make(factory)

    def body(self, db, job, ctx):
        cached = db.get(models.Reel, reel_id)        # a strong ref: the identity map only holds weak ones
        assert cached.status == models.ReelStatus.generating
        heartbeat(db, job, 50)                       # commit: no open transaction
        assert not db.in_transaction()
        other = factory()
        other.get(models.Reel, reel_id).status = models.ReelStatus.guide_ready
        other.commit()
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _task("r4.stale_no_txn", body)(job_id)
    assert _read(factory, job_id, reel_id, cut_id)[1].status == models.ReelStatus.guide_ready
