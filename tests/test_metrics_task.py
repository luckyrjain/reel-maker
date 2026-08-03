"""Tests for worker/tasks/metrics.py::pull_publish_metrics."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from api import models
from engine.publish.metrics import EngagementMetrics
from worker.tasks.metrics import pull_publish_metrics


def _reel(db):
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    return reel


def _published_cut(db, reel, *, platform=models.CutPlatform.youtube_shorts, post_id="post-1"):
    cut = models.Cut(
        reel_id=reel.id, platform=platform, status=models.CutStatus.published,
        platform_post_id=post_id, published_at=datetime.now(timezone.utc),
    )
    db.add(cut)
    db.commit()
    return cut


def test_only_published_cuts_with_a_platform_post_id_are_considered(db_session):
    db = db_session
    reel = _reel(db)
    approved = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved)
    published_no_id = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.published)
    db.add_all([approved, published_no_id])
    db.commit()

    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics._pull_one") as mock_pull_one,
    ):
        pull_publish_metrics()

    mock_pull_one.assert_not_called()


def test_pulls_metrics_for_published_cut_with_credential(db_session):
    db = db_session
    reel = _reel(db)
    cut = _published_cut(db, reel)
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    db.commit()

    fake_fetcher = MagicMock()
    fake_fetcher.fetch.return_value = EngagementMetrics(views=100, likes=10, comments=2)

    # pull_publish_metrics() closes its (mocked-to-be-ours) session in a
    # finally block — no-op that here so the session stays usable for the
    # post-hoc query below instead of raising DetachedInstanceError.
    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fake_fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()

    updated = db.get(models.Cut, cut.id)
    assert updated.views == 100
    assert updated.likes == 10
    assert updated.comments == 2
    assert updated.metrics_updated_at is not None


def test_skips_cut_when_no_credential_connected(db_session):
    db = db_session
    reel = _reel(db)
    cut = _published_cut(db, reel)
    # No Credential row at all.

    fake_fetcher = MagicMock()

    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fake_fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()

    fake_fetcher.fetch.assert_not_called()
    assert db.get(models.Cut, cut.id).views is None


def test_skips_platform_with_no_fetcher(db_session):
    db = db_session
    reel = _reel(db)
    _published_cut(db, reel, platform=models.CutPlatform.tiktok, post_id="tt-1")
    db.add(models.Credential(provider="tiktok", token_blob="tok"))
    db.commit()

    with patch("worker.tasks.metrics.SessionLocal", return_value=db):
        pull_publish_metrics()  # get_metrics_fetcher("tiktok") is really None — must not raise


def test_one_cuts_fetch_failure_does_not_abort_the_batch(db_session):
    db = db_session
    reel = _reel(db)
    failing_cut = _published_cut(db, reel, post_id="post-fail")
    ok_cut = _published_cut(db, reel, post_id="post-ok")
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    db.commit()

    fake_fetcher = MagicMock()
    def _fetch(cut, credential):
        if cut.platform_post_id == "post-fail":
            raise RuntimeError("API down")
        return EngagementMetrics(views=50)
    fake_fetcher.fetch.side_effect = _fetch

    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fake_fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()  # must not raise despite one cut failing

    assert db.get(models.Cut, ok_cut.id).views == 50
    assert db.get(models.Cut, failing_cut.id).views is None


def test_none_result_leaves_existing_metrics_untouched(db_session):
    db = db_session
    reel = _reel(db)
    cut = _published_cut(db, reel)
    cut.views = 999  # pre-existing value from a previous successful pull
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    db.commit()

    fake_fetcher = MagicMock()
    fake_fetcher.fetch.return_value = None

    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fake_fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()

    assert db.get(models.Cut, cut.id).views == 999
