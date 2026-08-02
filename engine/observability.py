"""
Lightweight stage-event instrumentation for the generation + render pipeline.

Usage:
    from engine.observability import record_stage

    with record_stage(db, reel_id, "judge", provider="nvidia", model=MODEL) as ev:
        score, reasons = judge_guide(context, guide, llm)
        ev.score = score
        ev.detail["reasons"] = reasons
"""
import time
from contextlib import contextmanager

from api import models


@contextmanager
def record_stage(
    db,
    reel_id: int,
    stage_name: str,
    *,
    cut_id: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    attempt: int | None = None,
    **detail,
):
    """Context manager that writes a StageEvent row on exit (success or failure).

    Note: this commits `db`, so any uncommitted ORM state the caller happens to
    be holding is flushed too. Every current call site sits on a commit boundary.
    Keep it that way — do not wrap a half-applied mutation in record_stage.
    """
    t0 = time.perf_counter()
    ev = models.StageEvent(
        reel_id=reel_id,
        cut_id=cut_id,
        stage=stage_name,
        provider=provider,
        model_name=model,
        attempt=attempt,
        ok=True,
        detail=detail or {},
    )
    try:
        yield ev
    except Exception as exc:
        ev.ok = False
        ev.detail = {**ev.detail, "error": repr(exc)}
        raise
    finally:
        ev.latency_ms = int((time.perf_counter() - t0) * 1000)
        db.add(ev)
        try:
            db.commit()
        except Exception:
            db.rollback()


def paid_call_count(db, reel_id: int) -> int:
    """Count StageEvent rows for a reel that hit a metered (NVIDIA NIM) provider.

    Local Ollama calls are free and don't count — see Settings.max_paid_llm_calls_per_reel,
    which uses this to stop a stuck retry loop from running up an unbounded bill.

    This is a lifetime count for the reel, not scoped to one job or attempt — there
    is currently no "regenerate this reel's guide" flow that would create a second
    generate Job, so that's not reachable today. If one is added later, a reel that
    already hit the cap would need an explicit reset (not just raising the global
    setting) to be regenerated again.
    """
    return (
        db.query(models.StageEvent)
        .filter(models.StageEvent.reel_id == reel_id, models.StageEvent.provider == "nvidia")
        .count()
    )
