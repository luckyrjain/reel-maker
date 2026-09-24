"""Regression tests found by mutation testing (round 3). Each docstring names the mutants it kills.

Every fixture here makes job id, reel id and cut id DIFFERENT numbers where it matters: with all three equal to 1
an id mix-up passes unnoticed."""
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Reject, Retry
from sqlalchemy import exc as sa_exc
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
# Only plain helper functions, not fixtures: importing a fixture by name and then using that same
# name as a test's parameter makes ruff flag every such test as "redefining" the import (F811). Each
# file in this suite defines its own factory/file_factory instead, matching repo convention.
from test_job_lifecycle import _make, _raises, _read, _set_job, _task, app  # noqa: F401  (app: registers dummy tasks)
from worker.tasks import common
from worker.tasks.common import _error_text, heartbeat, rollback_owner

_MIN = timedelta(minutes=1)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False)
    with patch("worker.tasks.common.SessionLocal", session_factory):
        yield session_factory


@pytest.fixture
def file_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/r3.db")
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


def _offset_ids(factory):
    """Burn ids so job.id, reel.id and cut.id are all different numbers (else id mix-ups pass)."""
    db = factory()
    r1 = models.Reel(context="decoy", status=models.ReelStatus.draft)
    r2 = models.Reel(context="decoy", status=models.ReelStatus.draft)
    db.add_all([r1, r2])
    db.flush()
    cuts = [models.Cut(reel_id=r1.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.draft)
            for _ in range(4)]
    db.add_all(cuts)
    db.flush()
    db.add(models.Job(type=models.JobType.render, reel_id=r1.id, cut_id=cuts[0].id, status=models.JobStatus.done))
    db.commit()
    db.close()


def _ids_differ(job_id, reel_id, cut_id):
    assert len({job_id, reel_id, cut_id}) == 3


# --- H3: heartbeat() must COMMIT (visible to another connection) -----------------------
def test_heartbeat_commits_progress_and_body_writes_where_another_connection_can_see_them(file_factory):
    """kills H3 (no commit)."""
    job_id, reel_id, cut_id = _make(file_factory)
    seen = {}

    def body(self, db, job, ctx):
        db.get(models.Cut, cut_id).caption = "written before the heartbeat"
        heartbeat(db, job, 42)
        other = file_factory()
        seen["progress"] = other.get(models.Job, job_id).progress
        seen["caption"] = other.get(models.Cut, cut_id).caption
        other.close()

    with patch("worker.tasks.common.SessionLocal", file_factory):
        _task("t.hbcommit", body)(job_id)
    assert seen == {"progress": 42, "caption": "written before the heartbeat"}


# --- H9 / R11 / R12 / A1 / M9d: id namespaces -------------------------------------------
def test_heartbeat_and_owner_rollback_use_the_right_id_when_job_reel_and_cut_ids_all_differ(factory):
    """kills H9, R11, R12, A1 (wrong-id keyed writes)."""
    _offset_ids(factory)
    job_id, reel_id, cut_id = _make(factory, models.JobType.render, reel_status=models.ReelStatus.guide_ready,
                                    cut_status=models.CutStatus.rendering)
    _ids_differ(job_id, reel_id, cut_id)

    def body(self, db, job, ctx):
        heartbeat(db, job, 55)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _task("t.ids", body, job_type="render")(job_id)
    job, reel, cut = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.failed and job.progress == 55
    assert cut.status == models.CutStatus.failed and reel.status == models.ReelStatus.guide_ready


def test_reaper_uses_the_job_id_not_the_reel_id(factory):
    """kills M9d."""
    from test_maintenance import _make as mk, _state
    from worker.tasks.maintenance import reap_stuck_jobs
    with patch("worker.tasks.maintenance.SessionLocal", factory):
        _offset_ids(factory)
        ids = mk(factory, models.JobType.generate, job_status=models.JobStatus.running)
        _ids_differ(*ids)
        reap_stuck_jobs()
    job, reel, _ = _state(factory, *ids)
    assert job.status == models.JobStatus.failed and reel.status == models.ReelStatus.failed


# --- S1: failure must roll back the body's flushed/added rows --------------------------
def test_failure_discards_rows_the_body_added(factory):
    """kills S1 (no rollback at the start of _settle_failure): expire_all() does not drop added rows."""
    job_id, reel_id, cut_id = _make(factory)

    def body(self, db, job, ctx):
        db.add(models.Reel(context="orphan", status=models.ReelStatus.draft))
        db.flush()
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _task("t.orphan", body)(job_id)
    assert factory().query(models.Reel).filter(models.Reel.context == "orphan").count() == 0


# --- R4a/R4b: rollback_owner re-reads under FOR UPDATE ----------------------------------
def test_rollback_owner_locks_and_refreshes_the_row():
    """kills R4a (no FOR UPDATE); SQLite ignores FOR UPDATE so only a spy can see it."""
    db = MagicMock()
    row = MagicMock()
    row.status = "generating"
    db.get.return_value = row
    job = MagicMock(reel_id=7, cut_id=None)
    with patch("worker.tasks.common.transition"):
        rollback_owner(db, job, "reel", {"generating"})
    db.refresh.assert_called_once_with(row, with_for_update=True)


def test_rollback_owner_does_not_trust_the_sessions_cached_row(factory):
    """kills R4b even without the expire_all() in _settle_failure."""
    job_id, reel_id, cut_id = _make(factory)
    db = factory()
    job = db.get(models.Job, job_id)
    assert db.get(models.Reel, reel_id).status == models.ReelStatus.generating        # cached
    other = factory()
    other.get(models.Reel, reel_id).status = models.ReelStatus.guide_ready
    other.commit()
    rollback_owner(db, job, "reel", {"generating"})
    db.commit()
    assert factory().get(models.Reel, reel_id).status == models.ReelStatus.guide_ready


# --- E5*: _error_text regex boundaries --------------------------------------------------
def test_error_text_redacts_multiline_and_nested_bracket_parameters_and_keeps_the_rest():
    """kills E5a (greedy), E5c (no $), E5d (no lookahead), E5e, E5g."""
    multi = "boom\n[SQL: UPDATE t SET c=%(c)s]\n[parameters: {'c': 'line1\\nSECRET2'}]\n(Background: https://x)"   # repr() escapes \n
    t = _error_text(ValueError(multi))
    assert "SECRET2" not in t and "(Background: https://x)" in t
    assert "[parameters: <redacted>]" in t

    nested = "[parameters: {'a': [1, 'SECRET3']}]\ntail [kept]"
    t = _error_text(ValueError(nested))
    assert "SECRET3" not in t and t.endswith("tail [kept]") and "}]" not in t

    at_end = "boom [parameters: {'a': 'SECRET4'}]"           # no trailing newline
    assert "SECRET4" not in _error_text(ValueError(at_end))

    prose = "invalid parameters: none given ]\nnext line"      # not the SQLAlchemy block
    assert _error_text(ValueError(prose)) == prose


# --- S3b: a non-transient error before the claim must not be retried -------------------
def test_a_non_transient_error_before_the_claim_is_not_retried(factory):
    """kills S3b."""
    job_id, *_ = _make(factory)
    task = _task("t.preclaim_nontransient", lambda self, db, job, ctx: None)

    def boom(*a, **k):
        s = factory(*a, **k)
        s.get = MagicMock(side_effect=ValueError("bad row"))
        return s

    with patch("worker.tasks.common.SessionLocal", boom), \
            patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(ValueError, match="bad row"):
            task(job_id)
    retry.assert_not_called()


# --- S6b: retry is not raised when the reset lost the CAS ---------------------------------
def test_no_retry_is_raised_when_the_job_was_reaped_before_the_reset(factory):
    """kills S6b (return ok -> True)."""
    job_id, *_ = _make(factory)

    def body(self, db, job, ctx):
        _set_job(factory, job_id, status=models.JobStatus.failed, error="reaped")
        raise ConnectionError("blip")

    task = _task("t.reaped_then_transient", body)
    with patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(ConnectionError):
            task(job_id)
    retry.assert_not_called()
    assert factory().get(models.Job, job_id).error == "reaped"


# --- J12e: a shutdown must not commit the body's half-done writes ------------------------
def test_shutdown_fail_does_not_commit_the_bodys_uncommitted_writes(factory):
    """kills J12e (no rollback before the shutdown-failure commit).

    Round 4 changed shutdown from releasing the job to pending (no message would ever come back for
    it) to failing it at once; this test now pins the status at `failed`, not `pending`."""
    job_id, reel_id, cut_id = _make(factory)

    def body(self, db, job, ctx):
        db.get(models.Cut, cut_id).caption = "half-done"
        db.add(models.Reel(context="orphan2", status=models.ReelStatus.draft))
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        _task("t.shutdown_discard", body)(job_id)
    db = factory()
    assert db.get(models.Cut, cut_id).caption is None
    assert db.query(models.Reel).filter(models.Reel.context == "orphan2").count() == 0
    assert db.get(models.Job, job_id).status == models.JobStatus.failed


# --- F3b/F8/F5b/F5c: refused retry ------------------------------------------------------
def test_a_refused_retry_leaves_a_job_a_sibling_already_took(factory):
    """kills F3b, F8 (unconditional stamp / owner rollback)."""
    job_id, reel_id, cut_id = _make(factory)
    task = _task("t.reject_sibling", _raises(ConnectionError("blip")))

    def refuse(*a, **k):
        _set_job(factory, job_id, status=models.JobStatus.running)   # redelivered elsewhere already
        raise Reject(ConnectionError("broker"), requeue=False)

    with patch.object(task, "retry", side_effect=refuse):
        with pytest.raises(Reject):
            task(job_id)
    job, reel, _ = _read(factory, job_id, reel_id, cut_id)
    assert job.status == models.JobStatus.running
    assert reel.status == models.ReelStatus.generating


def test_a_refused_retry_error_is_sanitised_and_truncated(factory):
    """kills F5b, F5c."""
    job_id, *_ = _make(factory)
    task = _task("t.reject_text", _raises(ConnectionError("a\x00b [parameters: {'k': 'SECRET5'}]\n" + "x" * 5000)))
    with patch.object(task, "retry", side_effect=Reject(ValueError("broker"), requeue=False)):
        with pytest.raises(Reject):
            task(job_id)
    err = factory().get(models.Job, job_id).error
    assert len(err) <= 2000 and "\x00" not in err
    assert err.startswith("could not schedule retry: ") and "SECRET5" not in err


# --- S10b: after_commit without a cleanup hook ------------------------------------------
def test_after_commit_failure_without_a_cleanup_hook_still_fails_the_job(factory):
    """kills S10b."""
    job_id, *_ = _make(factory)

    def bad_after_commit(result):
        raise ConnectionError("enqueue failed")

    task = _task("t.nohook", lambda self, db, job, ctx: 1, after_commit=bad_after_commit)
    with pytest.raises(ConnectionError):
        task(job_id)
    assert factory().get(models.Job, job_id).status == models.JobStatus.failed


# --- J8l: a failing done-stamp commit is a failure of an unfinished job ------------------
def test_a_transient_error_on_the_done_stamp_commit_is_retried_not_treated_as_done(factory):
    """kills J8l (committed = True before the commit)."""
    job_id, *_ = _make(factory)
    state = {"stamped": False, "raised": False}
    real_advance = common._advance

    def spy(db, jid, frm, values):
        if values.get("status") == models.JobStatus.done:
            state["stamped"] = True
        return real_advance(db, jid, frm, values)

    class Flaky(sessionmaker(bind=factory.kw["bind"]).class_):
        def commit(self):
            if state["stamped"] and not state["raised"]:
                state["raised"] = True
                raise sa_exc.OperationalError("COMMIT", {}, Exception("connection lost"))
            return super().commit()

    flaky = sessionmaker(bind=factory.kw["bind"], class_=Flaky, autoflush=False)
    task = _task("t.donecommit", lambda self, db, job, ctx: None)
    with patch("worker.tasks.common.SessionLocal", flaky), patch("worker.tasks.common._advance", spy), \
            patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(Retry):
            task(job_id)
    retry.assert_called_once()
    assert factory().get(models.Job, job_id).status == models.JobStatus.pending


# --- J4c: daemon thread ------------------------------------------------------------------
def test_the_heartbeat_thread_is_a_daemon(factory):
    """kills J4c."""
    job_id, *_ = _make(factory)
    created = []
    real = threading.Thread

    def spy(*a, **k):
        t = real(*a, **k)
        created.append(t)
        return t

    with patch("worker.tasks.common.threading.Thread", spy):
        _task("t.daemon", lambda self, db, job, ctx: None)(job_id)
    assert created and all(t.daemon for t in created)


# --- J4h: the thread beats while prepare() runs ----------------------------------------
def test_the_heartbeat_thread_runs_during_prepare(file_factory):
    """kills J4h (thread started only after prepare)."""
    job_id, *_ = _make(file_factory)
    seen = {}

    def prepare(db, job):
        def hb():
            o = file_factory()
            try:
                return o.get(models.Job, job_id).heartbeat_at
            finally:
                o.close()
        seen["a"] = hb()
        time.sleep(0.6)
        seen["b"] = hb()

    with patch("worker.tasks.common.SessionLocal", file_factory), \
            patch("worker.tasks.common.HEARTBEAT_INTERVAL_S", 0.1):
        _task("t.prep_beat", lambda self, db, job, ctx: None, prepare=prepare)(job_id)
    assert seen["b"] > seen["a"]


# --- maintenance ------------------------------------------------------------------------
def test_reap_stuck_jobs_passes_a_clause_that_backs_off_from_a_job_that_beat_after_the_select(factory):
    """kills M2c (running clause not re-checked in the UPDATE)."""
    from test_maintenance import _make as mk, _state
    from worker.tasks import maintenance
    with patch("worker.tasks.maintenance.SessionLocal", factory):
        ids = mk(factory, models.JobType.render, job_status=models.JobStatus.running,
                 cut_status=models.CutStatus.rendering, reel_status=models.ReelStatus.guide_ready)
        real = maintenance._reap_one

        def beat_first(db, job_id, seen, reason, clause):
            o = factory()
            o.query(models.Job).filter(models.Job.id == job_id).update(
                {"heartbeat_at": datetime.now(timezone.utc)}, synchronize_session=False)
            o.commit()
            return real(db, job_id, seen, reason, clause)

        with patch.object(maintenance, "_reap_one", side_effect=beat_first):
            maintenance.reap_stuck_jobs()
    job, _, cut = _state(factory, *ids)
    assert job.status == models.JobStatus.running and cut.status == models.CutStatus.rendering


def test_reap_stuck_jobs_passes_a_clause_that_backs_off_from_a_pending_job_that_was_just_reset(factory):
    """kills M3e."""
    from test_maintenance import _make as mk, _state
    from worker.tasks import maintenance
    with patch("worker.tasks.maintenance.SessionLocal", factory):
        ids = mk(factory, models.JobType.generate, job_status=models.JobStatus.pending)
        real = maintenance._reap_one

        def touch_first(db, job_id, seen, reason, clause):
            o = factory()
            o.query(models.Job).filter(models.Job.id == job_id).update(
                {"updated_at": datetime.now(timezone.utc)}, synchronize_session=False)
            o.commit()
            return real(db, job_id, seen, reason, clause)

        with patch.object(maintenance, "_reap_one", side_effect=touch_first):
            maintenance.reap_stuck_jobs()
    assert _state(factory, *ids)[0].status == models.JobStatus.pending


@pytest.mark.parametrize("minutes_ago, reaped", [(4.5, False), (5.5, True)])
def test_running_threshold_is_about_five_minutes(factory, minutes_ago, reaped):
    """kills M1a, M1b, M1d."""
    from test_maintenance import _make as mk, _state
    from worker.tasks.maintenance import reap_stuck_jobs
    now = datetime.now(timezone.utc)
    with patch("worker.tasks.maintenance.SessionLocal", factory):
        ids = mk(factory, models.JobType.render, job_status=models.JobStatus.running,
                 heartbeat_at=now - timedelta(minutes=minutes_ago), updated_at=now)
        reap_stuck_jobs()
    assert (_state(factory, *ids)[0].status == models.JobStatus.failed) is reaped


def test_reap_one_returns_true_when_it_failed_the_job(factory):
    """kills M7d."""
    from test_maintenance import _make as mk
    from worker.tasks.maintenance import _reap_one
    ids = mk(factory, models.JobType.render, job_status=models.JobStatus.running)
    cutoff = datetime.now(timezone.utc) - 5 * _MIN
    assert _reap_one(factory(), ids[0], models.JobStatus.running, "r", models.Job.heartbeat_at < cutoff) is True


# --- enrich abandon blast radius ---------------------------------------------------------
def test_abandon_generate_fails_only_the_named_job(factory):
    """kills EN1g (UPDATE without an id filter would fail EVERY pending job)."""
    from worker.tasks.enrich_context import _abandon_generate
    db = factory()
    reel = models.Reel(context="c", status=models.ReelStatus.generating)
    other_reel = models.Reel(context="c2", status=models.ReelStatus.generating)
    db.add_all([reel, other_reel])
    db.flush()
    enrich = models.Job(type=models.JobType.enrich, reel_id=reel.id, status=models.JobStatus.done)
    orphan = models.Job(type=models.JobType.generate, reel_id=reel.id, status=models.JobStatus.pending)
    bystander = models.Job(type=models.JobType.generate, reel_id=other_reel.id, status=models.JobStatus.pending)
    db.add_all([enrich, orphan, bystander])
    db.commit()
    _abandon_generate(db, enrich, orphan.id)
    db.commit()
    assert db.get(models.Job, bystander.id).status == models.JobStatus.pending
    assert db.get(models.Job, orphan.id).status == models.JobStatus.failed


def test_prepare_generate_refuses_every_non_generating_reel_state():
    """kills GN1c."""
    from worker.tasks.generate import _prepare_generate
    for st in ("enriching", "guide_ready", "failed", "draft"):
        db = MagicMock()
        reel = MagicMock(id=1)
        reel.status.value = st
        db.get.return_value = reel
        with pytest.raises(ValueError, match="not 'generating'"):
            _prepare_generate(db, MagicMock(reel_id=1))


# --- celery route ------------------------------------------------------------------------
def test_reaper_is_routed_to_the_generation_queue_not_the_single_slot_render_queue():
    """kills CE2."""
    from worker.celery_app import celery_app
    assert celery_app.conf.task_routes["worker.tasks.maintenance.reap_stuck_jobs"]["queue"] == "generation"


# --- P5a: no transaction is left open across the upload ---------------------------------
def _publish_setup(factory):
    db = factory()
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


def test_no_transaction_is_open_while_the_upload_runs(factory):
    """kills P5a (the read transaction would sit idle for the whole upload)."""
    from engine.publish.base import PublishResult
    from worker.tasks.publish import publish_cut
    job_id, cut_id = _publish_setup(factory)
    open_during_upload = []

    def upload(cut, credential, db, caption):
        open_during_upload.append(db.in_transaction())
        return PublishResult(platform_post_id="yt-1", url="u")

    with patch("worker.tasks.publish.get_publisher") as gp, patch("worker.tasks.publish.record_stage"):
        gp.return_value.publish.side_effect = upload
        publish_cut(job_id)
    assert open_during_upload == [False]


def test_a_publish_job_reaped_before_the_upload_never_uploads(factory):
    """kills P5e+P5f together (the two fences in front of the irreversible call)."""
    from worker.tasks.publish import publish_cut
    job_id, cut_id = _publish_setup(factory)

    def reaped_during_gate(db, cid):
        _set_job(factory, job_id, status=models.JobStatus.failed, error="reaped")

    with patch("worker.tasks.publish.get_publisher") as gp, patch("worker.tasks.publish.record_stage"), \
            patch("worker.tasks.publish.assert_safe_to_publish", side_effect=reaped_during_gate):
        with pytest.raises(common.JobLost):
            publish_cut(job_id)
    gp.return_value.publish.assert_not_called()


# --- AS1-3: resolve_or_reuse ends its transactions ---------------------------------------
def test_resolve_or_reuse_holds_no_transaction_across_the_network_call_or_after_returning(tmp_path):
    """kills AS1 (open across the search) and AS2/AS3 (pins left uncommitted and idle)."""
    from engine.render.asset_sourcer import SourcedAsset, resolve_or_reuse
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()   # job_task sessions never expire on commit
    reel = models.Reel(context="c", status=models.ReelStatus.draft)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, target_length_s=45.0,
                     status=models.CutStatus.draft)
    db.add(cut)
    db.flush()
    seen = []

    class Sourcer:
        def search(self, query, min_duration_s):
            seen.append(db.in_transaction())
            return SourcedAsset(source="pexels", source_ref="v1", local_path=tmp_path / "v1.mp4",
                                license_str="pexels_free", safe_to_publish=True, duration_s=10.0)

    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=Sourcer())
    assert seen == [False], "a transaction was open across the network call"
    assert not db.in_transaction(), "the pin insert was left uncommitted"
    db.close()
    check = sessionmaker(bind=engine)()
    assert check.query(models.CutAsset).count() == 1


# --- TP: template assertions must match the BUTTONS, not the prose -----------------------
from api.db import get_db
from api.main import app as fastapi_app
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    with TestClient(fastapi_app) as c:
        c._session_factory = TestingSessionLocal
        yield c
    fastapi_app.dependency_overrides.clear()


def _make_cut(session_factory, status, video_path="/data/videos/1/youtube_shorts.mp4"):
    db = session_factory()
    try:
        reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
                         status=status, video_path=video_path)
        db.add(cut)
        db.commit()
        return cut.id
    finally:
        db.close()


def _post_it(session_factory, cut_id, post_id="yt-live"):
    db = session_factory()
    try:
        cut = db.get(models.Cut, cut_id)
        cut.platform_post_id = post_id
        cut.guide = {"beats": []}
        db.commit()
        return cut.reel_id
    finally:
        db.close()


def test_failed_posted_cut_has_a_publish_button_and_no_render_button(client):
    """kills the vacuous-assertion gap: 'Retry publish' also appears in the prose."""
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    reel_id = _post_it(client._session_factory, cut_id)
    html = client.get(f"/api/reels/{reel_id}").text
    assert f'hx-post="/api/cuts/{cut_id}/publish"' in html
    assert f'hx-post="/api/cuts/{cut_id}/render"' not in html
    assert "yt-live" in html                       # kills TP7 (id not shown)
    assert "ships the last successfully" not in html   # kills TP2 (hint hidden for a posted cut)


def test_failed_unposted_cut_has_both_buttons(client):
    """kills TP5 (render button hidden) - 'Retry render' also appears in the prose."""
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    db = client._session_factory()
    reel_id = db.get(models.Cut, cut_id).reel_id
    db.close()
    html = client.get(f"/api/reels/{reel_id}").text
    assert f'hx-post="/api/cuts/{cut_id}/render"' in html
    assert f'hx-post="/api/cuts/{cut_id}/publish"' in html
    assert "ships the last successfully" in html


@pytest.mark.parametrize("minutes, reaped", [(239.5, False), (240.5, True)])
@pytest.mark.parametrize("job_type", [models.JobType.enrich, models.JobType.generate,
                                      models.JobType.render, models.JobType.publish])
def test_the_pending_threshold_is_exactly_240_minutes_for_every_job_type(factory, job_type, minutes, reaped):
    """A job legitimately queues for hours; a reaped job is terminal. Pins the boundary, not a bracket."""
    from test_maintenance import _make as mk, _state
    from worker.tasks.maintenance import reap_stuck_jobs
    with patch("worker.tasks.maintenance.SessionLocal", factory):
        ids = mk(factory, job_type, job_status=models.JobStatus.pending, reel_status=models.ReelStatus.enriching,
                 updated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes))
        reap_stuck_jobs()
    assert (_state(factory, *ids)[0].status == models.JobStatus.failed) is reaped


def test_the_retry_carries_the_original_exception(factory):
    """kills J11e (self.retry without exc= loses the traceback / final error)."""
    job_id, *_ = _make(factory)
    err = ConnectionError("blip")
    task = _task("t.retry_exc", _raises(err))
    with patch.object(task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(Retry):
            task(job_id)
    assert retry.call_args.kwargs["exc"] is err
