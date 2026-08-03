"""Tests for api/oauth.py — the generic OAuth2 authorization-code flow."""
from unittest.mock import MagicMock, patch

import pytest

from api.oauth import (
    InstagramOAuth,
    YouTubeOAuth,
    consume_state,
    get_oauth_provider,
    new_state,
)


# ── CSRF state ──────────────────────────────────────────────────────────────

def test_state_round_trip():
    state = new_state("youtube")
    assert consume_state(state) == "youtube"


def test_state_can_only_be_consumed_once():
    state = new_state("youtube")
    consume_state(state)
    assert consume_state(state) is None


def test_unknown_state_returns_none():
    assert consume_state("not-a-real-state") is None


def test_expired_state_returns_none():
    with patch("api.oauth.time.monotonic", return_value=1000.0):
        state = new_state("instagram")
    with patch("api.oauth.time.monotonic", return_value=1000.0 + 601):
        assert consume_state(state) is None


# ── YouTube ─────────────────────────────────────────────────────────────────

def test_youtube_authorize_url_contains_required_params():
    oauth = YouTubeOAuth("client-id", "secret", "https://example.com/cb")
    url = oauth.authorize_redirect_url("state123")
    assert "accounts.google.com" in url
    assert "client_id=client-id" in url
    assert "state=state123" in url
    assert "access_type=offline" in url


def test_youtube_exchange_code_posts_expected_payload():
    oauth = YouTubeOAuth("client-id", "secret", "https://example.com/cb")
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {"access_token": "tok", "refresh_token": "rtok", "expires_in": 3600}

    with patch("api.oauth.httpx.post", return_value=fake_resp) as mock_post:
        result = oauth.exchange_code("auth-code")

    assert result["access_token"] == "tok"
    _, kwargs = mock_post.call_args
    assert kwargs["data"]["code"] == "auth-code"
    assert kwargs["data"]["grant_type"] == "authorization_code"


# ── Instagram ───────────────────────────────────────────────────────────────

def test_instagram_authorize_url_contains_required_params():
    oauth = InstagramOAuth("app-id", "secret", "https://example.com/cb")
    url = oauth.authorize_redirect_url("state456")
    assert "facebook.com" in url
    assert "client_id=app-id" in url
    assert "state=state456" in url


def test_instagram_exchange_long_lived_token_requests_fb_exchange_grant():
    oauth = InstagramOAuth("app-id", "secret", "https://example.com/cb")
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {"access_token": "long-lived-tok", "expires_in": 5_183_944}

    with patch("api.oauth.httpx.get", return_value=fake_resp) as mock_get:
        result = oauth.exchange_long_lived_token("short-lived-tok")

    assert result["access_token"] == "long-lived-tok"
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["grant_type"] == "fb_exchange_token"
    assert kwargs["params"]["fb_exchange_token"] == "short-lived-tok"


def test_instagram_discover_account_finds_linked_ig_business_account():
    oauth = InstagramOAuth("app-id", "secret", "https://example.com/cb")

    pages_resp = MagicMock()
    pages_resp.raise_for_status.return_value = None
    pages_resp.json.return_value = {"data": [{"id": "page1", "access_token": "page-token"}]}

    ig_resp = MagicMock()
    ig_resp.raise_for_status.return_value = None
    ig_resp.json.return_value = {"instagram_business_account": {"id": "ig123"}}

    with patch("api.oauth.httpx.get", side_effect=[pages_resp, ig_resp]) as mock_get:
        account = oauth.discover_account("user-token")

    assert account == {"page_id": "page1", "page_access_token": "page-token", "ig_user_id": "ig123"}

    # Tokens must go in the Authorization header, not query params — a params
    # token leaks into httpx.HTTPStatusError's __str__ on any failed call.
    first_call, second_call = mock_get.call_args_list
    assert first_call.kwargs["headers"]["Authorization"] == "Bearer user-token"
    assert "access_token" not in (first_call.kwargs.get("params") or {})
    assert second_call.kwargs["headers"]["Authorization"] == "Bearer page-token"
    assert "access_token" not in (second_call.kwargs.get("params") or {})


def test_instagram_discover_account_raises_when_no_linked_account():
    oauth = InstagramOAuth("app-id", "secret", "https://example.com/cb")

    pages_resp = MagicMock()
    pages_resp.raise_for_status.return_value = None
    pages_resp.json.return_value = {"data": [{"id": "page1", "access_token": "page-token"}]}

    ig_resp = MagicMock()
    ig_resp.raise_for_status.return_value = None
    ig_resp.json.return_value = {}  # no instagram_business_account field

    with patch("api.oauth.httpx.get", side_effect=[pages_resp, ig_resp]):
        with pytest.raises(ValueError, match="No Facebook Page"):
            oauth.discover_account("user-token")


# ── provider registry ────────────────────────────────────────────────────────

def test_get_oauth_provider_returns_expected_types():
    assert isinstance(get_oauth_provider("youtube"), YouTubeOAuth)
    assert isinstance(get_oauth_provider("instagram"), InstagramOAuth)


def test_get_oauth_provider_raises_for_unknown():
    with pytest.raises(ValueError, match="Unknown OAuth provider"):
        get_oauth_provider("tiktok")


def test_redirect_uri_built_from_public_base_url():
    with patch("api.oauth.settings.public_base_url", "https://reels.example.com"):
        oauth = get_oauth_provider("youtube")
    assert oauth.redirect_uri == "https://reels.example.com/api/credentials/youtube/callback"
