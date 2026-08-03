from typing import Protocol, runtime_checkable

import httpx

from api.config import settings


@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, messages: list[dict], json_mode: bool = True) -> str: ...


class OllamaProvider:
    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # Usage from the most recent complete() call (cleared before each call, so a
        # failed call never leaks a prior call's tokens into cost accounting).
        self.last_usage: dict = {}
        # Cumulative usage across every successful complete() call this instance has
        # made. Callers that wrap several complete() calls in one StageEvent (e.g. a
        # multi-batch enrichment pass) read this before/after and diff it, since
        # last_usage alone would only reflect the final call in the batch.
        self.total_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0}

    def complete(self, messages: list[dict], json_mode: bool = True) -> str:
        self.last_usage = {}
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers,
            timeout=360.0,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        self.last_usage = usage
        self.total_usage["prompt_tokens"] += usage.get("prompt_tokens") or 0
        self.total_usage["completion_tokens"] += usage.get("completion_tokens") or 0
        return data["choices"][0]["message"]["content"]


def get_llm_provider() -> LLMProvider:
    """Returns the main generation LLM. Routes to NVIDIA NIM when USE_NVIDIA_FOR_GENERATION=true."""
    if settings.nvidia_api_key and settings.use_nvidia_for_generation:
        return OllamaProvider(
            base_url=settings.nvidia_base_url,
            model=settings.nvidia_generation_model,
            api_key=settings.nvidia_api_key,
        )
    # When LLM_BASE_URL points at NVIDIA manually, pass the API key automatically.
    api_key = settings.nvidia_api_key if "nvidia" in settings.llm_base_url else ""
    return OllamaProvider(base_url=settings.llm_base_url, model=settings.llm_model, api_key=api_key)


def is_nvidia_generation() -> bool:
    """True when the main generation LLM is routed through NVIDIA NIM."""
    return bool(settings.nvidia_api_key and settings.use_nvidia_for_generation) or \
           "nvidia" in settings.llm_base_url


def get_enrichment_provider() -> LLMProvider:
    """
    Returns the best available provider for enrichment and judge calls.
    Prefers NVIDIA NIM (hosted, powerful) when NVIDIA_API_KEY is set;
    falls back to local Ollama otherwise.
    """
    if settings.nvidia_api_key:
        return OllamaProvider(
            base_url=settings.nvidia_base_url,
            model=settings.nvidia_enrichment_model,
            api_key=settings.nvidia_api_key,
        )
    return OllamaProvider(base_url=settings.llm_base_url, model=settings.llm_enrichment_model)
