"""Tests for engine/publish/youtube.py — resumable upload flow."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from engine.publish.youtube import YouTubePublisher


def _fake_cut(video_path):
    cut = MagicMock()
    cut.video_path = str(video_path)
    cut.caption = "Argentina's weak spot could decide the tournament."
    cut.hashtags = ["football", "worldcup"]
    return cut


def _fake_credential(*, expired=False, refresh_token="rtok"):
    cred = MagicMock()
    cred.token_blob = "access-tok"
    cred.refresh_token_blob = refresh_token
    cred.expires_at = (
        datetime.now(timezone.utc) - timedelta(hours=1)
        if expired else
        datetime.now(timezone.utc) + timedelta(hours=1)
    )
    return cred


@pytest.fixture()
def video_file(tmp_path):
    p = tmp_path / "video.mp4"
    p.write_bytes(b"fake mp4 bytes")
    return p


def test_publish_uploads_and_returns_video_id(video_file):
    cut = _fake_cut(video_file)
    credential = _fake_credential()
    db = MagicMock()

    init_resp = MagicMock()
    init_resp.raise_for_status.return_value = None
    init_resp.headers = {"Location": "https://upload.example.com/session123"}

    upload_resp = MagicMock()
    upload_resp.raise_for_status.return_value = None
    upload_resp.json.return_value = {"id": "yt-abc123"}

    with (
        patch("engine.publish.youtube.httpx.post", return_value=init_resp) as mock_post,
        patch("engine.publish.youtube.httpx.put", return_value=upload_resp) as mock_put,
    ):
        result = YouTubePublisher().publish(cut, credential, db, caption=cut.caption)

    assert result.platform_post_id == "yt-abc123"
    assert result.url == "https://youtube.com/shorts/yt-abc123"

    _, post_kwargs = mock_post.call_args
    assert post_kwargs["headers"]["Authorization"] == "Bearer access-tok"
    assert post_kwargs["json"]["snippet"]["title"].startswith("Argentina")

    put_args, put_kwargs = mock_put.call_args
    assert put_args[0] == "https://upload.example.com/session123"
    assert put_kwargs["content"] == b"fake mp4 bytes"


def test_publish_raises_when_no_upload_url_returned(video_file):
    cut = _fake_cut(video_file)
    credential = _fake_credential()
    db = MagicMock()

    init_resp = MagicMock()
    init_resp.raise_for_status.return_value = None
    init_resp.headers = {}  # no Location header

    with patch("engine.publish.youtube.httpx.post", return_value=init_resp):
        with pytest.raises(ValueError, match="did not return a resumable upload session"):
            YouTubePublisher().publish(cut, credential, db, caption=cut.caption)


def test_expired_token_is_refreshed_before_upload(video_file):
    cut = _fake_cut(video_file)
    credential = _fake_credential(expired=True)
    db = MagicMock()

    fake_oauth = MagicMock()
    fake_oauth.refresh.return_value = {"access_token": "new-tok", "expires_in": 3600}

    init_resp = MagicMock()
    init_resp.raise_for_status.return_value = None
    init_resp.headers = {"Location": "https://upload.example.com/session123"}
    upload_resp = MagicMock()
    upload_resp.raise_for_status.return_value = None
    upload_resp.json.return_value = {"id": "yt-abc123"}

    with (
        patch("engine.publish.youtube.get_oauth_provider", return_value=fake_oauth),
        patch("engine.publish.youtube.httpx.post", return_value=init_resp) as mock_post,
        patch("engine.publish.youtube.httpx.put", return_value=upload_resp),
    ):
        YouTubePublisher().publish(cut, credential, db, caption=cut.caption)

    fake_oauth.refresh.assert_called_once_with("rtok")
    assert credential.token_blob == "new-tok"
    db.commit.assert_called_once()
    _, post_kwargs = mock_post.call_args
    assert post_kwargs["headers"]["Authorization"] == "Bearer new-tok"


def test_publish_uses_provided_caption_not_cut_caption(video_file):
    """The caption param (not cut.caption) must reach the upload — this is
    where worker/tasks/publish.py injects the attribution block."""
    cut = _fake_cut(video_file)
    cut.caption = "Original caption, no attribution"
    credential = _fake_credential()
    db = MagicMock()

    init_resp = MagicMock()
    init_resp.raise_for_status.return_value = None
    init_resp.headers = {"Location": "https://upload.example.com/session123"}
    upload_resp = MagicMock()
    upload_resp.raise_for_status.return_value = None
    upload_resp.json.return_value = {"id": "yt-abc123"}

    with (
        patch("engine.publish.youtube.httpx.post", return_value=init_resp) as mock_post,
        patch("engine.publish.youtube.httpx.put", return_value=upload_resp),
    ):
        YouTubePublisher().publish(
            cut, credential, db,
            caption="Original caption, no attribution\n\nImage credit: Jane Doe (CC BY-SA)",
        )

    _, post_kwargs = mock_post.call_args
    assert "Image credit" in post_kwargs["json"]["snippet"]["description"]
    assert post_kwargs["json"]["snippet"]["title"] == "Original caption, no attribution"


def test_publish_with_whitespace_only_caption_falls_back_to_default_title(video_file):
    """"".splitlines() is [] — a naive strip-then-splitlines[0] would IndexError
    on a caption that's whitespace-only rather than empty."""
    cut = _fake_cut(video_file)
    credential = _fake_credential()
    db = MagicMock()

    init_resp = MagicMock()
    init_resp.raise_for_status.return_value = None
    init_resp.headers = {"Location": "https://upload.example.com/session123"}
    upload_resp = MagicMock()
    upload_resp.raise_for_status.return_value = None
    upload_resp.json.return_value = {"id": "yt-abc123"}

    with (
        patch("engine.publish.youtube.httpx.post", return_value=init_resp) as mock_post,
        patch("engine.publish.youtube.httpx.put", return_value=upload_resp),
    ):
        YouTubePublisher().publish(cut, credential, db, caption="   \n\n  ")

    _, post_kwargs = mock_post.call_args
    assert post_kwargs["json"]["snippet"]["title"] == "Reel"


def test_expired_token_without_refresh_token_raises(video_file):
    cut = _fake_cut(video_file)
    credential = _fake_credential(expired=True, refresh_token=None)
    db = MagicMock()

    with pytest.raises(ValueError, match="no refresh token is stored"):
        YouTubePublisher().publish(cut, credential, db, caption=cut.caption)
