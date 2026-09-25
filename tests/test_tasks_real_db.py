"""Task behaviour that mocks cannot see: durable state after a failure, the arguments actually
passed on, and what the database holds. Runs the real tasks through job_task on in-memory SQLite."""
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from engine.publish.base import PublishResult


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False)
    with patch("worker.tasks.common.SessionLocal", session_factory):
        yield session_factory


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


def test_the_post_id_survives_a_failure_after_the_upload(factory):
    """The video is live and irreversible: if anything after the upload fails, or the job is reaped,
    the id must already be durable, or a retry posts it a second time."""
    from worker.tasks.publish import publish_cut
    job_id, cut_id = _publish_setup(factory)
    with (
        patch("worker.tasks.publish.get_publisher") as get_publisher,
        patch("worker.tasks.publish.record_stage"),
        patch("worker.tasks.publish.transition", side_effect=ValueError("bad transition")),
    ):
        get_publisher.return_value.publish.return_value = PublishResult(platform_post_id="yt-1", url="u")
        with pytest.raises(ValueError):
            publish_cut(job_id)
    db = factory()
    assert db.get(models.Cut, cut_id).platform_post_id == "yt-1"
    assert db.get(models.Job, job_id).status == models.JobStatus.failed


def test_a_successful_publish_persists_published_at_and_sends_the_built_caption(factory):
    """The Wikipedia attribution block reaches the platform only through the built caption."""
    from worker.tasks.publish import publish_cut
    job_id, cut_id = _publish_setup(factory)
    with (
        patch("worker.tasks.publish.get_publisher") as get_publisher,
        patch("worker.tasks.publish.record_stage"),
        patch("worker.tasks.publish.build_published_caption", return_value="cap + attribution"),
    ):
        get_publisher.return_value.publish.return_value = PublishResult(platform_post_id="yt-2", url="u")
        publish_cut(job_id)
    assert get_publisher.return_value.publish.call_args.kwargs["caption"] == "cap + attribution"
    cut = factory().get(models.Cut, cut_id)
    assert cut.published_at is not None and cut.platform_post_id == "yt-2"
    assert cut.status == models.CutStatus.published


def test_captions_upload_failure_does_not_fail_publish_cut_or_platform_post_id(factory, tmp_path):
    """A best-effort YouTube captions upload failure must never fail publish_cut —
    the video is already live by the time this runs. CRITICAL (design review
    trigger, easy to omit): the failure must still show up as a real
    StageEvent(stage="captions_upload", ok=False, detail["error"]=...) row, not
    silently recorded as ok=True (record_stage's default) — see
    engine/publish/youtube.py::YouTubePublisher.publish()'s record_stage
    composition and CLAUDE.md's Key conventions entry for this rule. Mutation-
    tested: forcing _upload_captions to raise (below) and reading the resulting
    row back for real, not just trusting the diff."""
    from engine.publish.youtube import YouTubePublisher
    from worker.tasks.publish import publish_cut

    job_id, cut_id = _publish_setup(factory)
    video_file = tmp_path / "v.mp4"
    video_file.write_bytes(b"fake mp4 bytes")
    db = factory()
    cut = db.get(models.Cut, cut_id)
    cut.video_path = str(video_file)
    cut.subtitle_path = "/data/videos/10/youtube_shorts.srt"
    db.commit()
    db.close()

    init_resp = MagicMock()
    init_resp.raise_for_status.return_value = None
    init_resp.headers = {"Location": "https://upload.example.com/session123"}
    upload_resp = MagicMock()
    upload_resp.raise_for_status.return_value = None
    upload_resp.json.return_value = {"id": "yt-real-id"}

    with (
        patch("worker.tasks.publish.get_publisher", return_value=YouTubePublisher()),
        patch("engine.publish.youtube.httpx.post", return_value=init_resp),
        patch("engine.publish.youtube.httpx.put", return_value=upload_resp),
        patch("engine.publish.youtube._upload_captions", side_effect=RuntimeError("captions API down")),
    ):
        publish_cut(job_id)  # must not raise

    db = factory()
    cut = db.get(models.Cut, cut_id)
    job = db.get(models.Job, job_id)
    # (a) the job succeeded — a captions-only failure never fails publish_cut.
    assert job.status == models.JobStatus.done
    assert job.error is None
    # (b) the video's post id/published_at are unaffected by the captions failure.
    assert cut.platform_post_id == "yt-real-id"
    assert cut.published_at is not None
    assert cut.status == models.CutStatus.published

    # (c) the failure is truthfully recorded, not silently swallowed.
    ev = (
        db.query(models.StageEvent)
        .filter(models.StageEvent.stage == "captions_upload", models.StageEvent.cut_id == cut_id)
        .one()
    )
    assert ev.ok is False
    assert "captions API down" in ev.detail["error"]
    assert ev.provider == "youtube"


def test_captions_upload_success_records_a_stage_event_with_ok_true(factory, tmp_path):
    """Sanity-check the happy path alongside the failure test above: a successful
    captions upload records ok=True, not just "no exception happened to propagate"."""
    from engine.publish.youtube import YouTubePublisher
    from worker.tasks.publish import publish_cut

    job_id, cut_id = _publish_setup(factory)
    video_file = tmp_path / "v.mp4"
    video_file.write_bytes(b"fake mp4 bytes")
    db = factory()
    cut = db.get(models.Cut, cut_id)
    cut.video_path = str(video_file)
    cut.subtitle_path = "/data/videos/10/youtube_shorts.srt"
    db.commit()
    db.close()

    init_resp = MagicMock()
    init_resp.raise_for_status.return_value = None
    init_resp.headers = {"Location": "https://upload.example.com/session123"}
    upload_resp = MagicMock()
    upload_resp.raise_for_status.return_value = None
    upload_resp.json.return_value = {"id": "yt-real-id-2"}

    with (
        patch("worker.tasks.publish.get_publisher", return_value=YouTubePublisher()),
        patch("engine.publish.youtube.httpx.post", return_value=init_resp),
        patch("engine.publish.youtube.httpx.put", return_value=upload_resp),
        patch("engine.publish.youtube._upload_captions", return_value=None) as mock_upload,
    ):
        publish_cut(job_id)

    mock_upload.assert_called_once()
    db = factory()
    ev = (
        db.query(models.StageEvent)
        .filter(models.StageEvent.stage == "captions_upload", models.StageEvent.cut_id == cut_id)
        .one()
    )
    assert ev.ok is True


def test_no_captions_upload_attempted_when_subtitle_path_is_unset(factory, tmp_path):
    """No SRT means nothing to attempt or record — no captions_upload StageEvent."""
    from engine.publish.youtube import YouTubePublisher
    from worker.tasks.publish import publish_cut

    job_id, cut_id = _publish_setup(factory)  # subtitle_path is None by default
    video_file = tmp_path / "v.mp4"
    video_file.write_bytes(b"fake mp4 bytes")
    db = factory()
    cut = db.get(models.Cut, cut_id)
    cut.video_path = str(video_file)
    db.commit()
    db.close()

    init_resp = MagicMock()
    init_resp.raise_for_status.return_value = None
    init_resp.headers = {"Location": "https://upload.example.com/session123"}
    upload_resp = MagicMock()
    upload_resp.raise_for_status.return_value = None
    upload_resp.json.return_value = {"id": "yt-real-id-3"}

    with (
        patch("worker.tasks.publish.get_publisher", return_value=YouTubePublisher()),
        patch("engine.publish.youtube.httpx.post", return_value=init_resp),
        patch("engine.publish.youtube.httpx.put", return_value=upload_resp),
        patch("engine.publish.youtube._upload_captions") as mock_upload,
    ):
        publish_cut(job_id)

    mock_upload.assert_not_called()
    db = factory()
    count = (
        db.query(models.StageEvent)
        .filter(models.StageEvent.stage == "captions_upload", models.StageEvent.cut_id == cut_id)
        .count()
    )
    assert count == 0


def test_enrich_enqueues_generate_with_the_id_of_the_job_row_it_created(factory):
    """generate_guide.delay(None) would silently break the enrich -> generate chain."""
    from worker.tasks.enrich_context import enrich_context
    db = factory()
    reel = models.Reel(context="c", status=models.ReelStatus.enriching)
    db.add(reel)
    db.flush()
    job = models.Job(type=models.JobType.enrich, reel_id=reel.id, status=models.JobStatus.pending,
                     meta={"generation_path": "auto"})
    db.add(job)
    db.commit()
    job_id, reel_id = job.id, reel.id
    db.close()
    with (
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(80, [])),
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.record_stage"),
        patch("worker.tasks.enrich_context.generate_guide") as generate,
    ):
        enrich_context(job_id)
    (generate_job_id,), _ = generate.delay.call_args
    db = factory()
    generate_job = db.get(models.Job, generate_job_id)
    assert generate_job is not None
    assert generate_job.type == models.JobType.generate and generate_job.reel_id == reel_id
    assert db.get(models.Reel, reel_id).status == models.ReelStatus.generating


def test_abandon_generate_leaves_a_reel_that_moved_on(factory):
    from worker.tasks.enrich_context import _abandon_generate
    db = factory()
    reel = models.Reel(context="c", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    enrich = models.Job(type=models.JobType.enrich, reel_id=reel.id, status=models.JobStatus.done)
    orphan = models.Job(type=models.JobType.generate, reel_id=reel.id, status=models.JobStatus.pending)
    db.add_all([enrich, orphan])
    db.commit()
    _abandon_generate(db, enrich, orphan.id)
    db.commit()
    assert db.get(models.Reel, reel.id).status == models.ReelStatus.guide_ready
