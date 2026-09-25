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
    # Legacy/no-op row under assert_video_matches_pins' None-means-skip rollout rule (see
    # engine/publish/gate.py) — a bare MagicMock would be truthy and non-None, so every test
    # below that exercises the uploading branch would otherwise trip the NEW staleness check
    # instead of whatever it actually means to exercise.
    cut.rendered_pins_fingerprint = None
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


def test_stale_video_pins_mismatch_blocks_publish():
    """assert_video_matches_pins wiring: a cut whose rendered_pins_fingerprint no longer
    matches the CURRENT pins must be blocked before any credential lookup or upload — the
    exact staleness hole this task's docs/specs/2026-09-video-pins-staleness-gate-system-
    design.md fixes. Uses its own distinct mock for compute_pins_fingerprint's query path,
    separate from unsafe_assets' db.query(...).join(...).filter(...).all() chain stub — the
    two must not be conflated."""
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    cut.rendered_pins_fingerprint = "fingerprint-from-the-render-that-built-video-path"
    db = _db_with(job, cut)

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.publish.get_publisher") as mock_get_publisher,
        patch(
            "engine.publish.gate.compute_pins_fingerprint",
            return_value="a-different-fingerprint-from-a-later-repin",
        ),
        patch.object(publish_cut, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(ValueError, match="Re-render before publishing"):
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


def test_post_id_is_committed_before_anything_that_can_fail_after_the_upload():
    """The commit must precede transition(): recording the id only in the done-stamp is not enough,
    because the done-stamp is lost if transition() raises or the job is reaped."""
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
        patch("worker.tasks.publish.transition", side_effect=lambda *a: order.append(("transition", None))),
    ):
        mock_get_publisher.return_value.publish.return_value = PublishResult(
            platform_post_id="yt-1", url="https://youtube.com/shorts/yt-1"
        )
        publish_cut(1)

    assert order.index(("commit", "yt-1")) < order.index(("transition", None))


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


def test_an_already_posted_cut_with_mismatched_pins_is_still_finalized_without_blocking():
    """Stronger version of the finalize-branch-exemption tests above: this one gives the cut
    a REAL, genuinely mismatched rendered_pins_fingerprint (not the fixture's default None) —
    so if assert_video_matches_pins were ever wired into the `if cut.platform_post_id:`
    branch by mistake, THIS test would raise and fail, unlike the two above (whose
    default None fingerprint short-circuits the check regardless of which branch it's
    called from, so they can't by themselves distinguish correct wiring from a misplaced
    call). Finalizing uploads nothing — a stale-pins video that's already live cannot be
    un-published by blocking the finalize; the operator's only way out is stuck."""
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    cut.platform_post_id = "yt-earlier"
    cut.rendered_pins_fingerprint = "fingerprint-from-the-render-that-built-video-path"
    db = _db_with(job, cut)

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.publish.get_publisher") as mock_get_publisher,
        patch(
            "engine.publish.gate.compute_pins_fingerprint",
            return_value="a-different-fingerprint-from-a-later-repin",
        ),
        patch("worker.tasks.publish.transition") as mock_transition,
    ):
        publish_cut(1)

    mock_get_publisher.return_value.publish.assert_not_called()
    mock_transition.assert_called_once_with(cut, "published", CUT_TRANSITIONS)
    assert job.status == models.JobStatus.done


def test_an_already_posted_cut_is_finalized_even_if_an_asset_was_flagged_since():
    """The gate guards what goes OUT. Finalizing uploads nothing, and blocking it would leave a live
    post unrecorded and the operator with no way out (render is refused for a posted cut)."""
    from worker.tasks.publish import publish_cut

    job = _job()
    cut = _cut()
    cut.platform_post_id = "yt-earlier"
    db = _db_with(job, cut, unsafe_rows=[_unsafe_row()])

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.publish.get_publisher") as mock_get_publisher,
        patch("worker.tasks.publish.transition") as mock_transition,
    ):
        publish_cut(1)

    mock_get_publisher.return_value.publish.assert_not_called()
    mock_transition.assert_called_once_with(cut, "published", CUT_TRANSITIONS)
    assert job.status == models.JobStatus.done
