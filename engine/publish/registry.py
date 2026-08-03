"""Maps a Cut's platform to the Publisher that knows how to upload to it, and
to the MetricsFetcher that knows how to read engagement stats back for it."""
from engine.publish.base import Publisher
from engine.publish.instagram import InstagramPublisher
from engine.publish.metrics import InstagramMetricsFetcher, MetricsFetcher, YouTubeMetricsFetcher
from engine.publish.tiktok import TikTokPublisher
from engine.publish.youtube import YouTubePublisher

_PUBLISHERS: dict[str, Publisher] = {
    "youtube_shorts": YouTubePublisher(),
    "instagram_reels": InstagramPublisher(),
    "tiktok": TikTokPublisher(),
}

# No TikTok entry — publishing isn't implemented for it, so no cut ever
# reaches "published" with platform=tiktok to pull metrics for.
_METRICS_FETCHERS: dict[str, MetricsFetcher] = {
    "youtube_shorts": YouTubeMetricsFetcher(),
    "instagram_reels": InstagramMetricsFetcher(),
}

# CutPlatform values don't match Credential.provider values 1:1 (e.g. both
# youtube_shorts and a future youtube_long platform would use the "youtube"
# credential) — this is the one place that mapping is defined.
_PLATFORM_TO_CREDENTIAL_PROVIDER: dict[str, str] = {
    "youtube_shorts": "youtube",
    "instagram_reels": "instagram",
    "tiktok": "tiktok",
}


def get_publisher(platform: str) -> Publisher:
    try:
        return _PUBLISHERS[platform]
    except KeyError:
        raise ValueError(f"No publisher registered for platform '{platform}'") from None


def get_metrics_fetcher(platform: str) -> MetricsFetcher | None:
    """Returns None (not a raise) for a platform with no fetcher — the caller
    (worker/tasks/metrics.py) treats that as "skip this cut", not an error;
    unlike get_publisher(), an unfetchable platform isn't a bug."""
    return _METRICS_FETCHERS.get(platform)


def credential_provider_for_platform(platform: str) -> str:
    return _PLATFORM_TO_CREDENTIAL_PROVIDER.get(platform, platform)
