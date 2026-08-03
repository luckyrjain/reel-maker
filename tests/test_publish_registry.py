"""Tests for engine/publish/registry.py."""
import pytest

from engine.publish.instagram import InstagramPublisher
from engine.publish.registry import credential_provider_for_platform, get_publisher
from engine.publish.tiktok import TikTokPublisher
from engine.publish.youtube import YouTubePublisher


def test_get_publisher_returns_expected_types():
    assert isinstance(get_publisher("youtube_shorts"), YouTubePublisher)
    assert isinstance(get_publisher("instagram_reels"), InstagramPublisher)
    assert isinstance(get_publisher("tiktok"), TikTokPublisher)


def test_get_publisher_raises_for_unknown_platform():
    with pytest.raises(ValueError, match="No publisher registered"):
        get_publisher("myspace_reels")


def test_credential_provider_mapping():
    assert credential_provider_for_platform("youtube_shorts") == "youtube"
    assert credential_provider_for_platform("instagram_reels") == "instagram"
    assert credential_provider_for_platform("tiktok") == "tiktok"


def test_tiktok_publisher_raises_clearly():
    with pytest.raises(NotImplementedError, match="audited app review"):
        TikTokPublisher().publish(cut=None, credential=None, db=None)
