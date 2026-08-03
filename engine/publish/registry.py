"""Maps a Cut's platform to the Publisher that knows how to upload to it."""
from engine.publish.base import Publisher
from engine.publish.instagram import InstagramPublisher
from engine.publish.tiktok import TikTokPublisher
from engine.publish.youtube import YouTubePublisher

_PUBLISHERS: dict[str, Publisher] = {
    "youtube_shorts": YouTubePublisher(),
    "instagram_reels": InstagramPublisher(),
    "tiktok": TikTokPublisher(),
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


def credential_provider_for_platform(platform: str) -> str:
    return _PLATFORM_TO_CREDENTIAL_PROVIDER.get(platform, platform)
