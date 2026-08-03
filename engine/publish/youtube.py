"""YouTube Data API v3 publisher — resumable upload flow.

Docs: https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
Publishing rides on the connected account's OAuth token (api/oauth.py::YouTubeOAuth).
Reel videos are short (well under YouTube's resumable-upload chunk-size concerns),
so this does a single init-then-PUT rather than true multi-chunk resuming — the
two-step protocol is still followed, just with one upload request instead of many.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from api.oauth import get_oauth_provider
from engine.publish.base import Publisher, PublishResult

_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


def get_valid_access_token(credential, db) -> str:
    """Return a usable YouTube access token, refreshing it first if it has
    expired. Shared by YouTubePublisher (upload) and YouTubeMetricsFetcher
    (read-back) — Google access tokens expire in ~1h, so any caller that
    holds onto the raw token_blob without this check will start 401ing."""
    now = datetime.now(timezone.utc)
    if credential.expires_at and credential.expires_at <= now:
        if not credential.refresh_token_blob:
            raise ValueError(
                "YouTube access token expired and no refresh token is stored — "
                "reconnect the account at /api/credentials"
            )
        oauth = get_oauth_provider("youtube")
        tokens = oauth.refresh(credential.refresh_token_blob)
        credential.token_blob = tokens["access_token"]
        expires_in = tokens.get("expires_in")
        credential.expires_at = (
            now + timedelta(seconds=int(expires_in)) if expires_in else None
        )
        db.commit()
    return credential.token_blob


class YouTubePublisher(Publisher):
    def publish(self, cut, credential, db, caption: str) -> PublishResult:
        access_token = get_valid_access_token(credential, db)
        video_path = Path(cut.video_path)
        video_size = video_path.stat().st_size

        # "".splitlines() is [] — a whitespace-only caption would otherwise
        # IndexError on [0]. Fall back to "Reel" before splitting, not after.
        stripped_caption = (caption or "").strip() or "Reel"
        title = stripped_caption.splitlines()[0][:100]
        description = caption or ""
        if cut.hashtags:
            description += "\n\n" + " ".join(f"#{h}" for h in cut.hashtags[:15])

        metadata = {
            "snippet": {
                "title": title,
                "description": description[:5000],
                "tags": (cut.hashtags or [])[:15],
            },
            # Adult commentary/analysis content, not for kids — set explicitly
            # since YouTube requires a declaration on every upload.
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }

        init_resp = httpx.post(
            _UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(video_size),
            },
            json=metadata,
            timeout=30.0,
        )
        init_resp.raise_for_status()
        upload_url = init_resp.headers.get("Location")
        if not upload_url:
            raise ValueError("YouTube did not return a resumable upload session URL")

        video_bytes = video_path.read_bytes()
        upload_resp = httpx.put(
            upload_url,
            headers={"Content-Type": "video/mp4", "Content-Length": str(video_size)},
            content=video_bytes,
            timeout=300.0,
        )
        upload_resp.raise_for_status()
        video_id = upload_resp.json()["id"]

        return PublishResult(
            platform_post_id=video_id,
            url=f"https://youtube.com/shorts/{video_id}",
        )
