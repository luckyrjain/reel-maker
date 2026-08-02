import json
import logging
from datetime import datetime, timezone

from pydantic import ValidationError

from api import models
from api.config import settings
from api.db import SessionLocal
from api.state import REEL_TRANSITIONS, transition
from engine.generation.beat_enrichment import (
    _enrich_with_insight,
    _has_conflict_beat,
    _is_shallow_beat,
    _make_conflict_stub,
)
from engine.generation.evaluator import score_guide
from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.llm import get_llm_provider, get_enrichment_provider, is_nvidia_generation
from engine.generation.llm_judge import judge_guide
from engine.generation.postprocess import clean_guide, _derive_on_screen as _postprocess_derive_on_screen
from engine.generation.prompt import build_messages, build_visuals_messages
from engine.generation.script_parser import BeatStub
from engine.generation import script_parser
from engine.generation.visual_fallback import (
    _fallback_visual,
    _first_person,
    _is_degenerate_visual,
)
from engine.observability import record_stage
from worker.celery_app import celery_app
from worker.tasks.common import should_retry

_log = logging.getLogger(__name__)

QUALITY_THRESHOLD = 80
QUALITY_THRESHOLD_LOCAL = 65  # lower bar for local Ollama models
_JUDGE_RULE_MIN = 55



def _combined_score(
    rule: int,
    rule_issues: list[str],
    guide: MasterGuide,
    context: str,
    db=None,
    reel_id: int | None = None,
    attempt: int | None = None,
) -> tuple[int, list[str]]:
    """Return (combined_score, all_issues). Calls LLM judge when rule score is strong enough."""
    if rule < _JUDGE_RULE_MIN:
        return rule, rule_issues
    enrichment_llm = get_enrichment_provider()
    judge_provider = "nvidia" if settings.nvidia_api_key else "ollama"
    if db and reel_id:
        with record_stage(db, reel_id, "judge",
                          provider=judge_provider,
                          attempt=attempt) as ev:
            llm_score, llm_issues = judge_guide(context, guide, enrichment_llm)
            ev.score = llm_score
            ev.detail["reasons"] = llm_issues
    else:
        llm_score, llm_issues = judge_guide(context, guide, enrichment_llm)
    combined = int(rule * 0.4 + llm_score * 0.6)
    return combined, rule_issues + llm_issues




def _stubs_to_platform_guide(
    stubs: list[BeatStub],
    visuals: dict[int, str],
    platform: str,
    target_length_s: float,
    caption: str,
    hashtags: list[str],
) -> PlatformGuide:
    beats = []
    for s in stubs:
        vd = visuals.get(s.index, "")
        if _is_degenerate_visual(vd):
            vd = _fallback_visual(s)
        # If this beat has a known player, ensure their name leads the visual direction.
        # The evaluator and asset sourcer both rely on finding the name in visual_direction.
        if s.player and vd:
            name_parts = s.player.lower().split()
            if not any(p in vd.lower() for p in name_parts):
                words = (s.player + " " + vd).split()
                vd = " ".join(words[:12])
        beats.append(Beat(
            index=s.index,
            type=s.beat_type,
            duration_s=s.duration_s,
            visual_direction=vd,
            on_screen_text=s.on_screen_text,
            vo_script=s.vo_script,
        ))
    return PlatformGuide(
        platform=platform,
        target_length_s=target_length_s,
        beats=beats,
        caption=caption,
        hashtags=hashtags,
    )


def _generate_caption_hashtags(
    stubs: list[BeatStub], niche: str, llm
) -> tuple[str, list[str]]:
    """Ask the LLM to write a caption and hashtags from the actual VO content."""
    combined_vo = " ".join(s.vo_script for s in stubs if s.vo_script)[:600]
    messages = [
        {"role": "system", "content":
            "You write social media copy for sports short-form videos. "
            "Return ONLY valid JSON — no markdown, no commentary."},
        {"role": "user", "content": (
            f"Given this voiceover script excerpt, write:\n"
            f"1. A caption: 1-2 punchy sentences, no hashtags, max 150 chars, hooks the viewer\n"
            f"2. Exactly 15 hashtags: no # prefix, mix of 5 broad / 5 niche / 5 trending\n\n"
            f'Return ONLY JSON: {{"caption": "...", "hashtags": ["tag1", ...]}}\n\n'
            f"SCRIPT:\n{combined_vo}\n"
            f"NICHE: {niche}"
        )},
    ]
    try:
        raw = llm.complete(messages, json_mode=True)
        data = json.loads(raw)
        caption = data.get("caption", "").strip()
        hashtags = data.get("hashtags", [])
        if caption and isinstance(hashtags, list) and len(hashtags) >= 5:
            return caption, [str(h).lstrip("#") for h in hashtags[:15]]
    except Exception:
        pass
    return "", []


def _generate_from_structured_script(
    reel, cuts, llm, db, target_lengths: dict, stubs: list[BeatStub], context: str
) -> MasterGuide:
    enrichment_llm = get_enrichment_provider()

    with record_stage(db, reel.id, "enrich") as ev:
        shallow_before = sum(1 for s in stubs if _is_shallow_beat(s))
        _enrich_with_insight(stubs, context, enrichment_llm)
        ev.detail["shallow_beats"] = shallow_before
        ev.detail["enriched_beats"] = shallow_before - sum(1 for s in stubs if _is_shallow_beat(s))

    conflict_visual: dict[int, str] = {}
    if not _has_conflict_beat(stubs):
        result = _make_conflict_stub(context, enrichment_llm, index=len(stubs) - 1)
        if result:
            conflict_stub, cv = result
            stubs.insert(-1, conflict_stub)
            for i, s in enumerate(stubs):
                s.index = i
            if cv:
                conflict_visual[conflict_stub.index] = cv

    # Backfill player names from VO so both the LLM hint AND the fallback path use them
    for s in stubs:
        if not s.player:
            s.player = _first_person(s.vo_script, "")

    beat_dicts = [
        {"index": s.index, "beat_type": s.beat_type,
         "duration_s": s.duration_s, "vo_script": s.vo_script,
         "player": s.player}
        for s in stubs
    ]
    visuals_messages = build_visuals_messages(beat_dicts, reel.niche or "")

    visuals: dict[int, str] = {}
    for attempt in range(3):
        raw = llm.complete(visuals_messages, json_mode=True)
        try:
            parsed = json.loads(raw)
            # Handle LLMs that wrap the array in a dict {"items": [...]} etc.
            if isinstance(parsed, dict):
                parsed = next((v for v in parsed.values() if isinstance(v, list)), [])
            if isinstance(parsed, list):
                for item in parsed:
                    idx = item.get("index")
                    vd = item.get("visual_direction", "")
                    if idx is not None and vd:
                        visuals[idx] = vd
                if visuals:
                    break
        except Exception:
            continue
    if not visuals:
        _log.warning("reel_id=%s: all 3 visuals attempts failed — using fallback visuals", reel.id)

    for idx, cv in conflict_visual.items():
        if not visuals.get(idx):
            visuals[idx] = cv

    niche = reel.niche or "general"
    niche_tag = niche.replace(" ", "").lower()
    caption, hashtags = _generate_caption_hashtags(stubs, niche, enrichment_llm)
    if not caption:
        caption = f"{niche.title()} breakdown — who makes the cut? #football #{niche_tag}"
        hashtags = [
            "football", "soccer", "footballanalysis", "socceranalysis",
            "footballreels", "sports", "sportscontent", "footballpundit",
            "footballhighlights", "soccerhighlights", niche_tag,
            "footballcontent", "sportstiktok", "footballtactics", "sportsreels",
        ][:15]

    platform_guides = []
    for cut in cuts:
        pg = _stubs_to_platform_guide(
            stubs, visuals,
            platform=cut.platform.value,
            target_length_s=cut.target_length_s or 45.0,
            caption=caption,
            hashtags=hashtags,
        )
        platform_guides.append(pg)

    title = niche.title() if niche and niche != "general" else "Squad Review"
    return MasterGuide(
        title=title,
        niche=niche,
        cuts=platform_guides,
    )


def _enrich_standard_path_guide(guide: MasterGuide, context: str) -> None:
    """Apply tactical insight enrichment to shallow body beats in a standard-path guide.

    Converts each platform guide's beats to BeatStubs, runs _enrich_with_insight,
    then writes enriched VO + recalculated duration + re-derived on_screen_text back.
    Each platform is enriched independently because beat sets may differ by platform.
    """
    enrichment_llm = get_enrichment_provider()
    for pg in guide.cuts:
        stubs = [
            BeatStub(
                index=b.index,
                beat_type=b.type,
                section="",
                player=_first_person(b.visual_direction, ""),
                vo_script=b.vo_script,
                duration_s=b.duration_s,
                on_screen_text=b.on_screen_text[:],
            )
            for b in pg.beats
        ]
        _enrich_with_insight(stubs, context, enrichment_llm)
        stub_map = {s.index: s for s in stubs}
        for beat in pg.beats:
            stub = stub_map.get(beat.index)
            if stub and stub.vo_script != beat.vo_script:
                beat.vo_script = stub.vo_script
                beat.duration_s = stub.duration_s
                beat.on_screen_text = _postprocess_derive_on_screen(stub.vo_script, max_lines=5)


def _heartbeat(db, job, progress: int) -> None:
    job.progress = progress
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()


@celery_app.task(bind=True, max_retries=2)
def generate_guide(self, job_id: int):
    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        if job is None:
            return
        # Idempotency guard: done = redelivery no-op; running = live sibling
        if job.status in (models.JobStatus.done, models.JobStatus.running):
            return

        reel = db.get(models.Reel, job.reel_id)
        if reel is None:
            raise ValueError(f"Reel {job.reel_id} no longer exists")
        effective_context = reel.enriched_context or reel.context

        job.status = models.JobStatus.running
        job.started_at = datetime.now(timezone.utc)
        job.heartbeat_at = job.started_at
        job.attempts = (job.attempts or 0) + 1
        job.progress = 10
        db.commit()

        cuts = db.query(models.Cut).filter(models.Cut.reel_id == reel.id).all()
        platforms = [c.platform.value for c in cuts]
        target_lengths = {c.platform.value: c.target_length_s or 45.0 for c in cuts}
        max_target = max(target_lengths.values())
        voiceover_mode = reel.voiceover_mode or "voiceover"

        llm = get_llm_provider()
        quality_threshold = QUALITY_THRESHOLD if is_nvidia_generation() else QUALITY_THRESHOLD_LOCAL
        guide = None
        last_exc: Exception | None = None
        last_score = 0
        last_issues: list[str] = []

        # ── Structured-script fast path ──────────────────────────────────────
        forced_path = (job.meta or {}).get("generation_path", "auto")
        stubs = script_parser.parse(effective_context)

        if forced_path == "standard":
            use_structured = False
        elif forced_path == "structured":
            use_structured = True
        else:  # auto
            use_structured = stubs is not None

        if use_structured:
            _heartbeat(db, job, 20)
            try:
                guide = _generate_from_structured_script(
                    reel, cuts, llm, db, target_lengths, stubs, context=effective_context
                )
                clean_guide(guide, voiceover_mode)
                rule_s, rule_i = score_guide(effective_context, guide, max_target)
                last_score, last_issues = _combined_score(
                    rule_s, rule_i, guide, effective_context,
                    db=db, reel_id=reel.id, attempt=1,
                )
                # Record path only after structured path succeeds
                job.meta = {**(job.meta or {}), "path": "structured", "stub_count": len(stubs) if stubs else 0}
                db.commit()
                # Fall through to standard path if quality is too low
                if last_score < quality_threshold:
                    job.meta = {**(job.meta or {}), "structured_score": last_score, "structured_fallback": True}
                    guide = None
            except Exception as exc:
                last_exc = exc
                guide = None

        # ── Standard LLM path (unstructured context or fallback) ─────────────
        if guide is None:
            last_exc = None  # structured-path exception must not pollute standard-path errors
            job.meta = {**(job.meta or {}), "path": "standard", "stub_count": len(stubs) if stubs else 0}
            db.commit()
            generate_provider = "nvidia" if is_nvidia_generation() else "ollama"
            feedback: list[str] = []
            best_guide: MasterGuide | None = None
            best_score = 0

            for attempt in range(3):
                _heartbeat(db, job, 20 + attempt * 20)

                messages = build_messages(
                    context=effective_context,
                    niche=reel.niche or "",
                    platforms=platforms,
                    voiceover_mode=voiceover_mode,
                    target_lengths=target_lengths,
                    prior_feedback=feedback or None,
                )

                with record_stage(db, reel.id, "generate",
                                  provider=generate_provider, attempt=attempt + 1) as ev:
                    raw = llm.complete(messages, json_mode=True)
                    ev.detail["raw_len"] = len(raw)

                try:
                    candidate = MasterGuide.model_validate_json(raw)
                except ValidationError as exc:
                    last_exc = exc
                    continue

                clean_guide(candidate, voiceover_mode)
                with record_stage(db, reel.id, "enrich",
                                  provider="nvidia" if settings.nvidia_api_key else "ollama",
                                  attempt=attempt + 1) as ev:
                    shallow_before = sum(
                        1 for pg in candidate.cuts for b in pg.beats
                        if b.type == "body" and b.vo_script
                        and len(b.vo_script.split()) < 25
                    )
                    _enrich_standard_path_guide(candidate, effective_context)
                    ev.detail["shallow_beats"] = shallow_before

                rule_s, rule_i = score_guide(effective_context, candidate, max_target)
                last_score, last_issues = _combined_score(
                    rule_s, rule_i, candidate, effective_context,
                    db=db, reel_id=reel.id, attempt=attempt + 1,
                )

                if last_score >= quality_threshold:
                    guide = candidate
                    break

                # Track best-of-N so we can accept it if all retries fail
                if last_score > best_score:
                    best_score = last_score
                    best_guide = candidate

                feedback = [i for i in last_issues if not i.startswith("Score breakdown")]
                last_exc = None

            # Accept best-of-N rather than failing when nothing clears the threshold
            if guide is None and best_guide is not None:
                guide = best_guide
                last_score = best_score

        if guide is None:
            if last_exc:
                raise ValueError(
                    f"LLM returned invalid guide after 3 attempts: {last_exc}"
                ) from last_exc
            raise ValueError(
                f"Guide quality score {last_score}/100 after 3 attempts — "
                f"below {quality_threshold}. Issues: {'; '.join(last_issues)}"
            )

        _heartbeat(db, job, 80)

        for platform_guide in guide.cuts:
            cut = next(
                (c for c in cuts if c.platform.value == platform_guide.platform), None
            )
            if cut is None:
                continue
            cut.guide = platform_guide.model_dump()
            cut.caption = platform_guide.caption
            cut.hashtags = platform_guide.hashtags

        transition(reel, "guide_ready", REEL_TRANSITIONS)
        job.progress = 100
        job.heartbeat_at = datetime.now(timezone.utc)
        job.status = models.JobStatus.done
        job.error = None
        job.meta = {**(job.meta or {}), "quality_score": last_score}
        db.commit()

    except Exception as exc:
        db.rollback()
        if should_retry(exc, self.request.retries, self.max_retries):
            job = db.get(models.Job, job_id)
            if job:
                # Reset to pending: the idempotency guard rejects `running`, so a
                # retry that left the status alone would be a silent no-op.
                job.status = models.JobStatus.pending
                job.error = f"transient failure, retry {self.request.retries + 1}: {exc}"[:2000]
                db.commit()
            raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
            job.error = str(exc)[:2000]
            reel = db.get(models.Reel, job.reel_id) if job.reel_id else None
            if reel and reel.status.value == "generating":
                try:
                    transition(reel, "failed", REEL_TRANSITIONS)
                except ValueError:
                    pass
            db.commit()
        raise
    finally:
        db.close()
