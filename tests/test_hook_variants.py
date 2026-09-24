"""Tests for engine/generation/hook_variants.py — best-effort alternate hook lines."""
import json
from unittest.mock import MagicMock

from engine.generation.hook_variants import N_VARIANTS, generate_hook_variants


def _llm(raw):
    llm = MagicMock()
    llm.complete.return_value = raw
    return llm


def test_returns_parsed_variants_from_a_plain_json_list():
    variants = generate_hook_variants(
        "You won't believe this.", "context", "football",
        _llm(json.dumps(["Alt hook one", "Alt hook two"])),
    )
    assert variants == ["Alt hook one", "Alt hook two"]


def test_unwraps_a_dict_response_with_a_nested_list():
    variants = generate_hook_variants(
        "Original hook.", "context", "football",
        _llm(json.dumps({"hooks": ["Alt A", "Alt B"]})),
    )
    assert variants == ["Alt A", "Alt B"]


def test_excludes_a_variant_identical_to_the_original():
    variants = generate_hook_variants(
        "Same hook.", "context", "football",
        _llm(json.dumps(["Same hook.", "Different hook."])),
    )
    assert variants == ["Different hook."]


def test_caps_at_n_variants():
    variants = generate_hook_variants(
        "Original.", "context", "football",
        _llm(json.dumps([f"Alt {i}" for i in range(10)])),
    )
    assert len(variants) == N_VARIANTS


def test_empty_hook_vo_returns_empty_without_calling_the_llm():
    llm = _llm(json.dumps(["should not be seen"]))
    assert generate_hook_variants("   ", "context", "football", llm) == []
    llm.complete.assert_not_called()


def test_malformed_json_returns_empty_list():
    assert generate_hook_variants("Original.", "context", "football", _llm("not json")) == []


def test_non_list_non_dict_json_returns_empty_list():
    assert generate_hook_variants("Original.", "context", "football", _llm(json.dumps(42))) == []


def test_llm_exception_is_swallowed():
    llm = MagicMock()
    llm.complete.side_effect = RuntimeError("provider down")
    assert generate_hook_variants("Original.", "context", "football", llm) == []


def test_non_string_items_in_the_list_are_dropped():
    variants = generate_hook_variants(
        "Original.", "context", "football",
        _llm(json.dumps(["Good one", 42, None, "  ", "Another good one"])),
    )
    assert variants == ["Good one", "Another good one"]
