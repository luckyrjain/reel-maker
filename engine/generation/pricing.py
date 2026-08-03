"""Cost estimation for LLM calls, recorded on StageEvent.cost_usd.

Rates are operator-supplied via Settings (nvidia_price_per_1m_*) because hosted
pricing varies by plan and changes over time — we don't hardcode a number that
could silently misrepresent real spend. Both default to 0.0, so cost_usd is 0
until the operator fills in their actual NVIDIA NIM rate. Local Ollama calls are
always free (self-hosted) regardless of token counts.
"""
from api.config import settings


def llm_cost_usd(provider: str, tokens_in: int | None, tokens_out: int | None) -> float:
    """Estimate the USD cost of one LLM call from its token usage.

    Returns 0.0 for non-"nvidia" providers (local Ollama is free) or when no
    token counts are available (e.g. the call failed before returning usage).
    """
    if provider != "nvidia":
        return 0.0
    tokens_in = tokens_in or 0
    tokens_out = tokens_out or 0
    if not tokens_in and not tokens_out:
        return 0.0
    return (
        tokens_in / 1_000_000 * settings.nvidia_price_per_1m_input_tokens
        + tokens_out / 1_000_000 * settings.nvidia_price_per_1m_output_tokens
    )
