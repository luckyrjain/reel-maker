from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Annotated

from api.db import get_db
from api import models
from api.display import safe_text
from engine.analytics.correlation import (
    MIN_SAMPLE,
    load_reel_stats,
    quality_engagement_correlation,
    top_bottom_performers,
)

router = APIRouter()
templates = Jinja2Templates(directory="ui/templates")
templates.env.filters["safe_text"] = safe_text   # for free text read from the DB (niche, hooks, notes)


def _notes(db: Session) -> list[models.PerformanceNote]:
    return (
        db.query(models.PerformanceNote)
        .order_by(models.PerformanceNote.created_at.desc())
        .all()
    )


def _insights_context(db: Session) -> dict:
    """Everything the page shows. A separate function so the loaded reels/cuts (`stats`, which
    hold every cut's guide JSON) are released when it returns -- before the template renders --
    instead of living until the response is built."""
    stats = load_reel_stats(db)   # one load shared by both analyses
    correlation = quality_engagement_correlation(db, stats)
    performers = top_bottom_performers(db, stats=stats)
    if isinstance(performers, tuple):
        top_performers, bottom_performers = performers
        combined_performers = None
    else:
        top_performers = bottom_performers = None
        combined_performers = performers
    return {
        "correlation": correlation,
        "top_performers": top_performers,
        "bottom_performers": bottom_performers,
        "combined_performers": combined_performers,
        "notes": _notes(db),
        "min_sample": MIN_SAMPLE,
    }


@router.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "insights.html", _insights_context(db))


@router.post("/insights/notes", response_class=HTMLResponse)
def create_note(
    request: Request,
    text: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    text = text.strip()
    if text:
        note = models.PerformanceNote(text=text, active=True)
        db.add(note)
        db.commit()
    return templates.TemplateResponse(
        request, "fragments/performance_notes.html", {"notes": _notes(db)},
    )


@router.post("/insights/notes/{note_id}/toggle", response_class=HTMLResponse)
def toggle_note(note_id: int, request: Request, db: Session = Depends(get_db)):
    note = db.get(models.PerformanceNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Performance note not found")
    note.active = not note.active
    db.commit()
    return templates.TemplateResponse(
        request, "fragments/performance_notes.html", {"notes": _notes(db)},
    )


@router.delete("/insights/notes/{note_id}", response_class=HTMLResponse)
def delete_note(note_id: int, request: Request, db: Session = Depends(get_db)):
    note = db.get(models.PerformanceNote, note_id)
    if note is not None:
        db.delete(note)
        db.commit()
    return templates.TemplateResponse(
        request, "fragments/performance_notes.html", {"notes": _notes(db)},
    )
