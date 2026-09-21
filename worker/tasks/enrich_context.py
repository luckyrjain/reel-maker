import logging

from api import models
from api.config import settings
from api.state import REEL_TRANSITIONS, transition
from engine.generation.context_enricher import evaluate_context, llm_enrich, ENRICH_THRESHOLD
from engine.generation.llm import get_enrichment_provider
from engine.generation.pricing import llm_cost_usd
from engine.generation.script_parser import is_structured as _is_structured_script
from engine.observability import record_stage
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat, job_task, rollback_owner
from worker.tasks.generate import generate_guide

_log = logging.getLogger(__name__)


def _load_reel(db, job):
    reel = db.get(models.Reel, job.reel_id)
    if reel is None:
        raise ValueError(f"Reel {job.reel_id} no longer exists")
    return reel


def _enqueue_generate(generate_job_id):
    generate_guide.delay(generate_job_id)


def _abandon_generate(db, job, generate_job_id):
    """The follow-up generate job could not be enqueued.

    Fail the pending row that was committed with the done-stamp (otherwise the UI shows a
    forever-pending job for a failed reel, and the reaper later rolls back whatever the
    reel is doing by then), and roll the reel back from "generating" so the operator can retry.
    """
    claimed = db.query(models.Job).filter(
        models.Job.id == generate_job_id, models.Job.status == models.JobStatus.pending,
    ).update(
        {"status": models.JobStatus.failed, "error": "generate_guide could not be enqueued"},
        synchronize_session=False,
    )
    if claimed == 0:
        # The message did reach a worker (e.g. the publish timed out after delivery) and the
        # generate job is already running: it owns the reel now, so leave the reel alone.
        return
    rollback_owner(db, job, "reel", {"generating"})


# max_retries=0 on purpose: enrichment falls back to the raw context, and a retry would
# re-run a paid LLM call for no gain. If the follow-up enqueue fails after the job is done,
# _abandon_generate cleans up (the reaper would otherwise only notice after 30 minutes).
@celery_app.task(bind=True, max_retries=0)
@job_task(
    "enrich",
    prepare=_load_reel,
    after_commit=_enqueue_generate,
    after_commit_failed=_abandon_generate,
    start_progress=10,
    max_runtime_s=30 * 60,
)
def enrich_context(self, db, job, reel):
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
    db.flush()  # autoflush is off; the id is needed by after_commit
    return generate_job.id
