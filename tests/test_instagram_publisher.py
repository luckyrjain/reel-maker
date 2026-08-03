"""Tests for engine/publish/instagram.py — container-create/poll/publish flow."""
from unittest.mock import MagicMock, patch

import pytest

from engine.publish.instagram import InstagramPublisher


def _fake_cut():
    cut = MagicMock()
    cut.id = 42
    cut.caption = "Saved $10k in one year with these simple habits."
    return cut


def _fake_credential(provider_account_id="ig123"):
    cred = MagicMock()
    cred.token_blob = "page-access-tok"
    cred.provider_account_id = provider_account_id
    return cred


def _resp(json_data, status_ok=True):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_data
    return resp


def test_publish_full_flow_returns_media_id():
    cut = _fake_cut()
    credential = _fake_credential()
    db = MagicMock()

    create_resp = _resp({"id": "creation-1"})
    poll_resp = _resp({"status_code": "FINISHED"})
    publish_resp = _resp({"id": "media-99"})

    with (
        patch("engine.publish.instagram.settings.public_base_url", "https://reels.example.com"),
        patch("engine.publish.instagram.httpx.post", side_effect=[create_resp, publish_resp]) as mock_post,
        patch("engine.publish.instagram.httpx.get", return_value=poll_resp),
        patch("engine.publish.instagram.time.sleep"),
    ):
        result = InstagramPublisher().publish(cut, credential, db, caption=cut.caption)

    assert result.platform_post_id == "media-99"
    assert result.url == "https://www.instagram.com/reel/media-99/"

    create_call = mock_post.call_args_list[0]
    assert create_call.args[0] == "https://graph.facebook.com/v19.0/ig123/media"
    assert create_call.kwargs["data"]["video_url"] == "https://reels.example.com/api/cuts/42/video"
    assert create_call.kwargs["data"]["media_type"] == "REELS"

    publish_call = mock_post.call_args_list[1]
    assert publish_call.kwargs["data"]["creation_id"] == "creation-1"


def test_publish_uses_provided_caption_not_cut_caption():
    """The caption param (not cut.caption) must reach the container — this is
    where worker/tasks/publish.py injects the attribution block."""
    cut = _fake_cut()
    cut.caption = "Original caption, no attribution"
    credential = _fake_credential()
    db = MagicMock()

    create_resp = _resp({"id": "creation-1"})
    poll_resp = _resp({"status_code": "FINISHED"})
    publish_resp = _resp({"id": "media-99"})

    with (
        patch("engine.publish.instagram.settings.public_base_url", "https://reels.example.com"),
        patch("engine.publish.instagram.httpx.post", side_effect=[create_resp, publish_resp]) as mock_post,
        patch("engine.publish.instagram.httpx.get", return_value=poll_resp),
        patch("engine.publish.instagram.time.sleep"),
    ):
        InstagramPublisher().publish(
            cut, credential, db,
            caption="Original caption, no attribution\n\nImage credit: Jane Doe (CC BY-SA)",
        )

    create_call = mock_post.call_args_list[0]
    assert "Image credit" in create_call.kwargs["data"]["caption"]


def test_missing_provider_account_id_raises_before_any_call():
    cut = _fake_cut()
    credential = _fake_credential(provider_account_id=None)
    db = MagicMock()

    with patch("engine.publish.instagram.httpx.post") as mock_post:
        with pytest.raises(ValueError, match="no linked Business Account ID"):
            InstagramPublisher().publish(cut, credential, db, caption=cut.caption)
    mock_post.assert_not_called()


def test_container_creation_without_id_raises():
    cut = _fake_cut()
    credential = _fake_credential()
    db = MagicMock()

    with patch("engine.publish.instagram.httpx.post", return_value=_resp({})):
        with pytest.raises(ValueError, match="did not return a media container id"):
            InstagramPublisher().publish(cut, credential, db, caption=cut.caption)


def test_poll_error_status_raises():
    cut = _fake_cut()
    credential = _fake_credential()
    db = MagicMock()

    create_resp = _resp({"id": "creation-1"})
    error_resp = _resp({"status_code": "ERROR"})

    with (
        patch("engine.publish.instagram.httpx.post", return_value=create_resp),
        patch("engine.publish.instagram.httpx.get", return_value=error_resp) as mock_get,
        patch("engine.publish.instagram.time.sleep"),
    ):
        with pytest.raises(ValueError, match="status_code=ERROR"):
            InstagramPublisher().publish(cut, credential, db, caption=cut.caption)

    # Token in the Authorization header, not params — a params token leaks into
    # httpx.HTTPStatusError's __str__ on any failed poll.
    _, poll_kwargs = mock_get.call_args
    assert poll_kwargs["headers"]["Authorization"] == "Bearer page-access-tok"
    assert "access_token" not in poll_kwargs["params"]


def test_poll_timeout_raises_after_max_polls():
    cut = _fake_cut()
    credential = _fake_credential()
    db = MagicMock()

    create_resp = _resp({"id": "creation-1"})
    in_progress_resp = _resp({"status_code": "IN_PROGRESS"})

    with (
        patch("engine.publish.instagram.httpx.post", return_value=create_resp),
        patch("engine.publish.instagram.httpx.get", return_value=in_progress_resp) as mock_get,
        patch("engine.publish.instagram.time.sleep") as mock_sleep,
    ):
        with pytest.raises(ValueError, match="did not finish within"):
            InstagramPublisher().publish(cut, credential, db, caption=cut.caption)

    from engine.publish.instagram import _MAX_POLLS
    assert mock_get.call_count == _MAX_POLLS
    assert mock_sleep.call_count == _MAX_POLLS


def test_publish_container_without_id_raises():
    cut = _fake_cut()
    credential = _fake_credential()
    db = MagicMock()

    create_resp = _resp({"id": "creation-1"})
    poll_resp = _resp({"status_code": "FINISHED"})
    publish_resp = _resp({})  # no "id"

    with (
        patch("engine.publish.instagram.httpx.post", side_effect=[create_resp, publish_resp]),
        patch("engine.publish.instagram.httpx.get", return_value=poll_resp),
        patch("engine.publish.instagram.time.sleep"),
    ):
        with pytest.raises(ValueError, match="did not return a published media id"):
            InstagramPublisher().publish(cut, credential, db, caption=cut.caption)
