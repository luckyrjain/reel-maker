"""Tests for POST /api/cuts/{id}/publish and GET /api/cuts/{id}/publish-status."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.db import get_db
from api.main import app


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        c._session_factory = TestingSessionLocal
        yield c
    app.dependency_overrides.clear()


def _make_cut(session_factory, status, video_path="/data/videos/1/youtube_shorts.mp4"):
    db = session_factory()
    try:
        reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        cut = models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
            status=status, video_path=video_path,
        )
        db.add(cut)
        db.commit()
        return cut.id
    finally:
        db.close()


def test_publish_requires_a_rendered_video(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.approved, video_path=None)
    resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 422


def test_publish_rejects_wrong_status(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.in_review)
    resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 409


def test_publish_rejects_already_publishing(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.publishing)
    resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 409


def test_publish_from_approved_enqueues_job(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.approved)
    with patch("api.routers.cuts.publish_cut") as mock_task:
        resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 200
    mock_task.delay.assert_called_once()

    db = client._session_factory()
    cut = db.get(models.Cut, cut_id)
    assert cut.status.value == "publishing"
    db.close()


def test_publish_retries_from_failed_via_approved(client):
    """A publish failure retries straight from 'approved', no re-render needed."""
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    with patch("api.routers.cuts.publish_cut") as mock_task:
        resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 200
    mock_task.delay.assert_called_once()

    db = client._session_factory()
    cut = db.get(models.Cut, cut_id)
    assert cut.status.value == "publishing"
    db.close()


def test_publish_status_fragment_404_for_missing_job(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.publishing)
    resp = client.get(f"/api/cuts/{cut_id}/publish-status", params={"job_id": 999999})
    assert resp.status_code == 404


def _make_job(session_factory, cut_id, *, status, error=None, progress=0):
    db = session_factory()
    try:
        cut = db.get(models.Cut, cut_id)
        job = models.Job(
            type=models.JobType.publish, reel_id=cut.reel_id, cut_id=cut_id,
            status=status, error=error, progress=progress,
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def test_publish_status_mid_retry_backoff_has_no_broken_retry_button(client):
    """A transient failure resets job.status to "pending" (not "failed") while it
    retries automatically. The fragment must not offer a "Retry publish" button in
    that window — cut.status is still "publishing", so clicking it would 409 and,
    since htmx doesn't swap on a non-2xx response, silently do nothing.
    """
    cut_id = _make_cut(client._session_factory, models.CutStatus.publishing)
    job_id = _make_job(
        client._session_factory, cut_id,
        status=models.JobStatus.pending,
        error="transient failure, retry 1: network down",
    )
    resp = client.get(f"/api/cuts/{cut_id}/publish-status", params={"job_id": job_id})
    assert resp.status_code == 200
    assert "Retry publish" not in resp.text
    assert "retrying automatically" in resp.text.lower()


def test_publish_status_terminal_failure_shows_retry_button(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.publishing)
    job_id = _make_job(
        client._session_factory, cut_id,
        status=models.JobStatus.failed,
        error="No connected youtube account",
    )
    resp = client.get(f"/api/cuts/{cut_id}/publish-status", params={"job_id": job_id})
    assert resp.status_code == 200
    assert "Retry publish" in resp.text
    assert "No connected youtube account" in resp.text


# ── re-rendering a cut that is already posted ────────────────────────────────

def _post_it(session_factory, cut_id, post_id="yt-live"):
    db = session_factory()
    try:
        cut = db.get(models.Cut, cut_id)
        cut.platform_post_id = post_id
        cut.guide = {"beats": []}          # truthy: trigger_render checks for a guide first
        db.commit()
        return cut.reel_id
    finally:
        db.close()


def test_render_refuses_a_cut_that_is_already_posted(client):
    """The video is live. A re-render would leave the cut pointing at that post while a later publish
    'finalizes' against it without uploading the new render."""
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    _post_it(client._session_factory, cut_id)
    with patch("api.routers.cuts.render_cut") as mock_task:
        resp = client.post(f"/api/cuts/{cut_id}/render")
    assert resp.status_code == 409
    assert "posted" in resp.json()["detail"].lower()
    mock_task.delay.assert_not_called()
    db = client._session_factory()
    assert db.get(models.Cut, cut_id).status == models.CutStatus.failed, "the refusal must not move the cut"


def test_render_still_works_for_a_failed_cut_that_was_never_posted(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    db = client._session_factory()
    db.get(models.Cut, cut_id).guide = {"beats": []}
    db.commit()
    with patch("api.routers.cuts.render_cut") as mock_task:
        resp = client.post(f"/api/cuts/{cut_id}/render")
    assert resp.status_code == 200
    mock_task.delay.assert_called_once()


def test_failed_posted_cut_offers_publish_but_not_render(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    reel_id = _post_it(client._session_factory, cut_id)
    html = client.get(f"/api/reels/{reel_id}").text
    assert "Retry publish" in html
    assert "Retry render" not in html
    assert "will not upload again" in html


def test_failed_unposted_cut_still_offers_both_retries(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    db = client._session_factory()
    reel_id = db.get(models.Cut, cut_id).reel_id
    db.close()
    html = client.get(f"/api/reels/{reel_id}").text
    assert "Retry render" in html and "Retry publish" in html


# ── enqueue failures and double clicks ───────────────────────────────────────

def _guide_cut(client, status):
    cut_id = _make_cut(client._session_factory, status)
    db = client._session_factory()
    db.get(models.Cut, cut_id).guide = {"beats": []}
    db.commit()
    db.close()
    return cut_id


def test_render_that_cannot_be_enqueued_fails_fast_and_frees_the_cut(client):
    """Otherwise the cut sits in `rendering`, answering every retry with 409, until the reaper decides
    the message was lost (hours)."""
    cut_id = _guide_cut(client, models.CutStatus.draft)
    with patch("api.routers.cuts.render_cut") as mock_task:
        mock_task.delay.side_effect = ConnectionError("broker down")
        resp = client.post(f"/api/cuts/{cut_id}/render")
    assert resp.status_code == 503
    db = client._session_factory()
    assert db.get(models.Cut, cut_id).status == models.CutStatus.failed
    job = db.query(models.Job).filter(models.Job.cut_id == cut_id).one()
    assert job.status == models.JobStatus.failed and "could not enqueue" in job.error
    with patch("api.routers.cuts.render_cut"):
        assert client.post(f"/api/cuts/{cut_id}/render").status_code == 200   # and it can be retried


def test_publish_that_cannot_be_enqueued_fails_fast_and_frees_the_cut(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.approved)
    with patch("api.routers.cuts.publish_cut") as mock_task:
        mock_task.delay.side_effect = ConnectionError("broker down")
        resp = client.post(f"/api/cuts/{cut_id}/publish")
    assert resp.status_code == 503
    db = client._session_factory()
    assert db.get(models.Cut, cut_id).status == models.CutStatus.failed
    assert db.query(models.Job).filter(models.Job.cut_id == cut_id).one().status == models.JobStatus.failed


def test_the_trigger_routes_lock_the_cut_row_so_a_double_click_serialises(client):
    """Retry render + Retry publish (or a double click) must not both pass the status guards."""
    from sqlalchemy.orm import Session
    cut_id = _guide_cut(client, models.CutStatus.draft)
    seen = []
    real_get = Session.get

    def spy(self, entity, ident, **kwargs):
        if entity is models.Cut:
            seen.append(kwargs.get("with_for_update"))
        return real_get(self, entity, ident, **kwargs)

    with patch.object(Session, "get", spy), patch("api.routers.cuts.render_cut"):
        client.post(f"/api/cuts/{cut_id}/render")
    assert seen and seen[0] is True

    publish_id = _make_cut(client._session_factory, models.CutStatus.approved)
    seen.clear()
    with patch.object(Session, "get", spy), patch("api.routers.cuts.publish_cut"):
        client.post(f"/api/cuts/{publish_id}/publish")
    assert seen and seen[0] is True


def test_failed_unposted_cut_warns_to_check_the_platform_before_retrying(client):
    """max_retries=0 only turns an automatic double post into a manual one unless the operator knows."""
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    db = client._session_factory()
    reel_id = db.get(models.Cut, cut_id).reel_id
    db.close()
    html = client.get(f"/api/reels/{reel_id}").text
    assert "check the platform first" in html


def _assert_committed_before_enqueue(client, trigger, patch_target):
    """Shared shape for the "no transaction open across .delay()" regression test: the job row must
    already be visible to a worker and the router's own session must hold no transaction at the moment
    .delay() runs (a slow broker failure would otherwise outlive an idle-in-transaction timeout).

    A mocked .delay() can't observe transaction state through a second connection: the commit already
    made the row visible regardless of what runs after it. The regression this guards (db.refresh()
    called BEFORE .delay(), which opens a read transaction that then sits open across the broker call)
    only shows up by inspecting the router's own session at the moment .delay() runs.
    """
    from api.db import get_db
    from api.main import app

    sessions = []
    real_override = app.dependency_overrides[get_db]

    def tracking_override():
        gen = real_override()
        db = next(gen)
        sessions.append(db)
        try:
            yield db
        finally:
            try:
                next(gen)
            except StopIteration:
                pass

    seen = {}

    def delay(job_id):
        assert isinstance(job_id, int)
        seen["in_transaction_during_delay"] = sessions[-1].in_transaction()
        other = client._session_factory()
        job = other.get(models.Job, job_id)
        seen["visible"] = job is not None and job.status == models.JobStatus.pending
        other.close()

    app.dependency_overrides[get_db] = tracking_override
    try:
        with patch(patch_target) as mock_task:
            mock_task.delay.side_effect = delay
            resp = trigger()
    finally:
        app.dependency_overrides[get_db] = real_override
    assert resp.status_code == 200, resp.text
    assert seen["visible"] is True
    assert seen["in_transaction_during_delay"] is False


def test_render_status_fragment_offers_a_retry_button_on_failure(client):
    """render_status.html's polling fragment showed only the badge and error text on failure, with no
    way to retry short of a full page reload -- unlike publish_status.html, which already had one."""
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed, video_path=None)
    db = client._session_factory()
    job = models.Job(type=models.JobType.render, reel_id=db.get(models.Cut, cut_id).reel_id,
                     cut_id=cut_id, status=models.JobStatus.failed, error="ffmpeg exit 1")
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()

    resp = client.get(f"/api/cuts/{cut_id}/render-status", params={"job_id": job_id})
    assert resp.status_code == 200
    assert "ffmpeg exit 1" in resp.text
    assert f'hx-post="/api/cuts/{cut_id}/render"' in resp.text
    assert "Retry render" in resp.text


def test_the_render_job_is_committed_before_it_is_enqueued(client):
    cut_id = _guide_cut(client, models.CutStatus.draft)
    _assert_committed_before_enqueue(
        client, lambda: client.post(f"/api/cuts/{cut_id}/render"), "api.routers.cuts.render_cut")


def test_the_publish_job_is_committed_before_it_is_enqueued(client):
    cut_id = _make_cut(client._session_factory, models.CutStatus.approved)
    _assert_committed_before_enqueue(
        client, lambda: client.post(f"/api/cuts/{cut_id}/publish"), "api.routers.cuts.publish_cut")


def test_approve_and_edit_lock_the_cut_row_like_the_trigger_routes(client):
    """An approve racing a render otherwise lets the render do its paid work and then fail its final transition."""
    from sqlalchemy.orm import Session
    cut_id = _make_cut(client._session_factory, models.CutStatus.in_review)
    seen = []
    real_get = Session.get

    def spy(self, entity, ident, **kwargs):
        if entity is models.Cut:
            seen.append(kwargs.get("with_for_update"))
        return real_get(self, entity, ident, **kwargs)

    with patch.object(Session, "get", spy):
        client.post(f"/api/cuts/{cut_id}/approve")
    assert seen and seen[0] is True

    seen.clear()
    editable = _make_cut(client._session_factory, models.CutStatus.in_review)
    with patch.object(Session, "get", spy):
        client.patch(f"/api/cuts/{editable}", data={"caption": "new"})
    assert seen and seen[0] is True


def test_update_cut_reads_the_body_before_taking_the_row_lock(client):
    """The lock must be taken AFTER the body is read: a slow or stalled client would otherwise hold the
    row lock (and a pool connection) for as long as its body trickles in, starving every other request
    waiting on that lock -- not just requests for this cut."""
    from sqlalchemy.orm import Session
    from starlette.requests import Request

    cut_id = _make_cut(client._session_factory, models.CutStatus.in_review)
    order = []
    real_get = Session.get
    real_form = Request.form

    def get_spy(self, entity, ident, **kwargs):
        if entity is models.Cut and kwargs.get("with_for_update"):
            order.append("lock")
        return real_get(self, entity, ident, **kwargs)

    async def form_spy(self, *a, **k):
        order.append("form")
        return await real_form(self, *a, **k)

    with patch.object(Session, "get", get_spy), patch.object(Request, "form", form_spy):
        resp = client.patch(f"/api/cuts/{cut_id}", data={"caption": "new"})
    assert resp.status_code == 200
    assert order == ["form", "lock"]


def test_a_failed_request_is_reported_to_the_operator_not_swallowed_by_htmx(client):
    """htmx does not swap 4xx/5xx responses, so without a handler the 503 for an unqueueable job
    looks like nothing happened."""
    assert client.get("/static/htmx-errors.js").status_code == 200
    assert "htmx:responseError" in client.get("/static/htmx-errors.js").text
    assert "/static/htmx-errors.js" in client.get("/").text
    cut_id = _make_cut(client._session_factory, models.CutStatus.failed)
    reel_id = client._session_factory().get(models.Cut, cut_id).reel_id
    assert "/static/htmx-errors.js" in client.get(f"/api/reels/{reel_id}").text
