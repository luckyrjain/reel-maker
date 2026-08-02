import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from api import models
from api.config import settings
from api.db import SessionLocal
from api.state import CUT_TRANSITIONS, transition
from engine.generation.guide_schema import PlatformGuide
from engine.observability import record_stage
from engine.render.asset_sourcer import get_asset_sourcer, get_hf_sourcer, get_hf_video_sourcer, get_wiki_sourcer, resolve_or_reuse
from engine.render.compositor import composite_cut
from engine.render.tts import get_tts_provider
from worker.celery_app import celery_app


def _tts_duration(vo_path: Path) -> float | None:
    """Return actual audio duration via ffprobe (fast, no decoding required)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(vo_path)],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio":
                return float(stream["duration"])
        return None
    except Exception:
        return None


def _heartbeat(db, job, progress: int) -> None:
    job.progress = progress
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()


@celery_app.task(bind=True, max_retries=2)
def render_cut(self, job_id: int):
    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        if job is None:
            return
        # Idempotency guard: done = redelivery no-op; running = live sibling
        if job.status in (models.JobStatus.done, models.JobStatus.running):
            return

        cut = db.get(models.Cut, job.cut_id)
        reel = db.get(models.Reel, cut.reel_id)

        job.status = models.JobStatus.running
        job.started_at = datetime.now(timezone.utc)
        job.heartbeat_at = job.started_at
        job.attempts = (job.attempts or 0) + 1
        job.progress = 5
        db.commit()

        guide = PlatformGuide(**cut.guide)
        beats = guide.beats
        n = len(beats)

        asset_store = Path(settings.asset_store_dir)
        video_store = Path(settings.video_store_dir)

        sourcer = get_asset_sourcer(asset_store)
        wiki = get_wiki_sourcer(asset_store)
        hf_video = get_hf_video_sourcer(asset_store)
        hf = get_hf_sourcer(asset_store)
        tts = get_tts_provider(asset_store / "tts")

        beat_asset_pairs: list[list] = []
        beat_video_paths: list[list[Path | None]] = []
        beat_vo_paths: list[Path | None] = []

        for i, beat in enumerate(beats):
            # resolve_or_reuse: reuses pinned assets when visual_direction hasn't
            # changed; re-resolves (and re-pins) only when the direction changed.
            pairs = resolve_or_reuse(
                db,
                cut=cut,
                beat_index=beat.index,
                visual_direction=beat.visual_direction,
                min_duration_s=beat.duration_s,
                sourcer=sourcer,
                wiki=wiki,
                hf_video=hf_video,
                hf=hf,
            )
            beat_asset_pairs.append(pairs)
            beat_video_paths.append([p for _, p in pairs])

            # Synthesize VO and nudge speaking rate toward beat target duration
            if reel.voiceover_mode == "voiceover" and beat.vo_script.strip():
                if hasattr(tts, "synth_to_budget"):
                    vo_path = tts.synth_to_budget(beat.vo_script, target_s=beat.duration_s)
                else:
                    vo_path = tts.synthesize(beat.vo_script)
            else:
                vo_path = None
            beat_vo_paths.append(vo_path)

            _heartbeat(db, job, 10 + int(55 * (i + 1) / n))

        _heartbeat(db, job, 70)

        # Override duration_s with actual TTS audio length so beat clips match speech.
        beat_dicts = []
        for beat, vo_path in zip(beats, beat_vo_paths):
            d = beat.model_dump()
            if vo_path and vo_path.exists():
                actual = _tts_duration(vo_path)
                if actual is not None:
                    d["duration_s"] = round(actual + 0.1, 2)
            beat_dicts.append(d)

        # Update CutAsset timecodes with TTS-accurate start/end values
        beat_start = 0.0
        for bd in beat_dicts:
            duration = float(bd["duration_s"])
            db.query(models.CutAsset).filter(
                models.CutAsset.cut_id == cut.id,
                models.CutAsset.beat_index == bd["index"],
            ).update({"start_s": beat_start, "end_s": beat_start + duration})
            beat_start += duration
        db.commit()

        out_path = video_store / str(reel.id) / f"{cut.platform.value}.mp4"
        thumb_path = video_store / str(reel.id) / f"{cut.platform.value}_thumb.jpg"

        with record_stage(db, reel.id, "composite", cut_id=cut.id) as ev:
            duration = composite_cut(
                beats=beat_dicts,
                beat_video_paths=beat_video_paths,
                beat_vo_paths=beat_vo_paths,
                output_path=out_path,
                thumbnail_path=thumb_path,
            )
            ev.detail["duration_s"] = duration

        cut.video_path = str(out_path)
        cut.thumbnail_path = str(thumb_path)
        cut.duration_s = duration
        transition(cut, "in_review", CUT_TRANSITIONS)

        job.progress = 100
        job.heartbeat_at = datetime.now(timezone.utc)
        job.status = models.JobStatus.done
        db.commit()

    except Exception as exc:
        db.rollback()
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
            job.error = str(exc)[:2000]
            if job.cut_id:
                cut = db.get(models.Cut, job.cut_id)
                if cut and cut.status.value == "rendering":
                    try:
                        transition(cut, "failed", CUT_TRANSITIONS)
                    except ValueError:
                        pass
            db.commit()
        raise
    finally:
        db.close()
