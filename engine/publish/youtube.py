"""YouTube Data API v3 publisher — resumable upload flow.

Docs: https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
Publishing rides on the connected account's OAuth token (api/oauth.py::YouTubeOAuth).
Reel videos are short (well under YouTube's resumable-upload chunk-size concerns),
so this does a single init-then-PUT rather than true multi-chunk resuming — the
two-step protocol is still followed, just with one upload request instead of many.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from api.oauth import get_oauth_provider
from engine.observability import record_stage
from engine.publish.base import Publisher, PublishResult

_log = logging.getLogger(__name__)

_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_CAPTIONS_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/captions"


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


def _build_multipart_related(parts: list[tuple[str, bytes]], boundary: str) -> bytes:
    """Build a `multipart/related` body per RFC 2387 / Google's `uploadType=multipart`
    upload protocol. This is NOT the same wire format as `multipart/form-data`
    (httpx's `files=` parameter) — `multipart/related` parts are distinguished only
    by their own `Content-Type` header, with no `Content-Disposition`/field names,
    which is what Google's upload endpoints for this protocol expect. Each `parts`
    entry is `(content_type, body_bytes)`."""
    body = bytearray()
    for content_type, content in parts:
        body += f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n".encode()
        body += content
        body += b"\r\n"
    body += f"--{boundary}--".encode()
    return bytes(body)


def _upload_captions(video_id: str, subtitle_path: str, access_token: str) -> None:
    """POST an SRT file to YouTube's captions.insert API for an already-uploaded
    video. Raises on any HTTP/network failure — the caller (YouTubePublisher.publish)
    is responsible for treating this as best-effort and never letting it fail the
    publish job; see that call site's record_stage() wrapper.

    Assumption flagged, not live-verified as of this implementation (see
    docs/specs/2026-09-srt-caption-export-system-design.md §3.3/§9.2): YouTube's
    Captions API is documented to accept raw SRT bytes as the media part with the
    format auto-detected from content. One real call against a real connected
    account is still needed before fully trusting this in production.
    """
    srt_bytes = Path(subtitle_path).read_bytes()
    snippet = {
        "snippet": {
            "videoId": video_id,
            "language": "en",
            "name": "",
            "isDraft": False,
        }
    }
    boundary = uuid.uuid4().hex
    body = _build_multipart_related(
        [
            ("application/json; charset=UTF-8", json.dumps(snippet).encode()),
            ("application/octet-stream", srt_bytes),
        ],
        boundary,
    )
    resp = httpx.post(
        _CAPTIONS_UPLOAD_URL,
        params={"uploadType": "multipart", "part": "snippet"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        content=body,
        timeout=30.0,
    )
    resp.raise_for_status()


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

        # Best-effort captions upload — the video is already live by this point.
        # MUST NOT fail this method: a captions-only failure must never surface as
        # "publish failed" for a post that, in fact, succeeded. record_stage() sets
        # ev.ok=False and commits a StageEvent on an exception raised inside its
        # `with` block, but then RE-RAISES — so the try/except goes INSIDE the
        # block and explicitly sets ev.ok/ev.detail on failure, rather than
        # wrapping the call with no inner try/except (which would let the
        # re-raise propagate and fail this publish) or catching outside the block
        # without touching ev (which would silently record a failed upload as the
        # default ok=True). See CLAUDE.md's Key conventions entry on this
        # record_stage composition rule and
        # docs/specs/2026-09-srt-caption-export-system-design.md §7.
        if cut.subtitle_path:
            with record_stage(
                db, cut.reel_id, "captions_upload", cut_id=cut.id, provider="youtube"
            ) as ev:
                try:
                    _upload_captions(video_id, cut.subtitle_path, access_token)
                except Exception as exc:
                    ev.ok = False
                    ev.detail["error"] = repr(exc)
                    _log.exception(
                        "caption upload failed for cut %s (video is live, video_id=%s)",
                        cut.id, video_id,
                    )

        return PublishResult(
            platform_post_id=video_id,
            url=f"https://youtube.com/shorts/{video_id}",
        )
