from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from api import models
from api.db import get_db
from api.state import CUT_TRANSITIONS, transition
from engine.generation.guide_edit import replace_beat_vo, set_beat_field
from worker.tasks.common import fail_unenqueued
from worker.tasks.publish import publish_cut
from worker.tasks.render import render_cut

router = APIRouter()
templates = Jinja2Templates(directory="ui/templates")


def active_job_for_cut(db: Session, cut: models.Cut) -> models.Job | None:
    """The in-flight render/publish Job backing a `"rendering"`/`"publishing"` cut, if
    any — lets a fresh page load (no live htmx poll chain yet) embed the same
    self-polling render_status.html/publish_status.html fragment the triggering POST
    itself returns, instead of a static "refresh to update" message. Closes the "Cut
    page does not poll while rendering" Open Issues item.

    Returns `None` for any other status, or defensively if no matching row is found —
    not expected in normal operation: `trigger_render()`/`trigger_publish()` create the
    Job in the same transaction that sets `cut.status`, and the 409 guard on
    `cut.status` ensures only one such job is ever in flight per cut at a time.
    cut_card.html falls back to the static message when this comes back `None`."""
    job_type = {"rendering": models.JobType.render, "publishing": models.JobType.publish}.get(
        cut.status.value
    )
    if job_type is None:
        return None
    return (
        db.query(models.Job)
        .filter(
            models.Job.cut_id == cut.id,
            models.Job.type == job_type,
            models.Job.status.in_([models.JobStatus.pending, models.JobStatus.running]),
        )
        .order_by(models.Job.created_at.desc())
        .first()
    )


def latest_failed_job_for_cut(db: Session, cut: models.Cut) -> models.Job | None:
    """The most recent terminal-`failed` render/publish Job for a `"failed"` cut, if
    any — lets the cut card show the ACTUAL failure reason (`job.error`) instead of
    the generic hard-coded "Failed. Retry render..." message. Closes the "Failure
    reason is not shown after a page refresh" Open Issues item: `job.error` used to be
    rendered only inside render_status.html/publish_status.html, the polling fragment
    of the tab that happened to be open when the job failed — any other view of the
    cut (a fresh page load, a different tab) showed nothing more specific.

    `CUT_TRANSITIONS["failed"]` is reachable from either a failed render OR a failed
    publish (see CLAUDE.md's Key conventions on why "failed" is deliberately
    ambiguous), so — unlike active_job_for_cut()'s status->type mapping — this does
    not filter by Job.type: it takes the most recently failed row of EITHER type,
    which is exactly the one that caused THIS cut to be sitting in "failed" right now
    (job_task's own failure path sets cut.status in the same transaction it fails the
    Job — see worker/tasks/common.py's per-job-type owner rollback).

    Returns `None` for any other status, or defensively if no matching row is found —
    not expected in normal operation, same caveat as active_job_for_cut().

    `order_by()` breaks a `created_at` tie on `Job.id` (also monotonic, assigned by the
    DB rather than this process's wall clock) — belt-and-suspenders found in review:
    unlike `active_job_for_cut()`, where the 409 status guard on `cut.status` structurally
    limits the match to at most one live row, a `"failed"` cut can accumulate several
    `failed` rows over repeated manual retries, so ordering has to be right, not just
    usually right."""
    if cut.status.value != "failed":
        return None
    return (
        db.query(models.Job)
        .filter(
            models.Job.cut_id == cut.id,
            models.Job.type.in_([models.JobType.render, models.JobType.publish]),
            models.Job.status == models.JobStatus.failed,
        )
        .order_by(models.Job.created_at.desc(), models.Job.id.desc())
        .first()
    )


def _cut_card(request: Request, cut: models.Cut, db: Session) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "fragments/cut_card.html",
        {
            "cut": cut,
            "active_job": active_job_for_cut(db, cut),
            "failed_job": latest_failed_job_for_cut(db, cut),
        },
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
        guide_changed = False
        for i in range(len(beats)):
            if form.get(f"beat_{i}_duration_s", "").strip():
                try:
                    new_duration = float(form[f"beat_{i}_duration_s"])
                except ValueError:
                    pass
                else:
                    guide_changed |= set_beat_field(beats, i, "duration_s", new_duration)
            if form.get(f"beat_{i}_visual_direction", "").strip():
                guide_changed |= set_beat_field(
                    beats, i, "visual_direction", form[f"beat_{i}_visual_direction"]
                )
            if f"beat_{i}_vo_script" in form:
                guide_changed |= set_beat_field(beats, i, "vo_script", form[f"beat_{i}_vo_script"])
            if f"beat_{i}_on_screen_text" in form:
                guide_changed |= set_beat_field(
                    beats, i, "on_screen_text", form[f"beat_{i}_on_screen_text"].splitlines()
                )
        if guide_changed:
            # Only reassign cut.guide when a beat field's NORMALIZED value actually
            # differs from what's stored — a pure "Save changes" click that touched only
            # the caption/hashtags (both separate Cut columns, not part of `guide` at
            # all) must leave `guide` byte-for-byte untouched, or it would spuriously
            # change Cut.rendered_guide_fingerprint's comparison target for a render that
            # never actually went stale. See engine/generation/guide_edit.py::set_beat_field()
            # and docs/specs/2026-09-stale-video-on-failed-rerender-system-design.md.
            guide["beats"] = beats
            cut.guide = guide

    db.commit()
    return _cut_card(request, cut, db)


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
    return _cut_card(request, cut, db)


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
    return _cut_card(request, cut, db)


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

    beats = cut.guide.get("beats", [])
    if not beats or beats[0].get("type") != "hook":
        raise HTTPException(status_code=422, detail="This cut's guide has no hook beat")
    cut.guide = replace_beat_vo(cut.guide, 0, cut.hook_variants[index])
    db.commit()
    return _cut_card(request, cut, db)
