"""Cost estimation for HuggingFace Inference API asset generation, recorded on
StageEvent.cost_usd — the render-side counterpart to engine/generation/pricing.py.

Same honesty policy as that module's llm_cost_usd(): HF billing varies by plan
and hardware tier with no fixed public rate, so these default to 0 until the
operator fills in their actual rate from their HF billing plan.
"""
from api.config import settings


def hf_image_cost_usd() -> float:
    return settings.huggingface_price_per_image


def hf_video_cost_usd(duration_s: float) -> float:
    return max(0.0, duration_s) * settings.huggingface_price_per_video_second
