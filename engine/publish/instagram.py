"""Instagram Graph API publisher — container-create/poll/publish flow (Reels).

Docs: https://developers.facebook.com/docs/instagram-api/guides/content-publishing
Instagram's API requires a publicly reachable HTTPS video_url — it fetches the
file itself rather than accepting an upload body, so this only works when
settings.public_base_url points at a real public address serving
GET /api/cuts/{id}/video. Publishing rides on the connected Facebook Page's
access token (credential.token_blob) and the linked Instagram Business Account
ID (credential.provider_account_id), both set during OAuth (api/oauth.py
InstagramOAuth.discover_account, api/routers/credentials.py).
"""
import time

import httpx

from api.config import settings
from engine.publish.base import Publisher, PublishResult

_GRAPH = "https://graph.facebook.com/v19.0"
_POLL_INTERVAL_S = 5.0
_MAX_POLLS = 36  # ~3 min total — generous for a short-form clip


class InstagramPublisher(Publisher):
    def publish(self, cut, credential, db) -> PublishResult:
        if not credential.provider_account_id:
            raise ValueError(
                "Connected Instagram account has no linked Business Account ID — "
                "reconnect at /api/credentials"
            )
        access_token = credential.token_blob
        ig_user_id = credential.provider_account_id
        video_url = f"{settings.public_base_url.rstrip('/')}/api/cuts/{cut.id}/video"

        creation_id = self._create_container(ig_user_id, access_token, video_url, cut.caption or "")
        self._wait_until_ready(creation_id, access_token)
        media_id = self._publish_container(ig_user_id, access_token, creation_id)

        return PublishResult(
            platform_post_id=media_id, url=f"https://www.instagram.com/reel/{media_id}/"
        )

    def _create_container(self, ig_user_id: str, access_token: str, video_url: str, caption: str) -> str:
        resp = httpx.post(
            f"{_GRAPH}/{ig_user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption[:2200],  # Instagram's caption length cap
                "access_token": access_token,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        creation_id = resp.json().get("id")
        if not creation_id:
            raise ValueError("Instagram did not return a media container id")
        return creation_id

    def _wait_until_ready(self, creation_id: str, access_token: str) -> None:
        """Poll the container until Instagram finishes fetching/processing the video.

        Raises on ERROR or timeout — both are treated as deterministic failures
        (worker/tasks/common.py::should_retry) rather than retried, since a fresh
        Celery retry would create a brand-new container from scratch anyway.
        """
        for _ in range(_MAX_POLLS):
            resp = httpx.get(
                f"{_GRAPH}/{creation_id}",
                params={"fields": "status_code", "access_token": access_token},
                timeout=30.0,
            )
            resp.raise_for_status()
            status = resp.json().get("status_code")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise ValueError("Instagram failed to process the uploaded video (status_code=ERROR)")
            time.sleep(_POLL_INTERVAL_S)
        raise ValueError(
            f"Instagram video processing did not finish within "
            f"{_MAX_POLLS * _POLL_INTERVAL_S:.0f}s — try publishing again"
        )

    def _publish_container(self, ig_user_id: str, access_token: str, creation_id: str) -> str:
        resp = httpx.post(
            f"{_GRAPH}/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": access_token},
            timeout=30.0,
        )
        resp.raise_for_status()
        media_id = resp.json().get("id")
        if not media_id:
            raise ValueError("Instagram did not return a published media id")
        return media_id
