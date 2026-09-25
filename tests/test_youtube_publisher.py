"""Tests for engine/publish/youtube.py — resumable upload flow."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from engine.publish.youtube import YouTubePublisher


def _fake_cut(video_path, subtitle_path=None):
    cut = MagicMock()
    cut.id = 5
    cut.reel_id = 10
    cut.video_path = str(video_path)
    cut.caption = "Argentina's weak spot could decide the tournament."
    cut.hashtags = ["football", "worldcup"]
    # Explicit, not MagicMock's incidental truthiness — most tests here don't
    # care about the captions-upload branch at all, so make its trigger opt-in.
    cut.subtitle_path = subtitle_path
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


# ── _upload_captions() request construction (Phase 5d) ──────────────────────
# Not exercised by the publish() tests above (subtitle_path=None there) or by
# test_tasks_real_db.py's real-DB StageEvent coverage (which mocks
# _upload_captions itself, never inspecting how it calls httpx.post) — this is
# the one place the actual outgoing request shape gets checked.


def test_upload_captions_sends_token_in_header_never_in_params(tmp_path):
    from engine.publish.youtube import _upload_captions

    srt_path = tmp_path / "cut.srt"
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    with patch("engine.publish.youtube.httpx.post", return_value=resp) as mock_post:
        _upload_captions("vid123", str(srt_path), "secret-access-token")

    _, kwargs = mock_post.call_args
    assert "secret-access-token" not in str(kwargs.get("params", {})), (
        "token must not leak into the URL — see CLAUDE.md's Token-in-URL convention"
    )
    assert kwargs["headers"]["Authorization"] == "Bearer secret-access-token"


def test_upload_captions_builds_a_multipart_related_body_not_form_data(tmp_path):
    """multipart/related (RFC 2387) is what Google's uploadType=multipart protocol
    expects — a different wire format than multipart/form-data (httpx's `files=`),
    whose parts carry Content-Disposition/field names Google's endpoint doesn't
    parse the same way. Assert the actual body/header shape, not just that some
    request was sent."""
    from engine.publish.youtube import _upload_captions

    srt_path = tmp_path / "cut.srt"
    srt_bytes = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    srt_path.write_bytes(srt_bytes)

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    with patch("engine.publish.youtube.httpx.post", return_value=resp) as mock_post:
        _upload_captions("vid123", str(srt_path), "tok")

    _, kwargs = mock_post.call_args
    content_type = kwargs["headers"]["Content-Type"]
    assert content_type.startswith("multipart/related; boundary=")
    boundary = content_type.split("boundary=", 1)[1]

    body = kwargs["content"]
    assert isinstance(body, bytes)
    assert f"--{boundary}".encode() in body
    assert f"--{boundary}--".encode() in body
    assert b'"videoId": "vid123"' in body
    assert srt_bytes in body
    assert b"Content-Disposition" not in body, (
        "multipart/related parts are distinguished by Content-Type alone — a "
        "Content-Disposition header would mean this regressed to form-data framing"
    )


def test_build_multipart_related_format(tmp_path):
    from engine.publish.youtube import _build_multipart_related

    body = _build_multipart_related(
        [("application/json", b'{"a": 1}'), ("application/octet-stream", b"raw bytes")],
        boundary="BOUND",
    )
    assert body == (
        b"--BOUND\r\nContent-Type: application/json\r\n\r\n"
        b'{"a": 1}\r\n'
        b"--BOUND\r\nContent-Type: application/octet-stream\r\n\r\n"
        b"raw bytes\r\n"
        b"--BOUND--"
    )
