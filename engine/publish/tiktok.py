"""TikTok publisher — deliberately not implemented.

TikTok's Content Posting API requires a separate, audited app-review process
beyond basic developer app registration (see developers.tiktok.com/doc/content-
posting-api-get-started) — meaningfully different from YouTube/Instagram's
self-serve OAuth. Rather than ship an integration nobody has been able to
verify against a real audited app, this raises clearly so a publish attempt
fails with an honest message instead of silently doing nothing or pretending
to succeed. TikTok is still selectable as a render/review platform (see
CutPlatform.tiktok) — only publishing is gated here.
"""
from engine.publish.base import Publisher, PublishResult


class TikTokPublisher(Publisher):
    def publish(self, cut, credential, db) -> PublishResult:
        raise NotImplementedError(
            "TikTok publishing isn't implemented — the Content Posting API requires "
            "a separate audited app review. Render and review TikTok cuts, then "
            "upload manually for now. See docs/roadmap.md."
        )
