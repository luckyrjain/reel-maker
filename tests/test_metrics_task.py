"""Tests for worker/tasks/metrics.py::pull_publish_metrics."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

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
    assert db.query(models.CutMetricSnapshot).filter_by(cut_id=cut.id).count() == 0


def test_skips_platform_with_no_fetcher(db_session):
    db = db_session
    reel = _reel(db)
    cut = _published_cut(db, reel, platform=models.CutPlatform.tiktok, post_id="tt-1")
    db.add(models.Credential(provider="tiktok", token_blob="tok"))
    db.commit()

    with patch("worker.tasks.metrics.SessionLocal", return_value=db):
        pull_publish_metrics()  # get_metrics_fetcher("tiktok") is really None — must not raise

    assert db.query(models.CutMetricSnapshot).filter_by(cut_id=cut.id).count() == 0


def test_one_cuts_fetch_failure_does_not_abort_the_batch(db_session):
    db = db_session
    reel = _reel(db)
    failing_cut = _published_cut(db, reel, post_id="post-fail")
    ok_cut = _published_cut(db, reel, post_id="post-ok")
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    db.commit()

    fake_fetcher = MagicMock()
    def _fetch(cut, credential, db):
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
    # No fetch happened at all (result is None before any snapshot code runs) --
    # no history row either.
    assert db.query(models.CutMetricSnapshot).filter_by(cut_id=cut.id).count() == 0


# ── Metrics history (docs/specs/2026-09-metrics-history-system-design.md) ──

def test_successful_fetch_writes_a_matching_history_snapshot(db_session):
    db = db_session
    reel = _reel(db)
    cut = _published_cut(db, reel)
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    db.commit()

    fake_fetcher = MagicMock()
    fake_fetcher.fetch.return_value = EngagementMetrics(views=100, likes=10, comments=2)

    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fake_fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()

    updated = db.get(models.Cut, cut.id)
    snapshots = db.query(models.CutMetricSnapshot).filter_by(cut_id=cut.id).all()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert (snap.views, snap.likes, snap.comments) == (100, 10, 2)
    # Single now() capture design: the snapshot's recorded_at must be EXACTLY the
    # same timestamp as cut.metrics_updated_at, not merely close in time.
    assert snap.recorded_at == updated.metrics_updated_at


def test_partial_fetch_snapshot_records_raw_missing_fields_not_the_forward_filled_cut(db_session):
    """The property most likely to regress: a partial fetch (the Instagram
    metric-drift case) must NOT record the forward-filled cut.likes/cut.comments
    into the snapshot -- that would fabricate a history point for a metric that
    genuinely wasn't measured this pull, corrupting a future trend line with a
    false flat segment. Mutation-tested against `views=cut.views` substituted
    for `views=result.views` etc."""
    db = db_session
    reel = _reel(db)
    cut = _published_cut(db, reel)
    # Pre-existing "latest known" values from an earlier, full pull.
    cut.likes = 50
    cut.comments = 5
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    db.commit()

    fake_fetcher = MagicMock()
    # This pull only returns views -- likes/comments genuinely weren't measured
    # (the Instagram metric-drift scenario), not "measured as zero."
    fake_fetcher.fetch.return_value = EngagementMetrics(views=100, likes=None, comments=None)

    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fake_fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()

    updated = db.get(models.Cut, cut.id)
    # The "latest known" columns correctly forward-fill: likes/comments stay at
    # their prior values, since this pull didn't say they changed.
    assert (updated.views, updated.likes, updated.comments) == (100, 50, 5)

    snap = db.query(models.CutMetricSnapshot).filter_by(cut_id=cut.id).one()
    # The history row must NOT inherit that forward-fill -- it records exactly
    # what this pull returned, missing fields and all.
    assert (snap.views, snap.likes, snap.comments) == (100, None, None)


def test_fully_none_result_still_writes_a_snapshot_recording_nothing_measured(db_session):
    """A response that came back but had every metric unavailable ("we tried and
    got nothing") is a distinct, meaningful data point from "we never tried"
    (no row at all) -- see the design doc §3. The behavior most likely to get
    quietly simplified away by an early-return guard during implementation."""
    db = db_session
    reel = _reel(db)
    cut = _published_cut(db, reel)
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    db.commit()

    fake_fetcher = MagicMock()
    fake_fetcher.fetch.return_value = EngagementMetrics(views=None, likes=None, comments=None)

    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fake_fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()

    snap = db.query(models.CutMetricSnapshot).filter_by(cut_id=cut.id).one()
    assert (snap.views, snap.likes, snap.comments) == (None, None, None)
    assert db.get(models.Cut, cut.id).metrics_updated_at is not None


def test_fetcher_exception_writes_no_snapshot_for_the_failing_cut_only(db_session):
    db = db_session
    reel = _reel(db)
    failing_cut = _published_cut(db, reel, post_id="post-fail")
    ok_cut = _published_cut(db, reel, post_id="post-ok")
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    db.commit()

    fake_fetcher = MagicMock()
    def _fetch(cut, credential, db):
        if cut.platform_post_id == "post-fail":
            raise RuntimeError("API down")
        return EngagementMetrics(views=50)
    fake_fetcher.fetch.side_effect = _fetch

    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fake_fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()

    assert db.query(models.CutMetricSnapshot).filter_by(cut_id=failing_cut.id).count() == 0
    assert db.query(models.CutMetricSnapshot).filter_by(cut_id=ok_cut.id).count() == 1



# --- credential tokens: decrypted up front, and an unreadable one skips only its own cut ----

def test_tokens_are_loaded_before_the_fetch_not_lazily_during_it(db_session):
    """The deferred token columns must already be loaded when the fetcher runs, or its first
    read would lazy-load them inside the HTTP call's open transaction."""
    from sqlalchemy import inspect
    db = db_session
    reel = _reel(db)
    _published_cut(db, reel)
    db.add(models.Credential(provider="youtube", token_blob="tok", refresh_token_blob="r"))
    db.commit()
    seen = {}

    def fetch(cut, credential, db_):
        seen["unloaded"] = set(inspect(credential).unloaded)
        return EngagementMetrics(views=1, likes=1, comments=1)

    fetcher = MagicMock()
    fetcher.fetch.side_effect = fetch
    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()
    assert "unloaded" in seen                      # the fetcher really ran
    assert not ({"token_blob", "refresh_token_blob"} & seen["unloaded"])


def test_an_unreadable_credential_skips_its_cut_without_aborting_the_batch(db_session, monkeypatch, caplog):
    """One platform's credential sealed under a rotated key must not stop the metrics pull
    for every other platform (or crash the beat task)."""
    from cryptography.fernet import Fernet
    db = db_session
    reel = _reel(db)
    yt = _published_cut(db, reel, platform=models.CutPlatform.youtube_shorts, post_id="yt-1")
    ig = _published_cut(db, reel, platform=models.CutPlatform.instagram_reels, post_id="ig-1")
    monkeypatch.setattr("api.crypto.settings.credentials_key", Fernet.generate_key().decode())
    db.add(models.Credential(provider="youtube", token_blob="old-tok"))
    db.commit()
    monkeypatch.setattr("api.crypto.settings.credentials_key", Fernet.generate_key().decode())
    db.add(models.Credential(provider="instagram", token_blob="fresh-tok"))
    db.commit()

    fetcher = MagicMock()
    fetcher.fetch.return_value = EngagementMetrics(views=7, likes=1, comments=1)
    import logging
    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fetcher),
        patch.object(db, "close"),
        caplog.at_level(logging.WARNING, logger="worker.tasks.metrics"),
    ):
        pull_publish_metrics()                     # must not raise

    skipped = [r for r in caplog.records if "Skipping metrics" in r.getMessage()]
    assert len(skipped) == 1 and skipped[0].levelno == logging.WARNING
    assert skipped[0].exc_info is None             # one line, no traceback per cut
    assert "CREDENTIALS_KEY" in skipped[0].getMessage() and "old-tok" not in caplog.text
    assert fetcher.fetch.call_count == 1
    assert fetcher.fetch.call_args.args[0].id == ig.id
    db.refresh(ig)
    db.refresh(yt)
    assert ig.views == 7 and yt.views is None


# ── the skip path: which cuts it covers, what it leaves behind, what it must NOT swallow ──

def _two_cuts_one_stale(db, monkeypatch, stale="youtube"):
    """Both platforms have a published cut; `stale`'s credential was sealed under a key that
    is no longer current. Cuts are created in the opposite order to the test above so a
    swallow that depends on order or platform is caught."""
    from cryptography.fernet import Fernet
    reel = _reel(db)
    ig = _published_cut(db, reel, platform=models.CutPlatform.instagram_reels, post_id="ig-1")
    yt = _published_cut(db, reel, platform=models.CutPlatform.youtube_shorts, post_id="yt-1")
    monkeypatch.setattr("api.crypto.settings.credentials_key", Fernet.generate_key().decode())
    db.add(models.Credential(provider=stale, token_blob="OLDTOK7"))
    db.commit()
    monkeypatch.setattr("api.crypto.settings.credentials_key", Fernet.generate_key().decode())
    db.add(models.Credential(provider="instagram" if stale == "youtube" else "youtube",
                             token_blob="FRESHTOK9"))
    db.commit()
    return ig, yt


def _run_pull(db, fetcher):
    with (
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=fetcher),
        patch.object(db, "close"),
    ):
        pull_publish_metrics()


def test_a_stale_instagram_credential_does_not_stop_the_youtube_cut(db_session, monkeypatch):
    ig, yt = _two_cuts_one_stale(db_session, monkeypatch, stale="instagram")
    fetcher = MagicMock()
    fetcher.fetch.return_value = EngagementMetrics(views=5, likes=1, comments=1)
    _run_pull(db_session, fetcher)
    assert fetcher.fetch.call_count == 1 and fetcher.fetch.call_args.args[0].id == yt.id


def test_a_skipped_cut_leaves_no_stamp_and_no_snapshot(db_session, monkeypatch):
    ig, yt = _two_cuts_one_stale(db_session, monkeypatch, stale="youtube")
    fetcher = MagicMock()
    fetcher.fetch.return_value = EngagementMetrics(views=5, likes=1, comments=1)
    _run_pull(db_session, fetcher)
    db_session.refresh(yt)
    assert yt.metrics_updated_at is None
    assert db_session.query(models.CutMetricSnapshot).filter_by(cut_id=yt.id).count() == 0


def test_the_skip_is_one_warning_line_with_ids_and_no_secret(db_session, monkeypatch, caplog):
    import logging
    ig, yt = _two_cuts_one_stale(db_session, monkeypatch, stale="youtube")
    fetcher = MagicMock()
    fetcher.fetch.return_value = EngagementMetrics(views=5, likes=1, comments=1)
    with caplog.at_level(logging.DEBUG):
        _run_pull(db_session, fetcher)
    recs = [r for r in caplog.records if r.name == "worker.tasks.metrics"
            and "Skipping metrics" in r.getMessage()]
    assert len(recs) == 1
    rec = recs[0]
    assert rec.levelno == logging.WARNING and rec.exc_info is None
    assert f"cut_id={yt.id}" in rec.getMessage() and "platform=youtube_shorts" in rec.getMessage()
    assert "OLDTOK7" not in caplog.text and "FRESHTOK9" not in caplog.text


@pytest.mark.parametrize("exc", [OperationalError("s", {}, Exception("db down")), KeyboardInterrupt()])
def test_only_an_unreadable_token_is_skipped_a_db_failure_still_surfaces(db_session, exc):
    """`except ValueError` must stay narrow: a database outage or Ctrl-C is not an
    'unreadable credential' and must not be swallowed as one."""
    db = db_session
    _published_cut(db, _reel(db))
    real_query = db.query

    def query(model, *a, **k):
        if model is models.Credential:
            raise exc
        return real_query(model, *a, **k)

    with (
        patch.object(db, "query", side_effect=query),
        patch("worker.tasks.metrics.SessionLocal", return_value=db),
        patch("worker.tasks.metrics.get_metrics_fetcher", return_value=MagicMock()),
        patch.object(db, "close"),
    ):
        with pytest.raises(type(exc)):
            pull_publish_metrics()
