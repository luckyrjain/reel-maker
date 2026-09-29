from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from typing import Annotated, Optional

from api.db import get_db
from api import models
from api.routers.cuts import active_job_for_cut, latest_failed_job_for_cut
from api.state import transition, REEL_TRANSITIONS
from engine.generation.estimate import estimate_generation
from engine.observability import latest_quality_scores
from engine.render.compositor import CURATED_TEXT_COLORS
from engine.render.tts import CURATED_EDGE_VOICES
from worker.tasks.common import fail_unenqueued
from worker.tasks.enrich_context import enrich_context

router = APIRouter()
templates = Jinja2Templates(directory="ui/templates")

REEL_LIST_PAGE_SIZE = 50
_DEFAULT_PLATFORMS = [models.CutPlatform.youtube_shorts, models.CutPlatform.instagram_reels]
_CURATED_TTS_VOICE_NAMES = {v for v, _ in CURATED_EDGE_VOICES}
_CURATED_TEXT_COLOR_NAMES = {c for c, _ in CURATED_TEXT_COLORS}

# Stage names excluded from the two headline pipeline-panel sums (total_cost,
# total_latency_ms) in _pipeline_summary() below — NOT from stage_summary,
# which stays unfiltered so every stage still gets its own per-stage row.
# "instagram_metrics" fires every 6h for as long as a cut stays published
# (months/years), unlike every generation/render stage which fires a bounded
# number of times per reel; folding it into "total LLM time" (the template's
# label for total_latency_ms) would silently inflate that headline stat
# forever and would be factually wrong regardless of volume (an HTTP
# metrics-pull round-trip is not LLM time). Named as a set (not inlined) so a
# future post-publish stage is an obvious one-line addition here, not a
# second silent gap. See docs/specs/2026-09-instagram-metrics-drift-system-design.md §8.
_EXCLUDED_FROM_TOTALS = {"instagram_metrics"}


@router.post("/reels", response_class=HTMLResponse)
def create_reel(
    request: Request,
    context: Annotated[str, Form()],
    niche: Annotated[Optional[str], Form()] = None,
    voiceover_mode: Annotated[str, Form()] = "voiceover",
    target_length_s: Annotated[float, Form()] = 45.0,
    generation_path: Annotated[str, Form()] = "auto",
    platforms: Annotated[list[str] | None, Form()] = None,
    tts_voice: Annotated[Optional[str], Form()] = None,
    text_color: Annotated[Optional[str], Form()] = None,
    db: Session = Depends(get_db),
):
    # Same "drop, don't 422" policy as the platforms checkboxes below — a stray or
    # renamed <option> value shouldn't fail the whole submission, it should just fall
    # back to the provider default (None). get_tts_provider() re-checks this anyway.
    if tts_voice not in _CURATED_TTS_VOICE_NAMES:
        tts_voice = None
    # Same policy, and the same defense-in-depth re-check happens inside
    # _build_text_filter() — an unvalidated color is a filter-graph injection point,
    # not just a cosmetic one, so this isn't purely a UX nicety.
    if text_color not in _CURATED_TEXT_COLOR_NAMES:
        text_color = None

    reel = models.Reel(
        context=context,
        niche=niche,
        voiceover_mode=voiceover_mode,
        tts_voice=tts_voice,
        text_color=text_color,
        status=models.ReelStatus.draft,
    )
    db.add(reel)
    db.flush()

    transition(reel, "enriching", REEL_TRANSITIONS)

    # Unrecognized values are dropped rather than raising 422 — a stray/renamed
    # checkbox value shouldn't fail the whole submission. Falls back to the
    # original two-platform default if nothing valid was submitted.
    selected = [p for p in (platforms or []) if p in models.CutPlatform.__members__]
    cut_platforms = [models.CutPlatform[p] for p in selected] or _DEFAULT_PLATFORMS

    for platform in cut_platforms:
        cut = models.Cut(
            reel_id=reel.id,
            platform=platform,
            target_length_s=target_length_s,
            status=models.CutStatus.draft,
        )
        db.add(cut)

    job = models.Job(
        type=models.JobType.enrich,
        reel_id=reel.id,
        status=models.JobStatus.pending,
        progress=0,
        meta={"generation_path": generation_path},
    )
    db.add(job)
    db.flush()
    job_id = job.id
    db.commit()   # nothing may be open across .delay() (see api/routers/cuts.py::trigger_render)

    try:
        enrich_context.delay(job_id)
    except Exception as exc:
        fail_unenqueued(db, job_id, models.JobType.enrich.value, exc)
        raise HTTPException(status_code=503, detail="Could not queue the job — try again") from exc
    db.refresh(job)

    return templates.TemplateResponse(
        request, "fragments/pipeline_status.html",
        {"job": job, "reel": reel,
         "poll_url": f"/api/reels/{reel.id}/active-job-fragment"},
    )


@router.post("/reels/estimate", response_class=HTMLResponse)
def estimate_reel(
    request: Request,
    context: Annotated[str, Form()] = "",
    generation_path: Annotated[str, Form()] = "auto",
    db: Session = Depends(get_db),
):
    estimate = estimate_generation(db, context, generation_path)
    return templates.TemplateResponse(
        request, "fragments/cost_estimate.html", {"estimate": estimate},
    )


def _reel_list_metrics(db: Session, reels: list[models.Reel]) -> dict[int, dict]:
    """Per-reel 'Quality' (latest job's quality_score) and 'Views' (max across
    the reel's cuts) for the reel-list table — quality-vs-engagement at a glance,
    no separate dashboard needed."""
    reel_ids = [r.id for r in reels]
    if not reel_ids:
        return {}

    jobs = (
        db.query(models.Job)
        .filter(models.Job.reel_id.in_(reel_ids))
        .order_by(models.Job.created_at)
        .all()
    )
    quality_by_reel = latest_quality_scores(jobs)

    metrics = {}
    for r in reels:
        views = [c.views for c in r.cuts if c.views is not None]
        metrics[r.id] = {
            "quality_score": quality_by_reel.get(r.id),
            "views": max(views) if views else None,
        }
    return metrics


@router.get("/reels", response_class=HTMLResponse)
def list_reels(
    request: Request,
    page: int = 1,
    db: Session = Depends(get_db),
):
    page = max(page, 1)
    offset = (page - 1) * REEL_LIST_PAGE_SIZE

    total = db.query(models.Reel).count()
    reels = (
        db.query(models.Reel)
        .options(joinedload(models.Reel.cuts))
        .order_by(models.Reel.created_at.desc())
        .offset(offset)
        .limit(REEL_LIST_PAGE_SIZE)
        .all()
    )
    metrics = _reel_list_metrics(db, reels)

    return templates.TemplateResponse(
        request, "reels_list.html",
        {
            "reels": reels,
            "metrics": metrics,
            "page": page,
            "has_next": offset + REEL_LIST_PAGE_SIZE < total,
            "has_prev": page > 1,
            "total": total,
        },
    )


@router.get("/reels/{reel_id}/active-job-fragment", response_class=HTMLResponse)
def active_job_fragment(reel_id: int, request: Request, db: Session = Depends(get_db)):
    reel = db.get(models.Reel, reel_id)
    if not reel:
        raise HTTPException(status_code=404, detail="Reel not found")

    # Return the currently active job (pending/running), falling back to latest
    active_job = (
        db.query(models.Job)
        .filter(
            models.Job.reel_id == reel_id,
            models.Job.status.in_([models.JobStatus.pending, models.JobStatus.running]),
        )
        .order_by(models.Job.created_at.desc())
        .first()
    )
    job = active_job or (
        db.query(models.Job)
        .filter(models.Job.reel_id == reel_id)
        .order_by(models.Job.created_at.desc())
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="No jobs for reel")

    poll_url = f"/api/reels/{reel_id}/active-job-fragment"
    return templates.TemplateResponse(
        request, "fragments/pipeline_status.html",
        {"job": job, "reel": reel, "poll_url": poll_url},
    )


def _pipeline_summary(db: Session, reel_id: int) -> dict:
    """Aggregate StageEvent + Job rows for a reel into cost/latency/quality figures.

    Kept as a plain function (not baked into the route) so it can be unit tested
    without spinning up the HTTP layer.
    """
    jobs = (
        db.query(models.Job)
        .filter(models.Job.reel_id == reel_id)
        .order_by(models.Job.created_at)
        .all()
    )
    stage_events = (
        db.query(models.StageEvent)
        .filter(models.StageEvent.reel_id == reel_id)
        .order_by(models.StageEvent.created_at)
        .all()
    )

    total_cost = sum(
        e.cost_usd or 0.0 for e in stage_events if e.stage not in _EXCLUDED_FROM_TOTALS
    )
    total_latency_ms = sum(
        e.latency_ms or 0 for e in stage_events if e.stage not in _EXCLUDED_FROM_TOTALS
    )
    quality_score = next(
        (
            j.meta.get("quality_score")
            for j in reversed(jobs)
            if j.meta and j.meta.get("quality_score") is not None
        ),
        None,
    )

    stage_summary: dict[str, dict] = {}
    for e in stage_events:
        s = stage_summary.setdefault(
            e.stage, {"count": 0, "latency_ms": 0, "cost_usd": 0.0, "failures": 0}
        )
        s["count"] += 1
        s["latency_ms"] += e.latency_ms or 0
        s["cost_usd"] += e.cost_usd or 0.0
        if not e.ok:
            s["failures"] += 1

    return {
        "jobs": jobs,
        "stage_events": stage_events,
        "stage_summary": stage_summary,
        "total_cost": total_cost,
        "total_latency_ms": total_latency_ms,
        "quality_score": quality_score,
    }


def latest_failed_job_for_reel(db: Session, reel: models.Reel) -> models.Job | None:
    """`latest_failed_job_for_cut()`'s reel-level sibling (api/routers/cuts.py) — a
    `"failed"` reel (an enrich/generate job failure rolls the reel back via
    `api/state.py::JOB_IN_FLIGHT`, the same `rollback_owner()` mechanism that rolls a
    cut back on a render/publish failure) had the identical "only in
    pipeline_status.html, the polling fragment of the tab that started it" gap:
    `reel.html`'s own status badge shows a bare `"failed"` with no reason, and
    `reel.html` never includes `pipeline_status.html` — that fragment is only ever
    returned by `POST /api/reels` and `GET /.../active-job-fragment` directly. Found
    during review of the cut-level fix as a real, symmetric gap the roadmap's original
    "cut card and reel page" wording already implied but the first version of this fix
    missed.

    Not type-filtered, for the same reason `latest_failed_job_for_cut()` isn't: a reel
    can fail either enrichment or generation, and the most-recently-failed row of
    either type is the one that caused the CURRENT `"failed"` state. Returns `None`
    for any other status, or defensively if no matching row is found."""
    if reel.status.value != "failed":
        return None
    return (
        db.query(models.Job)
        .filter(
            models.Job.reel_id == reel.id,
            models.Job.type.in_([models.JobType.enrich, models.JobType.generate]),
            models.Job.status == models.JobStatus.failed,
        )
        .order_by(models.Job.created_at.desc(), models.Job.id.desc())
        .first()
    )


@router.get("/reels/{reel_id}", response_class=HTMLResponse)
def reel_detail(reel_id: int, request: Request, db: Session = Depends(get_db)):
    reel = db.get(models.Reel, reel_id)
    if not reel:
        raise HTTPException(status_code=404, detail="Reel not found")
    cuts = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).all()
    pipeline = _pipeline_summary(db, reel_id)
    # See active_job_for_cut()'s docstring (api/routers/cuts.py) — lets a fresh page
    # load embed the self-polling render/publish status fragment instead of a static
    # "refresh to update" message for a cut mid-render/publish.
    active_jobs = {cut.id: active_job_for_cut(db, cut) for cut in cuts}
    # See latest_failed_job_for_cut()'s docstring — surfaces the actual job.error on
    # a fresh page load for a "failed" cut, instead of only the generic message.
    failed_jobs = {cut.id: latest_failed_job_for_cut(db, cut) for cut in cuts}
    # See latest_failed_job_for_reel()'s docstring — same fix, reel level.
    failed_reel_job = latest_failed_job_for_reel(db, reel)
    return templates.TemplateResponse(
        request, "reel.html",
        {
            "reel": reel, "cuts": cuts, "active_jobs": active_jobs,
            "failed_jobs": failed_jobs, "failed_reel_job": failed_reel_job, **pipeline,
        },
    )
