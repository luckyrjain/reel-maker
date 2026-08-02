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
    """Context manager that writes a StageEvent row on exit (success or failure)."""
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
