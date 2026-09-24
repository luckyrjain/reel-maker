"""Alternate hook-line generation.

One cheap post-acceptance LLM call so the operator can swap the opening line
without paying for a full guide regeneration. Pure content generation — no
Celery, no db, no Job — mirrors engine/generation/beat_enrichment.py's shape.
"""
import json

N_VARIANTS = 3


def build_hook_variant_messages(hook_vo: str, context: str, niche: str) -> list[dict]:
    example = json.dumps([f"hook line {i + 1}" for i in range(N_VARIANTS)])
    return [
        {"role": "system", "content": (
            "Return ONLY valid JSON — a list of strings, no other keys, no markdown."
        )},
        {"role": "user", "content": (
            f"Niche: {niche or 'general'}\n"
            f"Context:\n{context[:800]}\n\n"
            f"Current hook line: \"{hook_vo}\"\n\n"
            f"Write {N_VARIANTS} alternate hook lines for the same short-form video — "
            "similar length and tone to the current one, different angle or phrasing. "
            "Do not introduce facts, names, or events not already present in the context.\n\n"
            f"Return: {example}"
        )},
    ]


def generate_hook_variants(hook_vo: str, context: str, niche: str, llm) -> list[str]:
    """Best-effort — [] on any failure. This is a quality add-on generated after the
    guide already cleared the quality gate, so it must never fail the generate job."""
    hook_vo = hook_vo.strip()
    if not hook_vo:
        return []
    messages = build_hook_variant_messages(hook_vo, context, niche)
    try:
        raw = llm.complete(messages, json_mode=True)
        data = json.loads(raw)
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        variants = [str(v).strip() for v in data if isinstance(v, str) and str(v).strip()]
        return [v for v in variants if v != hook_vo][:N_VARIANTS]
    except Exception:
        return []
