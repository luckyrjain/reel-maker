"""Tests for engine/publish/metrics.py."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from engine.publish.metrics import InstagramMetricsFetcher, YouTubeMetricsFetcher


def _fake_cut(post_id="yt-abc123"):
    cut = MagicMock()
    cut.platform_post_id = post_id
    return cut


def _fake_credential(token="access-tok"):
    cred = MagicMock()
    cred.token_blob = token
    cred.expires_at = None  # not expired — get_valid_access_token() skips refresh
    return cred


def _resp(json_data):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_data
    return resp


# ── YouTube ─────────────────────────────────────────────────────────────────

def test_youtube_fetch_parses_statistics():
    resp = _resp({"items": [{"statistics": {"viewCount": "1500", "likeCount": "42", "commentCount": "3"}}]})
    with patch("engine.publish.metrics.httpx.get", return_value=resp) as mock_get:
        result = YouTubeMetricsFetcher().fetch(_fake_cut(), _fake_credential(), MagicMock())

    assert result.views == 1500
    assert result.likes == 42
    assert result.comments == 3
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer access-tok"
    assert kwargs["params"]["id"] == "yt-abc123"


def test_youtube_fetch_returns_none_when_video_not_found():
    resp = _resp({"items": []})
    with patch("engine.publish.metrics.httpx.get", return_value=resp):
        result = YouTubeMetricsFetcher().fetch(_fake_cut(), _fake_credential(), MagicMock())
    assert result is None


def test_youtube_fetch_tolerates_missing_fields():
    resp = _resp({"items": [{"statistics": {"viewCount": "10"}}]})
    with patch("engine.publish.metrics.httpx.get", return_value=resp):
        result = YouTubeMetricsFetcher().fetch(_fake_cut(), _fake_credential(), MagicMock())
    assert result.views == 10
    assert result.likes is None
    assert result.comments is None


def test_youtube_fetch_refreshes_expired_token_before_calling_api():
    """Google access tokens expire in ~1h. pull_publish_metrics runs every 6h,
    so an expired credential is the common case, not the exception — the
    fetcher must refresh (and persist) a new token first, the same as
    YouTubePublisher.publish() does, or every scheduled pull just 401s."""
    from datetime import datetime, timedelta, timezone

    cred = _fake_credential(token="stale-tok")
    cred.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    cred.refresh_token_blob = "refresh-tok"
    db = MagicMock()

    fake_oauth = MagicMock()
    fake_oauth.refresh.return_value = {"access_token": "fresh-tok", "expires_in": 3600}

    resp = _resp({"items": [{"statistics": {"viewCount": "10"}}]})
    with (
        patch("engine.publish.youtube.get_oauth_provider", return_value=fake_oauth),
        patch("engine.publish.metrics.httpx.get", return_value=resp) as mock_get,
    ):
        result = YouTubeMetricsFetcher().fetch(_fake_cut(), cred, db)

    assert result.views == 10
    assert cred.token_blob == "fresh-tok"
    db.commit.assert_called_once()
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer fresh-tok"


# ── Instagram ────────────────────────────────────────────────────────────────

def test_instagram_fetch_parses_insights():
    resp = _resp({
        "data": [
            {"name": "plays", "values": [{"value": 500}]},
            {"name": "likes", "values": [{"value": 30}]},
            {"name": "comments", "values": [{"value": 4}]},
        ]
    })
    with patch("engine.publish.metrics.httpx.get", return_value=resp) as mock_get:
        result = InstagramMetricsFetcher().fetch(_fake_cut(post_id="ig-media-1"), _fake_credential("page-tok"), MagicMock())

    assert result.views == 500
    assert result.likes == 30
    assert result.comments == 4
    args, kwargs = mock_get.call_args
    assert args[0] == "https://graph.facebook.com/v19.0/ig-media-1/insights"
    assert kwargs["headers"]["Authorization"] == "Bearer page-tok"
    assert "access_token" not in kwargs["params"], "token must not leak into the URL"


def test_instagram_fetch_returns_none_when_no_data():
    resp = _resp({"data": []})
    with patch("engine.publish.metrics.httpx.get", return_value=resp):
        result = InstagramMetricsFetcher().fetch(_fake_cut(), _fake_credential(), MagicMock())
    assert result is None


def test_instagram_fetch_ignores_metrics_without_values():
    resp = _resp({"data": [{"name": "plays", "values": []}]})
    with patch("engine.publish.metrics.httpx.get", return_value=resp):
        result = InstagramMetricsFetcher().fetch(_fake_cut(), _fake_credential(), MagicMock())
    assert result.views is None


# ── Instagram metrics drift instrumentation (record_stage) ───────────────────

def _fake_record_stage(calls):
    """Stand-in for engine.observability.record_stage matching its signature and
    context-manager contract closely enough for these tests: yields a plain
    object the fetch() code can set .ok / .detail[...] on, and records what
    happened after a normal (non-raising) exit — good enough for the
    "does the composition set ok/detail correctly without raising" tests below.
    The real record_stage is exercised directly (not faked) in the
    whole-request-failure test further down, since that one needs a real
    committed StageEvent row."""
    @contextmanager
    def fn(db, reel_id, stage_name, *, cut_id=None, **detail):
        ev = SimpleNamespace(ok=True, detail=dict(detail))
        yield ev
        calls.append({"reel_id": reel_id, "stage": stage_name, "cut_id": cut_id,
                       "ok": ev.ok, "detail": ev.detail})
    return fn


def test_instagram_fetch_records_the_no_data_yet_case_as_an_ordinary_ok_pull():
    """The empty-`data` case ("no insights yet" for a just-published video) still
    writes a StageEvent -- there is no way to enter record_stage's `with` block
    around the HTTP call (needed so a whole-request failure is still instrumented)
    and exit through an early `return` with zero write, since record_stage's own
    `finally` fires on every normal exit. Resolution (see the design doc's
    build-stage correction, found by a Lens B review of the first implementation):
    this case writes an ordinary ok=True StageEvent with no missing_metrics key,
    indistinguishable from a genuinely clean pull -- never flagged as drift, and
    self-limiting (once real Insights data exists, this branch is never hit again
    for that cut)."""
    calls = []
    resp = _resp({"data": []})
    with (
        patch("engine.publish.metrics.httpx.get", return_value=resp),
        patch("engine.publish.metrics.record_stage", _fake_record_stage(calls)),
    ):
        result = InstagramMetricsFetcher().fetch(_fake_cut(), _fake_credential(), MagicMock())

    assert result is None
    assert len(calls) == 1
    assert calls[0]["ok"] is True
    assert "missing_metrics" not in calls[0]["detail"]


def test_instagram_fetch_records_a_clean_pull_as_ok():
    calls = []
    resp = _resp({
        "data": [
            {"name": "plays", "values": [{"value": 500}]},
            {"name": "likes", "values": [{"value": 30}]},
            {"name": "comments", "values": [{"value": 4}]},
        ]
    })
    cut = _fake_cut(post_id="ig-media-1")
    with (
        patch("engine.publish.metrics.httpx.get", return_value=resp),
        patch("engine.publish.metrics.record_stage", _fake_record_stage(calls)),
    ):
        result = InstagramMetricsFetcher().fetch(cut, _fake_credential(), MagicMock())

    assert result.views == 500
    assert len(calls) == 1
    assert calls[0]["stage"] == "instagram_metrics"
    assert calls[0]["ok"] is True
    assert "missing_metrics" not in calls[0]["detail"]


def test_instagram_fetch_records_missing_metrics_without_raising():
    calls = []
    resp = _resp({
        "data": [
            {"name": "plays", "values": [{"value": 500}]},
            {"name": "likes", "values": [{"value": 30}]},
            # "comments" entirely absent — the renamed/retired-metric case.
        ]
    })
    with (
        patch("engine.publish.metrics.httpx.get", return_value=resp),
        patch("engine.publish.metrics.record_stage", _fake_record_stage(calls)),
    ):
        result = InstagramMetricsFetcher().fetch(_fake_cut(), _fake_credential(), MagicMock())

    # Existing merge-friendly contract preserved: a partial result, not a raise.
    assert result.views == 500
    assert result.likes == 30
    assert result.comments is None

    assert len(calls) == 1
    assert calls[0]["ok"] is False
    assert calls[0]["detail"]["missing_metrics"] == ["comments"]


def test_instagram_fetch_does_not_flag_a_present_but_valueless_metric_as_missing():
    """The exact regression the design's revision note exists to guard against:
    a metric present in `data` by name but with an empty `values` list (Meta
    returned the field, just with no value yet) must NOT be treated as a
    missing/renamed metric — only genuinely absent names should be flagged.
    Reuses test_instagram_fetch_ignores_metrics_without_values's fixture shape.

    Mutation-tested (see the Builder's report): temporarily changed the
    implementation to diff against by_name.keys() instead of the raw `data`
    names, confirmed this test fails, then reverted."""
    calls = []
    resp = _resp({"data": [{"name": "plays", "values": []}]})
    with (
        patch("engine.publish.metrics.httpx.get", return_value=resp),
        patch("engine.publish.metrics.record_stage", _fake_record_stage(calls)),
    ):
        result = InstagramMetricsFetcher().fetch(_fake_cut(), _fake_credential(), MagicMock())

    assert result.views is None  # existing behavior: empty values -> None, unchanged
    assert len(calls) == 1
    assert calls[0]["ok"] is False  # likes/comments are genuinely absent
    missing = calls[0]["detail"]["missing_metrics"]
    assert "plays" not in missing, "present-but-valueless must not count as missing"
    assert sorted(missing) == ["comments", "likes"]


def test_instagram_fetch_records_a_whole_request_failure():
    """A whole-request failure (resp.raise_for_status() raises, e.g. an entirely
    invalid/retired metric name) must still propagate unchanged (_pull_one()'s
    existing per-cut catch is the outer safety net) AND must leave a real,
    committed StageEvent(ok=False) row — using a real in-memory SQLite session
    here rather than mocking record_stage, per the change-impact report's
    test-strength recommendation (a real row proves the composition actually
    writes, not just that a mock was called)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    reel = models.Reel(context="c", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id, platform=models.CutPlatform.instagram_reels,
        status=models.CutStatus.published, platform_post_id="ig-media-1",
    )
    db.add(cut)
    db.commit()

    fake_response = MagicMock()
    fake_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400 Bad Request", request=MagicMock(), response=MagicMock()
    )
    with patch("engine.publish.metrics.httpx.get", return_value=fake_response):
        with pytest.raises(httpx.HTTPStatusError):
            InstagramMetricsFetcher().fetch(cut, _fake_credential(), db)

    events = db.query(models.StageEvent).filter_by(stage="instagram_metrics").all()
    assert len(events) == 1
    assert events[0].ok is False
    assert "error" in events[0].detail
    assert events[0].reel_id == reel.id
    assert events[0].cut_id == cut.id
