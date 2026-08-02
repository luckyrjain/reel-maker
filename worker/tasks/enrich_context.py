import logging
from datetime import datetime, timezone

from api import models
from api.config import settings
from api.db import SessionLocal
from api.state import REEL_TRANSITIONS, transition
from engine.generation.context_enricher import evaluate_context, llm_enrich, ENRICH_THRESHOLD
from engine.generation.llm import get_enrichment_provider
from engine.generation.pricing import llm_cost_usd
from engine.generation.script_parser import is_structured as _is_structured_script
from engine.observability import record_stage
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat
from worker.tasks.generate import generate_guide

_log = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=0)
def enrich_context(self, job_id: int):
    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        if job is None:
            return
        if job.status.value in (models.JobStatus.done.value, models.JobStatus.running.value):
            return

        reel = db.get(models.Reel, job.reel_id)
        if reel is None:
            raise ValueError(f"Reel {job.reel_id} no longer exists")

        job.status = models.JobStatus.running
        job.started_at = datetime.now(timezone.utc)
        job.heartbeat_at = job.started_at
        job.attempts = (job.attempts or 0) + 1
        job.progress = 10
        db.commit()

        # ── Step 1: Evaluate context quality ─────────────────────────────────
        score, issues = evaluate_context(reel.context)
        job.meta = {**(job.meta or {}), "context_score": score, "context_issues": issues}
        heartbeat(db, job, 30)

        # ── Step 2: Enrich if below threshold (skip for structured scripts) ────
        is_structured = _is_structured_script(reel.context)
        if score < ENRICH_THRESHOLD and not is_structured:
            heartbeat(db, job, 40)
            llm = get_enrichment_provider()
            enrich_provider = "nvidia" if settings.nvidia_api_key else "ollama"
            with record_stage(db, reel.id, "context_enrich", provider=enrich_provider) as ev:
                enriched = llm_enrich(reel.context, reel.niche or "", llm)
                ev.detail["score_before"] = score
                ev.detail["issues"] = issues
                usage = getattr(llm, "last_usage", {})
                ev.tokens_in = usage.get("prompt_tokens")
                ev.tokens_out = usage.get("completion_tokens")
                ev.cost_usd = llm_cost_usd(enrich_provider, ev.tokens_in, ev.tokens_out)

            if enriched:
                reel.enriched_context = enriched
                job.meta = {**(job.meta or {}), "enriched": True}
                _log.info("reel_id=%s: context enriched (score was %d)", reel.id, score)
            else:
                job.meta = {**(job.meta or {}), "enriched": False, "enrich_failed": True}
                _log.warning(
                    "reel_id=%s: llm_enrich returned None (score %d), proceeding with original",
                    reel.id, score,
                )
        else:
            skipped_reason = "structured_script" if is_structured else "score_above_threshold"
            job.meta = {**(job.meta or {}), "enriched": False, "enrich_skipped": skipped_reason}
            _log.info(
                "reel_id=%s: skipping enrichment (%s, score=%d)",
                reel.id, skipped_reason, score,
            )

        heartbeat(db, job, 80)

        # ── Step 3: Transition reel + create and enqueue generate job ─────────
        transition(reel, "generating", REEL_TRANSITIONS)

        generation_path = (job.meta or {}).get("generation_path", "auto")
        generate_job = models.Job(
            type=models.JobType.generate,
            reel_id=reel.id,
            status=models.JobStatus.pending,
            progress=0,
            meta={"generation_path": generation_path, "context_score": score},
        )
        db.add(generate_job)

        job.status = models.JobStatus.done
        job.progress = 100
        job.heartbeat_at = datetime.now(timezone.utc)
        job.error = None
        db.commit()
        db.refresh(generate_job)

        generate_guide.delay(generate_job.id)

    except Exception as exc:
        db.rollback()
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
            job.error = str(exc)[:2000]
            reel = db.get(models.Reel, job.reel_id) if job.reel_id else None
            if reel and reel.status.value == "enriching":
                try:
                    transition(reel, "failed", REEL_TRANSITIONS)
                except ValueError:
                    pass
            db.commit()
        raise
    finally:
        db.close()
