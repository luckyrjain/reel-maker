"""Pre-generation cost/time estimate for the create-reel form.

Call-count and time ranges come from documented, observed pipeline behavior
(see CLAUDE.md / ui/templates/index.html's own form hint). Cost is deliberately
NOT a guessed per-token figure — it's the average of what a reel on the same
generation path has actually cost, computed from this operator's own
StageEvent history. With no history yet, we say so rather than inventing a
number.
"""
from dataclasses import dataclass

from api import models
from api.config import settings
from engine.generation.script_parser import is_structured

# (min, max) — matches ui/templates/index.html's own form hint text.
_STRUCTURED_TIME_S = (60, 120)
_STANDARD_TIME_S = (120, 300)

# Rough paid-LLM-call ranges per path (enrich/conflict/visuals/caption/judge for
# structured; up to 3 retry attempts of generate+enrich+judge for standard).
_STRUCTURED_CALLS = (4, 7)
_STANDARD_CALLS = (3, 9)


@dataclass
class GenerationEstimate:
    path: str
    calls_min: int
    calls_max: int
    time_min_s: int
    time_max_s: int
    avg_cost_usd: float | None
    sample_size: int
    pricing_configured: bool


def resolve_generation_path(context: str, generation_path: str) -> str:
    """Mirror generate_guide's own path selection (worker/tasks/generate.py)."""
    if generation_path == "structured":
        return "structured"
    if generation_path == "standard":
        return "standard"
    return "structured" if is_structured(context) else "standard"


def _historical_avg_cost(db, path: str) -> tuple[float | None, int]:
    """Average total StageEvent cost per reel among past reels that took `path`.

    Returns (avg_cost_usd, sample_size). avg_cost_usd is None when there is no
    completed history yet for this path.

    Reels where the structured path was tried first and fell through on a low
    quality score (job.meta["structured_fallback"]) are excluded from the
    "standard" bucket: their final path is "standard", but their StageEvent
    cost also includes the failed structured attempt, which would inflate the
    average for reels that go straight to standard. is_structured() means a
    future estimate for a structured-looking context is quoted from the
    "structured" bucket anyway, so this exclusion only keeps "standard" honest
    for reels that actually went straight there.
    """
    done_generate_jobs = (
        db.query(models.Job)
        .filter(
            models.Job.type == models.JobType.generate,
            models.Job.status == models.JobStatus.done,
        )
        .all()
    )
    reel_ids = {
        j.reel_id for j in done_generate_jobs
        if (j.meta or {}).get("path") == path and not (j.meta or {}).get("structured_fallback")
    }
    if not reel_ids:
        return None, 0

    events = (
        db.query(models.StageEvent)
        .filter(models.StageEvent.reel_id.in_(reel_ids))
        .all()
    )
    cost_by_reel: dict[int, float] = {}
    for e in events:
        cost_by_reel[e.reel_id] = cost_by_reel.get(e.reel_id, 0.0) + (e.cost_usd or 0.0)
    if not cost_by_reel:
        return None, 0
    costs = list(cost_by_reel.values())
    return sum(costs) / len(costs), len(costs)


def estimate_generation(db, context: str, generation_path: str) -> GenerationEstimate:
    path = resolve_generation_path(context, generation_path)
    calls_min, calls_max = _STRUCTURED_CALLS if path == "structured" else _STANDARD_CALLS
    time_min, time_max = _STRUCTURED_TIME_S if path == "structured" else _STANDARD_TIME_S
    avg_cost, sample_size = _historical_avg_cost(db, path)
    pricing_configured = bool(
        settings.nvidia_price_per_1m_input_tokens or settings.nvidia_price_per_1m_output_tokens
    )
    return GenerationEstimate(
        path=path,
        calls_min=calls_min,
        calls_max=calls_max,
        time_min_s=time_min,
        time_max_s=time_max,
        avg_cost_usd=avg_cost,
        sample_size=sample_size,
        pricing_configured=pricing_configured,
    )
