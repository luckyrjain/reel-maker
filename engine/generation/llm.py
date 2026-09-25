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


def check_model_available(base_url: str, model: str, headers: dict, timeout: float = 5.0) -> tuple[bool, str]:
    """Best-effort — never raises. (True, "") when `model` is listed at
    `{base_url}/models` (the OpenAI-compatible models endpoint both Ollama and NVIDIA
    NIM serve); (False, reason) for a timeout, a network error, a non-2xx response, or
    a response that doesn't list `model`.

    This is exactly how the NVIDIA model-catalog-drift incident documented in
    docs/product-gap-analysis-and-roadmap-2026-08.md was diagnosed after the fact
    (`qwen/qwen3-next-80b-a3b-instruct` returning 410 Gone, confirmed against NVIDIA's
    live /v1/models list) — this function runs that same check proactively, at startup,
    instead of only after every generation job has already failed one at a time.
    """
    try:
        resp = httpx.get(f"{base_url}/models", headers=headers, timeout=timeout)
        resp.raise_for_status()
        ids = {m.get("id") for m in resp.json().get("data", [])}
    except httpx.TimeoutException:
        return False, f"{base_url}/models timed out after {timeout}s"
    except Exception as exc:
        return False, f"{base_url}/models unreachable ({exc})"
    if model in ids:
        return True, ""
    sample = ", ".join(sorted(i for i in ids if isinstance(i, str))[:5]) or "(none)"
    return False, f"{model!r} not found at {base_url}/models — currently lists: {sample}"


def validate_configured_models() -> list[str]:
    """Best-effort startup check for the main generation and enrichment/judge models —
    called from api/main.py's lifespan hook. Never raises; returns one human-readable
    warning per (base_url, model) pair that isn't available, deduped so pointing both
    roles at the same local Ollama model doesn't check it twice.

    A local Ollama model failing this check is routinely a false positive, not a real
    problem: this repo's own documented dev setup starts the API (terminal 1) before
    `ollama serve` (terminal 5) — see README's "Running the app". NVIDIA NIM failing it
    is a much stronger, less ignorable signal (a hosted endpoint being unreachable, or
    a model NVIDIA has deprecated out from under a fixed model-name config, isn't a
    "haven't started it yet" situation) — the message below is worded to reflect that
    difference rather than raising the same alarm level for both.
    """
    checked: set[tuple[str, str]] = set()
    warnings: list[str] = []
    for label, provider in (
        ("main generation (LLM_MODEL / NVIDIA_GENERATION_MODEL)", get_llm_provider()),
        ("enrichment/judge (LLM_ENRICHMENT_MODEL / NVIDIA_ENRICHMENT_MODEL)", get_enrichment_provider()),
    ):
        key = (provider.base_url, provider.model)
        if key in checked:
            continue
        checked.add(key)
        ok, detail = check_model_available(provider.base_url, provider.model, provider._headers)
        if ok:
            continue
        hint = (
            "harmless if you haven't started `ollama serve` yet"
            if "nvidia" not in provider.base_url
            else "this is a hosted endpoint — check NVIDIA_API_KEY and whether the model was renamed/deprecated"
        )
        warnings.append(f"{label}: {detail} ({hint})")
    return warnings
