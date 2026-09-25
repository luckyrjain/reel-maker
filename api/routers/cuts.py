from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from api import models
from api.config import settings
from api.db import get_db
from api.state import CUT_TRANSITIONS, transition
from engine.generation.postprocess import _derive_on_screen
from worker.tasks.common import fail_unenqueued
from worker.tasks.publish import publish_cut
from worker.tasks.render import render_cut

router = APIRouter()
templates = Jinja2Templates(directory="ui/templates")


def _cut_card(request: Request, cut: models.Cut) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "fragments/cut_card.html",
        {"cut": cut},
    )


@router.post("/cuts/{cut_id}/render", response_class=HTMLResponse)
def trigger_render(cut_id: int, request: Request, db: Session = Depends(get_db)):
    # FOR UPDATE: a double-click (or Retry render + Retry publish) serialises on the cut row, so the
    # second request sees the first one's status change instead of both passing the guards below.
    cut = db.get(models.Cut, cut_id, with_for_update=True)
    if not cut:
        raise HTTPException(status_code=404, detail="Cut not found")

    if not cut.guide:
        raise HTTPException(status_code=422, detail="No guide yet — run guide generation first")

    if cut.platform_post_id:
        # The video is already live. Re-rendering would leave the cut pointing at that post while
        # a later publish "finalizes" against it without ever uploading the new render.
        raise HTTPException(status_code=409, detail="Already posted — it cannot be re-rendered")

    current = cut.status.value
    if current == "rendering":
        raise HTTPException(status_code=409, detail="Render already in progress")
    if current == "failed":
        transition(cut, "draft", CUT_TRANSITIONS)
        current = "draft"
    if current not in ("draft", "in_review"):
        raise HTTPException(status_code=409, detail=f"Cannot render from status '{current}'")

    transition(cut, "rendering", CUT_TRANSITIONS)

    job = models.Job(
        type=models.JobType.render,
        reel_id=cut.reel_id,
        cut_id=cut_id,
        status=models.JobStatus.pending,
        progress=0,
    )
    db.add(job)
    db.flush()
    job_id = job.id
    db.commit()   # nothing may be open across .delay(): a slow broker failure would outlive an idle-in-transaction timeout

    try:
        render_cut.delay(job_id)
    except Exception as exc:
        fail_unenqueued(db, job_id, models.JobType.render.value, exc)
        raise HTTPException(status_code=503, detail="Could not queue the render — try again") from exc
    db.refresh(job)

    return templates.TemplateResponse(
        request, "fragments/render_status.html",
        {"job": job, "cut": cut},
    )


@router.get("/cuts/{cut_id}/render-status", response_class=HTMLResponse)
def render_status_fragment(
    cut_id: int, job_id: int, request: Request, db: Session = Depends(get_db)
):
    job = db.get(models.Job, job_id)
    cut = db.get(models.Cut, cut_id)
    if not job or not cut:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "fragments/render_status.html",
        {"job": job, "cut": cut},
    )


@router.patch("/cuts/{cut_id}", response_class=HTMLResponse)
async def update_cut(cut_id: int, request: Request, db: Session = Depends(get_db)):
    # Read the body BEFORE taking any lock: a slow or stalled client (flaky connection, proxy hiccup —
    # no malice needed) would otherwise hold the row lock, and a pool connection, for as long as the
    # body trickles in. That starves every other request waiting on the same lock, not just this cut's.
    form = await request.form()

    cut = db.get(models.Cut, cut_id, with_for_update=True)   # see trigger_render
    if not cut:
        raise HTTPException(status_code=404, detail="Cut not found")
    if cut.status.value != "in_review":
        raise HTTPException(status_code=409, detail="Can only edit cuts with status 'in_review'")

    if form.get("caption", "").strip():
        cut.caption = form["caption"].strip()

    if form.get("hashtags_raw", "").strip():
        cut.hashtags = [
            t.strip().lstrip("#")
            for t in form["hashtags_raw"].split(",")
            if t.strip()
        ]

    if cut.guide:
        guide = dict(cut.guide)
        beats = [dict(b) for b in guide.get("beats", [])]
        for i in range(len(beats)):
            if form.get(f"beat_{i}_duration_s", "").strip():
                try:
                    beats[i]["duration_s"] = float(form[f"beat_{i}_duration_s"])
                except ValueError:
                    pass
            if form.get(f"beat_{i}_visual_direction", "").strip():
                beats[i]["visual_direction"] = form[f"beat_{i}_visual_direction"].strip()
            if f"beat_{i}_vo_script" in form:
                new_vo = form[f"beat_{i}_vo_script"].strip()
                beats[i]["vo_script"] = new_vo
                beats[i]["on_screen_text"] = _derive_on_screen(new_vo, max_lines=5)
            if f"beat_{i}_on_screen_text" in form:
                lines = [
                    l.strip()
                    for l in form[f"beat_{i}_on_screen_text"].splitlines()
                    if l.strip()
                ]
                beats[i]["on_screen_text"] = lines[:5]
        guide["beats"] = beats
        cut.guide = guide

    db.commit()
    return _cut_card(request, cut)


@router.post("/cuts/{cut_id}/approve", response_class=HTMLResponse)
def approve_cut(cut_id: int, request: Request, db: Session = Depends(get_db)):
    cut = db.get(models.Cut, cut_id, with_for_update=True)   # see trigger_render
    if not cut:
        raise HTTPException(status_code=404, detail="Cut not found")
    if cut.status.value != "in_review":
        raise HTTPException(
            status_code=409, detail=f"Cannot approve from status '{cut.status.value}'"
        )
    transition(cut, "approved", CUT_TRANSITIONS)
    db.commit()
    return _cut_card(request, cut)


@router.post("/cuts/{cut_id}/publish", response_class=HTMLResponse)
def trigger_publish(cut_id: int, request: Request, db: Session = Depends(get_db)):
    cut = db.get(models.Cut, cut_id, with_for_update=True)   # see trigger_render
    if not cut:
        raise HTTPException(status_code=404, detail="Cut not found")

    if not cut.video_path:
        raise HTTPException(status_code=422, detail="No rendered video yet — render and approve first")

    current = cut.status.value
    if current == "publishing":
        raise HTTPException(status_code=409, detail="Publish already in progress")
    if current == "failed":
        # A publish failure (bad credentials, network error) doesn't need a
        # re-render — retry straight from "approved". A render failure is
        # retried via /render instead, which reverts to "draft" itself.
        transition(cut, "approved", CUT_TRANSITIONS)
        current = "approved"
    if current not in ("approved", "scheduled"):
        raise HTTPException(status_code=409, detail=f"Cannot publish from status '{current}'")

    transition(cut, "publishing", CUT_TRANSITIONS)

    job = models.Job(
        type=models.JobType.publish,
        reel_id=cut.reel_id,
        cut_id=cut_id,
        status=models.JobStatus.pending,
        progress=0,
    )
    db.add(job)
    db.flush()
    job_id = job.id
    db.commit()   # see trigger_render

    try:
        publish_cut.delay(job_id)
    except Exception as exc:
        fail_unenqueued(db, job_id, models.JobType.publish.value, exc)
        raise HTTPException(status_code=503, detail="Could not queue the publish — try again") from exc
    db.refresh(job)

    return templates.TemplateResponse(
        request, "fragments/publish_status.html",
        {"job": job, "cut": cut},
    )


@router.get("/cuts/{cut_id}/publish-status", response_class=HTMLResponse)
def publish_status_fragment(
    cut_id: int, job_id: int, request: Request, db: Session = Depends(get_db)
):
    job = db.get(models.Job, job_id)
    cut = db.get(models.Cut, cut_id)
    if not job or not cut:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "fragments/publish_status.html",
        {"job": job, "cut": cut},
    )


@router.get("/cuts/{cut_id}/video")
def stream_video(cut_id: int, db: Session = Depends(get_db)):
    cut = db.get(models.Cut, cut_id)
    if not cut or not cut.video_path:
        raise HTTPException(status_code=404, detail="Video not found")
    video_store = Path(settings.video_store_dir).resolve()
    resolved = Path(cut.video_path).resolve()
    if not resolved.is_relative_to(video_store):
        raise HTTPException(status_code=403, detail="Forbidden")
    return FileResponse(
        resolved,
        media_type="video/mp4",
        filename=f"reel_{cut.reel_id}_{cut.platform.value}.mp4",
    )


@router.get("/cuts/{cut_id}/subtitles")
def stream_subtitles(cut_id: int, db: Session = Depends(get_db)):
    cut = db.get(models.Cut, cut_id)
    if not cut or not cut.subtitle_path:
        raise HTTPException(status_code=404, detail="Subtitles not found")
    video_store = Path(settings.video_store_dir).resolve()
    resolved = Path(cut.subtitle_path).resolve()
    if not resolved.is_relative_to(video_store):   # see stream_video
        raise HTTPException(status_code=403, detail="Forbidden")
    return FileResponse(
        resolved,
        media_type="application/x-subrip",
        filename=f"reel_{cut.reel_id}_{cut.platform.value}.srt",
    )


@router.get("/cuts/{cut_id}/thumbnail/{index}")
def stream_thumbnail(cut_id: int, index: int, db: Session = Depends(get_db)):
    cut = db.get(models.Cut, cut_id)
    if not cut or not cut.thumbnail_candidates or not (0 <= index < len(cut.thumbnail_candidates)):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    video_store = Path(settings.video_store_dir).resolve()
    resolved = Path(cut.thumbnail_candidates[index]).resolve()
    if not resolved.is_relative_to(video_store):   # see stream_video
        raise HTTPException(status_code=403, detail="Forbidden")
    return FileResponse(resolved, media_type="image/jpeg")


@router.post("/cuts/{cut_id}/thumbnail", response_class=HTMLResponse)
async def choose_thumbnail(cut_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()   # see update_cut — read before the lock
    cut = db.get(models.Cut, cut_id, with_for_update=True)
    if not cut:
        raise HTTPException(status_code=404, detail="Cut not found")
    if cut.status.value != "in_review":
        raise HTTPException(status_code=409, detail="Can only choose a thumbnail for cuts with status 'in_review'")
    try:
        index = int(form.get("index", ""))
    except ValueError:
        raise HTTPException(status_code=422, detail="index must be an integer")
    if not cut.thumbnail_candidates or not (0 <= index < len(cut.thumbnail_candidates)):
        raise HTTPException(status_code=422, detail="No such thumbnail candidate")
    cut.thumbnail_path = cut.thumbnail_candidates[index]
    db.commit()
    return _cut_card(request, cut)


@router.post("/cuts/{cut_id}/hook-variant", response_class=HTMLResponse)
async def choose_hook_variant(cut_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()   # see update_cut — read before the lock
    cut = db.get(models.Cut, cut_id, with_for_update=True)
    if not cut:
        raise HTTPException(status_code=404, detail="Cut not found")
    if cut.status.value != "in_review":
        raise HTTPException(status_code=409, detail="Can only swap the hook for cuts with status 'in_review'")
    try:
        index = int(form.get("index", ""))
    except ValueError:
        raise HTTPException(status_code=422, detail="index must be an integer")
    if not cut.hook_variants or not (0 <= index < len(cut.hook_variants)) or not cut.guide:
        raise HTTPException(status_code=422, detail="No such hook variant")

    guide = dict(cut.guide)
    beats = [dict(b) for b in guide.get("beats", [])]
    if not beats or beats[0].get("type") != "hook":
        raise HTTPException(status_code=422, detail="This cut's guide has no hook beat")
    new_vo = cut.hook_variants[index]
    beats[0]["vo_script"] = new_vo
    beats[0]["on_screen_text"] = _derive_on_screen(new_vo, max_lines=5)
    guide["beats"] = beats
    cut.guide = guide
    db.commit()
    return _cut_card(request, cut)
