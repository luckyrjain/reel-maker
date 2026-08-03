"""Tests for engine/generation/pricing.py cost estimation."""
from unittest.mock import patch

from engine.generation.pricing import llm_cost_usd


def test_ollama_provider_is_always_free():
    assert llm_cost_usd("ollama", 1_000_000, 1_000_000) == 0.0


def test_nvidia_with_zero_configured_rates_is_free():
    """Default settings ship with 0.0 rates — cost tracking is inert until configured."""
    assert llm_cost_usd("nvidia", 1_000_000, 1_000_000) == 0.0


def test_nvidia_cost_scales_with_configured_rates():
    with (
        patch("engine.generation.pricing.settings.nvidia_price_per_1m_input_tokens", 2.0),
        patch("engine.generation.pricing.settings.nvidia_price_per_1m_output_tokens", 6.0),
    ):
        cost = llm_cost_usd("nvidia", 1_000_000, 500_000)
    assert cost == 2.0 + 3.0


def test_none_token_counts_treated_as_zero():
    with (
        patch("engine.generation.pricing.settings.nvidia_price_per_1m_input_tokens", 2.0),
        patch("engine.generation.pricing.settings.nvidia_price_per_1m_output_tokens", 6.0),
    ):
        assert llm_cost_usd("nvidia", None, None) == 0.0
