"""Tests for OllamaProvider's token usage capture (engine/generation/llm.py)."""
from unittest.mock import MagicMock, patch

from engine.generation.llm import OllamaProvider


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
