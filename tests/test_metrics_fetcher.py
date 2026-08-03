"""Tests for engine/publish/metrics.py."""
from unittest.mock import MagicMock, patch

from engine.publish.metrics import InstagramMetricsFetcher, YouTubeMetricsFetcher


def _fake_cut(post_id="yt-abc123"):
    cut = MagicMock()
    cut.platform_post_id = post_id
    return cut


def _fake_credential(token="access-tok"):
    cred = MagicMock()
    cred.token_blob = token
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
        result = YouTubeMetricsFetcher().fetch(_fake_cut(), _fake_credential())

    assert result.views == 1500
    assert result.likes == 42
    assert result.comments == 3
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer access-tok"
    assert kwargs["params"]["id"] == "yt-abc123"


def test_youtube_fetch_returns_none_when_video_not_found():
    resp = _resp({"items": []})
    with patch("engine.publish.metrics.httpx.get", return_value=resp):
        result = YouTubeMetricsFetcher().fetch(_fake_cut(), _fake_credential())
    assert result is None


def test_youtube_fetch_tolerates_missing_fields():
    resp = _resp({"items": [{"statistics": {"viewCount": "10"}}]})
    with patch("engine.publish.metrics.httpx.get", return_value=resp):
        result = YouTubeMetricsFetcher().fetch(_fake_cut(), _fake_credential())
    assert result.views == 10
    assert result.likes is None
    assert result.comments is None


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
        result = InstagramMetricsFetcher().fetch(_fake_cut(post_id="ig-media-1"), _fake_credential("page-tok"))

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
        result = InstagramMetricsFetcher().fetch(_fake_cut(), _fake_credential())
    assert result is None


def test_instagram_fetch_ignores_metrics_without_values():
    resp = _resp({"data": [{"name": "plays", "values": []}]})
    with patch("engine.publish.metrics.httpx.get", return_value=resp):
        result = InstagramMetricsFetcher().fetch(_fake_cut(), _fake_credential())
    assert result.views is None
