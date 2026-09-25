"""Tests for OllamaProvider's token usage capture (engine/generation/llm.py)."""
import httpx
from unittest.mock import MagicMock, patch

from engine.generation.llm import (
    OllamaProvider, check_model_available, validate_configured_models,
)


def _fake_response(content="hello", usage=None):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
        **({"usage": usage} if usage is not None else {}),
    }
    return resp


def test_complete_captures_last_usage():
    provider = OllamaProvider(base_url="http://x", model="m")
    usage = {"prompt_tokens": 100, "completion_tokens": 40}
    with patch("engine.generation.llm.httpx.post", return_value=_fake_response(usage=usage)):
        result = provider.complete([{"role": "user", "content": "hi"}])
    assert result == "hello"
    assert provider.last_usage == usage


def test_complete_accumulates_total_usage_across_calls():
    provider = OllamaProvider(base_url="http://x", model="m")
    with patch(
        "engine.generation.llm.httpx.post",
        return_value=_fake_response(usage={"prompt_tokens": 10, "completion_tokens": 5}),
    ):
        provider.complete([{"role": "user", "content": "a"}])
        provider.complete([{"role": "user", "content": "b"}])
    assert provider.total_usage == {"prompt_tokens": 20, "completion_tokens": 10}


def test_complete_missing_usage_defaults_to_empty():
    provider = OllamaProvider(base_url="http://x", model="m")
    with patch("engine.generation.llm.httpx.post", return_value=_fake_response(usage=None)):
        provider.complete([{"role": "user", "content": "hi"}])
    assert provider.last_usage == {}
    assert provider.total_usage == {"prompt_tokens": 0, "completion_tokens": 0}


def test_failed_call_clears_last_usage_and_does_not_add_to_total():
    """A failed call must not leak a prior call's tokens into cost accounting."""
    provider = OllamaProvider(base_url="http://x", model="m")
    ok_resp = _fake_response(usage={"prompt_tokens": 10, "completion_tokens": 5})
    with patch("engine.generation.llm.httpx.post", return_value=ok_resp):
        provider.complete([{"role": "user", "content": "a"}])
    assert provider.last_usage == {"prompt_tokens": 10, "completion_tokens": 5}

    failing_resp = MagicMock()
    failing_resp.raise_for_status.side_effect = RuntimeError("boom")
    with patch("engine.generation.llm.httpx.post", return_value=failing_resp):
        try:
            provider.complete([{"role": "user", "content": "b"}])
        except RuntimeError:
            pass

    assert provider.last_usage == {}
    assert provider.total_usage == {"prompt_tokens": 10, "completion_tokens": 5}


# ── check_model_available / validate_configured_models ──────────────────────────

def _models_response(ids):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": [{"id": i} for i in ids]}
    return resp


def test_check_model_available_true_when_model_is_listed():
    with patch("engine.generation.llm.httpx.get", return_value=_models_response(["qwen3:14b", "llama3.1:8b"])):
        ok, detail = check_model_available("http://x", "qwen3:14b", {})
    assert ok is True
    assert detail == ""


def test_check_model_available_false_when_model_is_not_listed():
    with patch("engine.generation.llm.httpx.get", return_value=_models_response(["llama3.1:8b"])):
        ok, detail = check_model_available("http://x", "qwen3:14b", {})
    assert ok is False
    assert "qwen3:14b" in detail and "llama3.1:8b" in detail


def test_check_model_available_false_on_timeout():
    with patch("engine.generation.llm.httpx.get", side_effect=httpx.TimeoutException("slow")):
        ok, detail = check_model_available("http://x", "m", {}, timeout=2.0)
    assert ok is False
    assert "timed out" in detail


def test_check_model_available_false_on_network_error_never_raises():
    with patch("engine.generation.llm.httpx.get", side_effect=httpx.ConnectError("refused")):
        ok, detail = check_model_available("http://x", "m", {})
    assert ok is False
    assert "unreachable" in detail


def test_check_model_available_false_on_non_2xx():
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())
    with patch("engine.generation.llm.httpx.get", return_value=resp):
        ok, detail = check_model_available("http://x", "m", {})
    assert ok is False


def test_validate_configured_models_empty_when_both_available():
    with (
        patch("engine.generation.llm.get_llm_provider",
              return_value=OllamaProvider(base_url="http://a", model="gen-model")),
        patch("engine.generation.llm.get_enrichment_provider",
              return_value=OllamaProvider(base_url="http://a", model="enrich-model")),
        patch("engine.generation.llm.httpx.get", return_value=_models_response(["gen-model", "enrich-model"])),
    ):
        assert validate_configured_models() == []


def test_validate_configured_models_dedupes_identical_base_url_and_model():
    """Both roles pointed at the same local Ollama model — must ping it once, not twice."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _models_response([])  # never listed -> always a miss, to prove dedup by call count

    with (
        patch("engine.generation.llm.get_llm_provider",
              return_value=OllamaProvider(base_url="http://a", model="same-model")),
        patch("engine.generation.llm.get_enrichment_provider",
              return_value=OllamaProvider(base_url="http://a", model="same-model")),
        patch("engine.generation.llm.httpx.get", side_effect=fake_get),
    ):
        warnings = validate_configured_models()
    assert len(calls) == 1
    assert len(warnings) == 1


def test_validate_configured_models_local_ollama_hint_differs_from_nvidia_hint():
    with (
        patch("engine.generation.llm.get_llm_provider",
              return_value=OllamaProvider(base_url="http://localhost:11434/v1", model="m1")),
        patch("engine.generation.llm.get_enrichment_provider",
              return_value=OllamaProvider(base_url="https://integrate.api.nvidia.com/v1", model="m2")),
        patch("engine.generation.llm.httpx.get", return_value=_models_response([])),
    ):
        warnings = validate_configured_models()
    assert len(warnings) == 2
    ollama_warning = next(w for w in warnings if "m1" in w)
    nvidia_warning = next(w for w in warnings if "m2" in w)
    assert "ollama serve" in ollama_warning
    assert "NVIDIA_API_KEY" in nvidia_warning
