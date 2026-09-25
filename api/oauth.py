"""Generic OAuth2 authorization-code flow for connecting publish-target accounts.

Two providers are wired up: "youtube" (Google) and "instagram" (Meta Graph API,
via a Facebook Page linked to an Instagram Business/Creator account). Tokens
land in the `credentials` table, encrypted at rest via api.crypto.Encrypted
(see models.Credential — token_blob/refresh_token_blob are Encrypted columns,
so callers just assign plain strings to them).

CSRF state is a short-lived, process-local store rather than a DB table or
signed token — this is a local-first, single-operator tool (CLAUDE.md), run as
a single FastAPI process, so there is no multi-worker state to coordinate.
A server restart mid-flow simply invalidates any pending authorize request,
which is an acceptable, rare failure mode here.
"""
import secrets
import time

import httpx

from api.config import settings

_STATE_TTL_S = 600
_pending_states: dict[str, tuple[str, float]] = {}

SUPPORTED_PROVIDERS = ("youtube", "instagram")


def _prune_expired_states() -> None:
    now = time.monotonic()
    for s in [s for s, (_, exp) in _pending_states.items() if exp < now]:
        _pending_states.pop(s, None)


def new_state(provider: str) -> str:
    """Mint a CSRF state token for an OAuth authorize redirect."""
    _prune_expired_states()
    state = secrets.token_urlsafe(32)
    _pending_states[state] = (provider, time.monotonic() + _STATE_TTL_S)
    return state


def consume_state(state: str) -> str | None:
    """Validate + consume a CSRF state token once. Returns the provider name, or
    None if the state is unknown, already used, or expired."""
    _prune_expired_states()
    entry = _pending_states.pop(state, None)
    return entry[0] if entry else None


class OAuthProvider:
    name: str
    authorize_url: str
    token_url: str
    scope: str

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorize_redirect_url(self, state: str) -> str:
        raise NotImplementedError

    def exchange_code(self, code: str) -> dict:
        """Exchange an authorization code for tokens. Returns the raw provider JSON."""
        raise NotImplementedError


class YouTubeOAuth(OAuthProvider):
    name = "youtube"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    # youtube.upload alone is NOT sufficient for captions.insert (Phase 5d's best-effort
    # caption-track upload, engine/publish/youtube.py::_upload_captions) — Google's Captions
    # API documents youtube.force-ssl (or youtubepartner) as required. Widened here rather
    # than left narrow, since a previously-connected account would otherwise 403 forever on
    # every caption upload with no way to fix it short of reconnecting anyway — reconnecting
    # is required either way for an already-connected account to pick up the wider scope.
    scope = (
        "https://www.googleapis.com/auth/youtube.upload "
        "https://www.googleapis.com/auth/youtube.force-ssl"
    )

    def authorize_redirect_url(self, state: str) -> str:
        params = httpx.QueryParams({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "access_type": "offline",  # required to receive a refresh_token
            "prompt": "consent",
            "state": state,
        })
        return f"{self.authorize_url}?{params}"

    def exchange_code(self, code: str) -> dict:
        resp = httpx.post(self.token_url, data={
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    def refresh(self, refresh_token: str) -> dict:
        resp = httpx.post(self.token_url, data={
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }, timeout=30.0)
        resp.raise_for_status()
        return resp.json()


class InstagramOAuth(OAuthProvider):
    """OAuth via Meta's Facebook Login — Instagram publishing rides on a
    connected Facebook Page's access token, not a token issued to a personal
    Instagram account directly."""

    name = "instagram"
    authorize_url = "https://www.facebook.com/v19.0/dialog/oauth"
    token_url = "https://graph.facebook.com/v19.0/oauth/access_token"
    scope = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement"

    def authorize_redirect_url(self, state: str) -> str:
        params = httpx.QueryParams({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "response_type": "code",
            "state": state,
        })
        return f"{self.authorize_url}?{params}"

    def exchange_code(self, code: str) -> dict:
        # POST with a form body, not GET with query params — RFC 6749 §3.2
        # requires every OAuth2 token endpoint to support POST specifically so
        # client_secret never has to ride in a URL (query strings land in
        # server/proxy logs and, via httpx.HTTPStatusError's __str__() on any
        # failed call, in job.error / HTTPException responses — the same class
        # of leak fixed elsewhere in this module for bearer tokens). Matches
        # YouTubeOAuth.exchange_code()'s pattern in this same file.
        resp = httpx.post(self.token_url, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "code": code,
        }, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    def exchange_long_lived_token(self, short_lived_token: str) -> dict:
        """Swap the ~1-2h token exchange_code() returns for a ~60-day one.

        Facebook Page access tokens derived from a long-lived user token don't
        expire under normal use — this matters even though publishing ultimately
        uses the Page token (from discover_account), not the user token itself,
        because the Page token inherits its lifetime from the user token it was
        derived from. Skipping this step would leave a connected account
        needing re-authorization every 1-2 hours instead of ~60 days.
        """
        # POST body, not GET params — see exchange_code()'s comment; the same
        # leak applies here to both client_secret and the short-lived token.
        resp = httpx.post(self.token_url, data={
            "grant_type": "fb_exchange_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "fb_exchange_token": short_lived_token,
        }, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    def discover_account(self, user_access_token: str) -> dict:
        """Find the Instagram Business Account behind the user's Facebook Pages.

        Returns {"page_id", "page_access_token", "ig_user_id"} for the first
        page with a linked Instagram Business/Creator account. Publish calls use
        the *page* access token, not the user token exchange_code() returned.

        Tokens go in the Authorization header, not query params — Graph API
        supports both, but httpx.HTTPStatusError's default __str__ includes the
        full request URL, so a token passed via params would leak into any log
        or error message a failed call bubbles up to (worker/tasks/*.py's
        `job.error = str(exc)`, in particular).
        """
        resp = httpx.get(
            "https://graph.facebook.com/v19.0/me/accounts",
            headers={"Authorization": f"Bearer {user_access_token}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        for page in resp.json().get("data", []):
            page_id, page_token = page["id"], page["access_token"]
            ig_resp = httpx.get(
                f"https://graph.facebook.com/v19.0/{page_id}",
                params={"fields": "instagram_business_account"},
                headers={"Authorization": f"Bearer {page_token}"},
                timeout=30.0,
            )
            ig_resp.raise_for_status()
            ig_account = ig_resp.json().get("instagram_business_account")
            if ig_account:
                return {"page_id": page_id, "page_access_token": page_token, "ig_user_id": ig_account["id"]}
        raise ValueError(
            "No Facebook Page with a linked Instagram Business/Creator account was found. "
            "In the Instagram app: Settings → Account type → switch to Professional, "
            "then connect it to a Facebook Page you manage."
        )


def get_oauth_provider(provider: str) -> OAuthProvider:
    redirect_uri = f"{settings.public_base_url.rstrip('/')}/api/credentials/{provider}/callback"
    if provider == "youtube":
        return YouTubeOAuth(
            settings.youtube_oauth_client_id, settings.youtube_oauth_client_secret, redirect_uri
        )
    if provider == "instagram":
        return InstagramOAuth(
            settings.meta_oauth_app_id, settings.meta_oauth_app_secret, redirect_uri
        )
    raise ValueError(f"Unknown OAuth provider: {provider}")
