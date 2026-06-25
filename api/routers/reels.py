from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Annotated, Optional

from api.db import get_db
from api import models
from api.state import transition, REEL_TRANSITIONS
from worker.tasks.enrich_context import enrich_context

router = APIRouter()
templates = Jinja2Templates(directory="ui/templates")


@router.post("/reels", response_class=HTMLResponse)
def create_reel(
    request: Request,
    context: Annotated[str, Form()],
    niche: Annotated[Optional[str], Form()] = None,
    voiceover_mode: Annotated[str, Form()] = "voiceover",
    target_length_s: Annotated[float, Form()] = 45.0,
    generation_path: Annotated[str, Form()] = "auto",
    db: Session = Depends(get_db),
):
    reel = models.Reel(
        context=context,
        niche=niche,
        voiceover_mode=voiceover_mode,
        status=models.ReelStatus.draft,
    )
    db.add(reel)
    db.flush()

    transition(reel, "enriching", REEL_TRANSITIONS)

    for platform in [models.CutPlatform.youtube_shorts, models.CutPlatform.instagram_reels]:
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
    db.commit()
    db.refresh(job)

    enrich_context.delay(job.id)

    return templates.TemplateResponse(
        request, "fragments/pipeline_status.html",
        {"job": job, "reel": reel,
         "poll_url": f"/api/reels/{reel.id}/active-job-fragment"},
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


@router.get("/reels/{reel_id}", response_class=HTMLResponse)
def reel_detail(reel_id: int, request: Request, db: Session = Depends(get_db)):
    reel = db.get(models.Reel, reel_id)
    if not reel:
        raise HTTPException(status_code=404, detail="Reel not found")
    cuts = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).all()
    return templates.TemplateResponse(
        request, "reel.html",
        {"reel": reel, "cuts": cuts},
    )
