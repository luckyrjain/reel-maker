from pathlib import Path

from api import models
from api.config import settings
from api.state import CUT_TRANSITIONS, transition
from engine.generation.guide_schema import PlatformGuide
from engine.observability import record_stage
from engine.render.asset_sourcer import get_asset_sourcer, get_hf_sourcer, get_hf_video_sourcer, get_music_sourcer, get_wiki_sourcer, resolve_or_reuse
from engine.render.compositor import composite_cut
from engine.render.tts import SilentProvider, _audio_duration, get_tts_provider
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat, job_task


def _load_cut_and_reel(db, job):
    cut = db.get(models.Cut, job.cut_id)
    if cut is None:
        raise ValueError(f"Cut {job.cut_id} no longer exists")
    reel = db.get(models.Reel, cut.reel_id)
    if reel is None:
        raise ValueError(f"Reel {cut.reel_id} no longer exists")
    return cut, reel


@celery_app.task(bind=True, max_retries=2)
@job_task("render", prepare=_load_cut_and_reel)
def render_cut(self, db, job, ctx):
    cut, reel = ctx
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

        heartbeat(db, job, 10 + int(55 * (i + 1) / n))

    heartbeat(db, job, 70)

    # Override duration_s with actual TTS audio length so beat clips match speech.
    # SilentProvider returns the same 1 s placeholder for every beat, so measuring
    # it would collapse the whole reel to ~1 s per beat — keep the planned durations.
    has_speech = not isinstance(tts, SilentProvider)
    beat_dicts = []
    for beat, vo_path in zip(beats, beat_vo_paths):
        d = beat.model_dump()
        if has_speech and vo_path and vo_path.exists():
            actual = _audio_duration(vo_path)
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

    # One music track for the whole cut, matched against whichever beat's
    # music_cue is set first (typically the hook) — track-switching mid-cut
    # would be jarring for a <90s video. None if no local library track matches.
    music_cue = next((bd.get("music_cue") for bd in beat_dicts if bd.get("music_cue")), None)
    music_path = get_music_sourcer().find(music_cue) if music_cue else None

    with record_stage(db, reel.id, "composite", cut_id=cut.id) as ev:
        duration = composite_cut(
            beats=beat_dicts,
            beat_video_paths=beat_video_paths,
            beat_vo_paths=beat_vo_paths,
            output_path=out_path,
            thumbnail_path=thumb_path,
            music_path=music_path,
        )
        ev.detail["duration_s"] = duration
        ev.detail["music_cue"] = music_cue
        ev.detail["music_track"] = music_path.name if music_path else None

    cut.video_path = str(out_path)
    cut.thumbnail_path = str(thumb_path)
    cut.duration_s = duration
    transition(cut, "in_review", CUT_TRANSITIONS)
