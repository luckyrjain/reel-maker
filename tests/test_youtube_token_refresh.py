"""The YouTube access-token refresh, end to end.

`YouTubeOAuth.refresh()` is replaced by a mock in every other test (`fake_oauth.refresh` in
test_youtube_publisher.py and test_metrics_fetcher.py), so its request, its error path and the
real call chain `get_valid_access_token -> get_oauth_provider -> YouTubeOAuth.refresh -> httpx`
never ran. Google access tokens last about an hour and metrics are pulled every 6 hours, so this
is the COMMON path of every scheduled pull, not an edge case.

The HTTP layer is faked at `httpx.post` (the one call `refresh()` makes), but a real
`httpx.Client` with a `MockTransport` builds the request, so the form encoding that Google would
receive is checked as sent.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import httpx
import pytest

from api.oauth import YouTubeOAuth
from engine.publish.youtube import get_valid_access_token

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SETTINGS = SimpleNamespace(
    public_base_url="https://app.example.com",
    youtube_oauth_client_id="client-id-1",
    youtube_oauth_client_secret="client-secret-2",
    meta_oauth_app_id="",
    meta_oauth_app_secret="",
)


class _Google:
    """Stands in for `httpx.post` and records the requests it was given. It answers through a
    real httpx.Client(MockTransport), so the request is built exactly as httpx would send it."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = {"access_token": "fresh-tok", "expires_in": 3599, "token_type": "Bearer"} if body is None else body
        self.requests: list[httpx.Request] = []
        self.timeouts: list = []

    def __call__(self, url, *, data=None, timeout=None, **kwargs):
        self.timeouts.append(timeout)

        def handler(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.body, request=request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.post(url, data=data, timeout=timeout)

    @property
    def form(self) -> dict[str, str]:
        assert len(self.requests) == 1, f"expected exactly one request, got {len(self.requests)}"
        return {k: v[0] for k, v in parse_qs(self.requests[0].content.decode()).items()}


def _provider():
    return YouTubeOAuth("client-id-1", "client-secret-2", "https://app.example.com/cb")


def _credential(*, expires_in_s=-3600, refresh_token="refresh-tok-3", token="stale-tok", expires_at="auto"):
    if expires_at == "auto":
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_s)
    return SimpleNamespace(token_blob=token, refresh_token_blob=refresh_token, expires_at=expires_at)


# ── YouTubeOAuth.refresh() itself ───────────────────────────────────────────

def test_refresh_posts_a_form_encoded_refresh_grant_to_googles_token_endpoint():
    google = _Google()
    with patch("api.oauth.httpx.post", side_effect=google):
        _provider().refresh("refresh-tok-3")

    request = google.requests[0]
    assert request.method == "POST"
    assert str(request.url) == _TOKEN_URL
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert google.form == {
        "refresh_token": "refresh-tok-3",
        "client_id": "client-id-1",
        "client_secret": "client-secret-2",
        "grant_type": "refresh_token",
    }


def test_refresh_keeps_the_refresh_token_and_client_secret_out_of_the_url():
    google = _Google()
    with patch("api.oauth.httpx.post", side_effect=google):
        _provider().refresh("refresh-tok-3")

    url = str(google.requests[0].url)
    assert "?" not in url
    assert "refresh-tok-3" not in url and "client-secret-2" not in url


def test_refresh_sends_a_timeout():
    google = _Google()
    with patch("api.oauth.httpx.post", side_effect=google):
        _provider().refresh("refresh-tok-3")

    assert google.timeouts == [30.0]


def test_refresh_returns_the_providers_json_body_unchanged():
    body = {"access_token": "fresh-tok", "expires_in": 3599, "scope": "a b", "token_type": "Bearer"}
    with patch("api.oauth.httpx.post", side_effect=_Google(body=body)):
        assert _provider().refresh("refresh-tok-3") == body


def test_refresh_raises_on_a_rejected_grant_without_echoing_the_secrets():
    """A revoked or expired refresh token is answered with 400 invalid_grant."""
    google = _Google(status=400, body={"error": "invalid_grant", "error_description": "Token has been revoked."})
    with patch("api.oauth.httpx.post", side_effect=google):
        with pytest.raises(httpx.HTTPStatusError) as caught:
            _provider().refresh("refresh-tok-3")

    assert caught.value.response.status_code == 400
    text = str(caught.value)
    assert "refresh-tok-3" not in text and "client-secret-2" not in text   # lands in job.error / logs


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_refresh_raises_for_every_error_status(status):
    with patch("api.oauth.httpx.post", side_effect=_Google(status=status, body={"error": "x"})):
        with pytest.raises(httpx.HTTPStatusError):
            _provider().refresh("refresh-tok-3")


# ── get_valid_access_token through the real provider ────────────────────────

@pytest.fixture()
def real_provider():
    """Run the real get_oauth_provider("youtube") on known settings instead of mocking it."""
    with patch("api.oauth.settings", _SETTINGS):
        yield


def test_an_expired_token_is_refreshed_through_the_real_provider_and_persisted(real_provider):
    google = _Google(body={"access_token": "fresh-tok", "expires_in": 3599})
    credential = _credential(expires_in_s=-60)
    db = MagicMock()
    before = datetime.now(timezone.utc)

    with patch("api.oauth.httpx.post", side_effect=google):
        token = get_valid_access_token(credential, db)

    assert token == "fresh-tok" and credential.token_blob == "fresh-tok"
    assert google.form["refresh_token"] == "refresh-tok-3"           # the stored one was used
    assert google.form["client_id"] == "client-id-1" and google.form["client_secret"] == "client-secret-2"
    assert credential.refresh_token_blob == "refresh-tok-3"          # Google sends no new one: keep ours
    assert before + timedelta(seconds=3599) <= credential.expires_at <= datetime.now(timezone.utc) + timedelta(seconds=3599)
    db.commit.assert_called_once()


def test_a_token_that_is_still_valid_is_returned_without_any_request(real_provider):
    google = _Google()
    credential = _credential(expires_in_s=3600)
    db = MagicMock()

    with patch("api.oauth.httpx.post", side_effect=google):
        assert get_valid_access_token(credential, db) == "stale-tok"

    assert google.requests == []
    db.commit.assert_not_called()


def test_a_credential_without_an_expiry_is_never_refreshed(real_provider):
    google = _Google()
    credential = _credential(expires_at=None)
    db = MagicMock()

    with patch("api.oauth.httpx.post", side_effect=google):
        assert get_valid_access_token(credential, db) == "stale-tok"

    assert google.requests == []
    db.commit.assert_not_called()


def test_a_failed_refresh_leaves_the_credential_untouched_and_commits_nothing(real_provider):
    google = _Google(status=400, body={"error": "invalid_grant"})
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    credential = _credential(expires_at=expired_at)
    db = MagicMock()

    with patch("api.oauth.httpx.post", side_effect=google):
        with pytest.raises(httpx.HTTPStatusError):
            get_valid_access_token(credential, db)

    assert credential.token_blob == "stale-tok"
    assert credential.refresh_token_blob == "refresh-tok-3"
    assert credential.expires_at == expired_at
    db.commit.assert_not_called()


def test_a_refresh_response_without_an_access_token_does_not_half_update_the_credential(real_provider):
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    credential = _credential(expires_at=expired_at)
    db = MagicMock()

    with patch("api.oauth.httpx.post", side_effect=_Google(body={"expires_in": 3599})):
        with pytest.raises(KeyError):
            get_valid_access_token(credential, db)

    assert credential.token_blob == "stale-tok" and credential.expires_at == expired_at
    db.commit.assert_not_called()


def test_an_expired_token_with_no_refresh_token_fails_before_any_request(real_provider):
    google = _Google()
    credential = _credential(refresh_token=None)
    db = MagicMock()

    with patch("api.oauth.httpx.post", side_effect=google):
        with pytest.raises(ValueError, match="reconnect the account"):
            get_valid_access_token(credential, db)

    assert google.requests == []
    assert credential.token_blob == "stale-tok"
    db.commit.assert_not_called()


def test_expires_in_given_as_a_string_is_accepted(real_provider):
    credential = _credential(expires_in_s=-60)
    before = datetime.now(timezone.utc)

    with patch("api.oauth.httpx.post", side_effect=_Google(body={"access_token": "t", "expires_in": "1800"})):
        get_valid_access_token(credential, MagicMock())

    assert before + timedelta(seconds=1800) <= credential.expires_at <= datetime.now(timezone.utc) + timedelta(seconds=1800)


def test_a_response_without_expires_in_stores_no_expiry_current_behaviour(real_provider):
    """Characterization: with no `expires_in` the new token gets expires_at=None, which later
    reads as "never expires" (see the test above), so it would not be refreshed again. Google
    always sends expires_in, so this is not reachable today; it is pinned so a change is deliberate."""
    credential = _credential(expires_in_s=-60)

    with patch("api.oauth.httpx.post", side_effect=_Google(body={"access_token": "t"})):
        get_valid_access_token(credential, MagicMock())

    assert credential.token_blob == "t" and credential.expires_at is None


def test_the_refreshed_token_is_what_the_metrics_fetcher_sends_to_the_api(real_provider):
    """The chain a scheduled pull actually takes: expired credential -> real refresh -> API call."""
    from engine.publish.metrics import YouTubeMetricsFetcher

    google = _Google(body={"access_token": "fresh-tok", "expires_in": 3599})
    credential = _credential(expires_in_s=-60)
    cut = SimpleNamespace(platform_post_id="yt-abc123")
    api = MagicMock()
    api.raise_for_status.return_value = None
    api.json.return_value = {"items": [{"statistics": {"viewCount": "7"}}]}

    with (
        patch("api.oauth.httpx.post", side_effect=google),
        patch("engine.publish.metrics.httpx.get", return_value=api) as mock_get,
    ):
        result = YouTubeMetricsFetcher().fetch(cut, credential, MagicMock())

    assert result.views == 7
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer fresh-tok"
