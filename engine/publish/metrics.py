"""Post-publish engagement metrics pull-back.

Fetches view/like/comment counts for already-published cuts from each
platform's read API. Read-only — never touches publish state, credentials,
or the publish_cut retry machinery. worker/tasks/metrics.py orchestrates
calling these on a schedule.
"""
from dataclasses import dataclass

import httpx


@dataclass
class EngagementMetrics:
    views: int | None = None
    likes: int | None = None
    comments: int | None = None


class MetricsFetcher:
    def fetch(self, cut, credential) -> EngagementMetrics | None:
        """Return the latest metrics, or None if the platform has no data yet
        (e.g. a video that was just published). Raises on a real API failure —
        the caller is responsible for catching per-cut so one failure doesn't
        abort the whole pull."""
        raise NotImplementedError


class YouTubeMetricsFetcher(MetricsFetcher):
    """videos.list?part=statistics — public video statistics, stable since the
    Data API v3's introduction. No extra scope beyond the stored upload token
    is required to read it."""

    def fetch(self, cut, credential) -> EngagementMetrics | None:
        resp = httpx.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "statistics", "id": cut.platform_post_id},
            headers={"Authorization": f"Bearer {credential.token_blob}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return None
        stats = items[0].get("statistics", {})
        return EngagementMetrics(
            views=int(stats["viewCount"]) if "viewCount" in stats else None,
            likes=int(stats["likeCount"]) if "likeCount" in stats else None,
            comments=int(stats["commentCount"]) if "commentCount" in stats else None,
        )


class InstagramMetricsFetcher(MetricsFetcher):
    """Graph API media insights for a published Reel.

    Lower confidence than the YouTube fetcher: Meta has changed Insights
    metric names for Reels more than once (e.g. "plays" vs "video_views" at
    different API versions). Verify this metric list against the Graph API
    version you're actually targeting before relying on it — see
    developers.facebook.com/docs/instagram-api/guides/insights.
    """

    _METRICS = "plays,likes,comments"

    def fetch(self, cut, credential) -> EngagementMetrics | None:
        # Token in the Authorization header, not params — see discover_account()'s
        # docstring in api/oauth.py for why (leaks into httpx.HTTPStatusError's
        # __str__ on any GET request that fails, which _pull_one() logs).
        resp = httpx.get(
            f"https://graph.facebook.com/v19.0/{cut.platform_post_id}/insights",
            params={"metric": self._METRICS},
            headers={"Authorization": f"Bearer {credential.token_blob}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None
        by_name = {d["name"]: d["values"][0]["value"] for d in data if d.get("values")}
        return EngagementMetrics(
            views=by_name.get("plays"),
            likes=by_name.get("likes"),
            comments=by_name.get("comments"),
        )
