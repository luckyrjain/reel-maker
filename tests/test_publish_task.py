"""Tests for the publish_cut task's failure handling and success-path cleanup."""
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry

from api import models
from api.state import CUT_TRANSITIONS
from engine.publish.base import PublishResult


def _job(job_id=1, cut_id=5, reel_id=10):
    job = MagicMock()
    job.id = job_id
    job.cut_id = cut_id
    job.reel_id = reel_id
    job.status = models.JobStatus.pending
    job.attempts = 0
    job.progress = 0
    job.error = "stale error from a previous attempt"
    return job


def _cut():
    cut = MagicMock()
    cut.id = 5
    cut.reel_id = 10
    cut.video_path = "/data/videos/10/youtube_shorts.mp4"
    cut.platform_post_id = None   # not posted yet (a bare MagicMock would be truthy)
    cut.platform.value = "youtube_shorts"
    cut.status.value = "publishing"
    return cut


def _unsafe_row(beat_index=0, source="wikipedia", source_ref="x", license_="CC BY-SA"):
    """A (CutAsset, Asset) row shaped like what gate.py's join query returns."""
    cut_asset = MagicMock()
    cut_asset.beat_index = beat_index
    asset = MagicMock()
    asset.source = source
    asset.source_ref = source_ref
    asset.license = license_
    asset.safe_to_publish = False
    return (cut_asset, asset)


def _db_with(job, cut, unsafe_rows=(), credential=MagicMock()):
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else cut
    db.query.return_value.join.return_value.filter.return_value.all.return_value = list(unsafe_rows)
    db.query.return_value.filter.return_value.first.return_value = credential
    return db


def test_missing_cut_fails_job_with_actionable_message():
    from worker.tasks.publish import publish_cut

    job = _job(cut_id=77)
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else None

    with patch("worker.tasks.common.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="Cut 77 no longer exists"):
            publish_cut(1)

    assert job.status == models.JobStatus.failed
    assert "Cut 77 no longer exists" in job.error


def test_missing_video_path_fails_deterministically():
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    cut.video_path = None
    db = _db_with(job, cut)

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch.object(publish_cut, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(ValueError, match="no rendered video"):
            publish_cut(1)

    mock_retry.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_unsafe_asset_blocks_publish():
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    db = _db_with(job, cut, unsafe_rows=[_unsafe_row()])

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.publish.get_publisher") as mock_get_publisher,
        patch.object(publish_cut, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(ValueError, match="not cleared for publishing"):
            publish_cut(1)

    mock_retry.assert_not_called()
    mock_get_publisher.assert_not_called()
    mock_get_publisher.return_value.publish.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_missing_credential_fails_with_actionable_message():
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    db = _db_with(job, cut, credential=None)

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch.object(publish_cut, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(ValueError, match="No connected youtube account"):
            publish_cut(1)

    mock_retry.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_deterministic_publisher_failure_transitions_cut_to_failed():
    """e.g. TikTokPublisher's NotImplementedError — must fail once, no retry."""
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    db = _db_with(job, cut)

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.publish.get_publisher") as mock_get_publisher,
        patch("worker.tasks.common.transition") as mock_transition,
        patch.object(publish_cut, "retry", side_effect=Retry()) as mock_retry,
    ):
        mock_get_publisher.return_value.publish.side_effect = NotImplementedError("not done yet")
        with pytest.raises(NotImplementedError):
            publish_cut(1)

    mock_retry.assert_not_called()
    assert job.status == models.JobStatus.failed
    mock_transition.assert_called_once_with(cut, "failed", CUT_TRANSITIONS)


def test_successful_publish_records_platform_post_id():
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    db = _db_with(job, cut)

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.publish.get_publisher") as mock_get_publisher,
        patch("worker.tasks.publish.record_stage"),
        patch("worker.tasks.publish.transition") as mock_transition,
    ):
        mock_get_publisher.return_value.publish.return_value = PublishResult(
            platform_post_id="yt-video-123", url="https://youtube.com/shorts/yt-video-123"
        )
        publish_cut(1)

    assert cut.platform_post_id == "yt-video-123"
    assert cut.published_at is not None
    assert job.status == models.JobStatus.done
    assert job.error is None
    mock_transition.assert_called_once_with(cut, "published", CUT_TRANSITIONS)
    mock_get_publisher.return_value.publish.assert_called_once()
    assert "caption" in mock_get_publisher.return_value.publish.call_args.kwargs


def test_a_transient_publisher_error_is_never_retried_automatically():
    """A read timeout can arrive after the platform accepted the upload; retrying would post twice."""
    import httpx
    from worker.tasks.publish import publish_cut

    assert publish_cut.max_retries == 0

    job = _job()
    cut = _cut()
    db = _db_with(job, cut)
    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.publish.get_publisher") as mock_get_publisher,
        patch("worker.tasks.publish.record_stage"),
        patch("worker.tasks.common.transition"),
        patch.object(publish_cut, "retry", side_effect=Retry()) as mock_retry,
    ):
        mock_get_publisher.return_value.publish.side_effect = httpx.ReadTimeout("response lost")
        with pytest.raises(httpx.ReadTimeout):
            publish_cut(1)

    mock_retry.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_post_id_is_committed_before_the_done_stamp():
    """The post is live and irreversible: its id must be durable even if the job is reaped later."""
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    db = _db_with(job, cut)
    order = []
    db.commit.side_effect = lambda: order.append(("commit", cut.platform_post_id))

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.publish.get_publisher") as mock_get_publisher,
        patch("worker.tasks.publish.record_stage"),
        patch("worker.tasks.publish.transition"),
    ):
        mock_get_publisher.return_value.publish.return_value = PublishResult(
            platform_post_id="yt-1", url="https://youtube.com/shorts/yt-1"
        )
        publish_cut(1)

    assert ("commit", "yt-1") in order, "platform_post_id was never committed on its own"


def test_an_already_posted_cut_is_finalized_without_uploading_again():
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    cut.platform_post_id = "yt-earlier"
    db = _db_with(job, cut)

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.publish.get_publisher") as mock_get_publisher,
        patch("worker.tasks.publish.transition") as mock_transition,
    ):
        publish_cut(1)

    mock_get_publisher.return_value.publish.assert_not_called()
    assert cut.platform_post_id == "yt-earlier"
    mock_transition.assert_called_once_with(cut, "published", CUT_TRANSITIONS)
    assert job.status == models.JobStatus.done


def test_an_already_posted_cut_still_passes_the_safety_gate():
    """Finalizing must not become a way around assert_safe_to_publish."""
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    cut.platform_post_id = "yt-earlier"
    db = _db_with(job, cut, unsafe_rows=[_unsafe_row()])

    with patch("worker.tasks.common.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="not cleared for publishing"):
            publish_cut(1)
