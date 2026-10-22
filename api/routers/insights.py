from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Annotated

from api.db import get_db
from api import models
from engine.hidden_text import strip_tag_chars
from engine.analytics.correlation import (
    MIN_SAMPLE,
    quality_engagement_correlation,
    top_bottom_performers,
)

router = APIRouter()
templates = Jinja2Templates(directory="ui/templates")


def _notes(db: Session) -> list[models.PerformanceNote]:
    return (
        db.query(models.PerformanceNote)
        .order_by(models.PerformanceNote.created_at.desc())
        .all()
    )


@router.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, db: Session = Depends(get_db)):
    correlation = quality_engagement_correlation(db)
    performers = top_bottom_performers(db)
    if isinstance(performers, tuple):
        top_performers, bottom_performers = performers
        combined_performers = None
    else:
        top_performers = bottom_performers = None
        combined_performers = performers

    return templates.TemplateResponse(
        request, "insights.html",
        {
            "correlation": correlation,
            "top_performers": top_performers,
            "bottom_performers": bottom_performers,
            "combined_performers": combined_performers,
            "notes": _notes(db),
            "min_sample": MIN_SAMPLE,
        },
    )


@router.post("/insights/notes", response_class=HTMLResponse)
def create_note(
    request: Request,
    text: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    # Tag characters are invisible to the operator but readable by the LLM that receives every active
    # note (ASCII smuggling), so they never reach the table. Cleaned BEFORE the empty check: a note
    # made only of them is not a note.
    text = strip_tag_chars(text).strip()
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
