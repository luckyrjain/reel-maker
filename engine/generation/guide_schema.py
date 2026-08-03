from typing import Literal
from pydantic import BaseModel, Field, field_validator


_CTA_ALIASES = {"cta", "closing", "outro", "end", "call_to_action", "call to action"}
_HOOK_ALIASES = {"hook", "intro", "open", "opening"}

class Beat(BaseModel):
    index: int
    type: Literal["hook", "body", "cta"]

    @field_validator("type", mode="before")
    @classmethod
    def coerce_beat_type(cls, v: object) -> str:
        if isinstance(v, str):
            norm = v.lower().replace("-", "_").replace(" ", "_")
            if norm in _CTA_ALIASES:
                return "cta"
            if norm in _HOOK_ALIASES:
                return "hook"
            if norm not in ("hook", "body", "cta"):
                return "body"
        return v
    duration_s: float = Field(gt=0, le=30)
    visual_direction: str = Field(
        description="Specific stock footage search query, e.g. 'person counting cash at desk closeup'"
    )
    on_screen_text: list[str] = Field(
        description="1–3 short text overlays, max 5 words each"
    )
    vo_script: str = Field(
        description="Voiceover speech text. Empty string '' if voiceover_mode is not 'voiceover'."
    )
    music_cue: str | None = Field(
        default=None,
        description="Music mood keyword, e.g. 'upbeat motivational'. None to inherit from previous beat.",
    )
    transition: Literal["cut", "fade", "slide"] = "cut"

    @field_validator("on_screen_text", mode="before")
    @classmethod
    def coerce_to_list(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [v] if v.strip() else []
        return v  # type: ignore[return-value]


class PlatformGuide(BaseModel):
    platform: Literal["youtube_shorts", "instagram_reels", "tiktok"]
    target_length_s: float
    beats: list[Beat] = Field(
        min_length=3,
        description=(
            "Enough beats to fill target_length_s (≥3, no hard cap). "
            "First beat type must be 'hook' (1.5–3s). "
            "Last beat type must be 'cta'. Beat durations must sum to ~target_length_s."
        ),
    )
    caption: str = Field(description="Post caption, 1–2 punchy sentences")
    hashtags: list[str] = Field(
        min_length=5,
        max_length=25,
        description="15 hashtags — 5 broad, 5 niche, 5 trending-style. No # prefix.",
    )

    @field_validator("hashtags", mode="before")
    @classmethod
    def coerce_hashtags(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [t.strip().lstrip("#") for t in v.split(",") if t.strip()]
        return v  # type: ignore[return-value]


class MasterGuide(BaseModel):
    title: str = Field(description="Short catchy internal title for this reel")
    niche: str
    cuts: list[PlatformGuide] = Field(
        description="One PlatformGuide per requested platform"
    )
