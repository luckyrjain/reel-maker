"""Raw file-byte streaming for a Cut's rendered artifacts (video / subtitles / thumbnail).

Split out of api/routers/cuts.py — closes candidate 2 of the improve-codebase-architecture
review. cuts.py owns Cut lifecycle orchestration (render/approve/publish/update, plus the
variant-choosers, which all share the _cut_card() template-rendering helper); this module
owns the genuinely different "serve a file safely from a sandboxed directory" concern — a
GET returning raw bytes via FileResponse, no template, no DB write. See
docs/specs/2026-09-cut-media-router-split-module-design.md.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from api import models
from api.config import settings
from api.db import get_db

router = APIRouter()


def _resolve_within_video_store(path_str: str) -> Path:
    """Resolve path_str and confirm it falls under VIDEO_STORE_DIR, raising 403 otherwise.
    The one implementation of the path-traversal guard shared by every file-streaming
    endpoint below — previously copy-pasted three times (see CAR candidate 1, the
    improve-codebase-architecture review)."""
    video_store = Path(settings.video_store_dir).resolve()
    resolved = Path(path_str).resolve()
    if not resolved.is_relative_to(video_store):
        raise HTTPException(status_code=403, detail="Forbidden")
    return resolved


@router.get("/cuts/{cut_id}/video")
def stream_video(cut_id: int, db: Session = Depends(get_db)):
    cut = db.get(models.Cut, cut_id)
    if not cut or not cut.video_path:
        raise HTTPException(status_code=404, detail="Video not found")
    resolved = _resolve_within_video_store(cut.video_path)
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
    resolved = _resolve_within_video_store(cut.subtitle_path)
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
    resolved = _resolve_within_video_store(cut.thumbnail_candidates[index])
    return FileResponse(resolved, media_type="image/jpeg")
