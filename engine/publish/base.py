"""Shared publisher interface.

Each concrete publisher (engine/publish/youtube.py, instagram.py, tiktok.py)
takes the rendered Cut plus its Credential and uploads the video, returning a
PublishResult. Raising is the failure-signaling mechanism — worker/tasks/
publish.py classifies transient vs deterministic failures the same way
generate_guide/render_cut already do (worker/tasks/common.py::should_retry).
"""
from dataclasses import dataclass


@dataclass
class PublishResult:
    platform_post_id: str
    url: str | None = None


class Publisher:
    def publish(self, cut, credential, db) -> PublishResult:
        raise NotImplementedError
