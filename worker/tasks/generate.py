import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple, TypeVar

from celery.exceptions import SoftTimeLimitExceeded
from pydantic import ValidationError

from api import models
from api.config import settings
from api.state import REEL_TRANSITIONS, transition
from engine.generation.beat_enrichment import (
    _enrich_with_insight,
    _has_conflict_beat,
    _is_shallow_beat,
    _make_conflict_stub,
)
from engine.generation.estimate import resolve_generation_path
from engine.generation.evaluator import score_guide
from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.hook_variants import generate_hook_variants
from engine.generation.llm import get_llm_provider, get_enrichment_provider, is_nvidia_generation
from engine.generation.llm_judge import judge_guide
from engine.generation.postprocess import clean_guide
from engine.generation.pricing import llm_cost_usd
from engine.generation.prompt import build_messages, build_visuals_messages
from engine.generation.script_parser import BeatStub
from engine.generation import script_parser
from engine.generation.visual_fallback import (
    _fallback_visual,
    _first_person,
    _is_degenerate_visual,
)
from engine.observability import paid_call_count, record_stage
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat, job_task, time_limits

_log = logging.getLogger(__name__)

QUALITY_THRESHOLD = 80
QUALITY_THRESHOLD_LOCAL = 65  # lower bar for local Ollama models
_JUDGE_RULE_MIN = 55


def _enforce_paid_call_budget(db, reel_id: int) -> None:
    """Raise a deterministic (non-retried) error once a reel hits its paid-call cap.

    Checked at task entry (catches a stuck Celery retry re-running the whole task)
    and again before each standard-path attempt (catches a single run's own
    generate/enrich/judge loop). A no-op when NVIDIA isn't configured, since local
    Ollama calls are free and never count toward the cap.
    """
    count = paid_call_count(db, reel_id)
    if count >= settings.max_paid_llm_calls_per_reel:
        raise ValueError(
            f"Paid LLM call budget exceeded for this reel ({count}/"
            f"{settings.max_paid_llm_calls_per_reel} calls) — see the reel's pipeline "
            "panel for cost history, or raise MAX_PAID_LLM_CALLS_PER_REEL in .env."
        )


# Reaper-resume cost note (design §7; corrected after a second review pass found the first
# version of this comment too optimistic): the "resumed job runs concurrently with a live
# zombie for at most one attempt's worth of paid calls" bound below is an EMERGENT side effect
# of heartbeat() placement, not a designed or enforced invariant — and it only holds for the
# STANDARD path's attempt loop (heartbeat(db, job, 20 + attempt * 20) once per iteration,
# immediately below). It does NOT hold for the structured-script fast path above: there is
# exactly one heartbeat() call (`heartbeat(db, job, 20)`) before
# _generate_from_structured_script() runs an unfenced enrich call, an optional conflict-stub
# call, and its own internal visuals-LLM retries, followed by an unfenced judge call in
# _combined_score() — no fencing checkpoint anywhere in that stretch. A zombie on the
# structured path can therefore burn several paid calls, not "one attempt's worth," before its
# next heartbeat() (the standard path's first iteration, reached only on fallback, or the
# final heartbeat(db, job, 80) near the end of this function) raises JobLost. generate's resume
# budget is capped at 1 (not enrich/render's 2) partly because of this — but that number is a
# conservative choice given the uncertainty here, not a value derived from a proven bound.
# record_stage() and paid_call_count() (engine/observability.py) have zero fencing/claim_token-
# awareness at all: nothing stops a zombie's StageEvent writes or a concurrent
# _enforce_paid_call_budget() read from happening at any point in either path. See
# _RESUMABLE_TASKS in worker/tasks/maintenance.py.



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
            usage = getattr(enrichment_llm, "last_usage", {})
            ev.tokens_in = usage.get("prompt_tokens")
            ev.tokens_out = usage.get("completion_tokens")
            ev.cost_usd = llm_cost_usd(judge_provider, ev.tokens_in, ev.tokens_out)
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
            # Structured-script beats never carry an LLM-written music_cue (that's
            # only produced by the standard path's full guide generation) — without
            # this, render_cut would never find a cue to look up a music track for,
            # and the structured path (the faster, more commonly recommended one)
            # would silently never get background music. A single genre-neutral
            # default on the hook beat is enough: render_cut uses the first
            # non-empty cue across all beats for the whole cut's music track.
            music_cue="upbeat energetic" if s.beat_type == "hook" else None,
        ))
    return PlatformGuide(
        platform=platform,
        target_length_s=target_length_s,
        beats=beats,
        caption=caption,
        hashtags=hashtags,
    )


def _generate_caption_hashtags(
    vo_scripts: list[str], niche: str, llm
) -> tuple[str, list[str]]:
    """Ask the LLM to write a caption and hashtags from the actual VO content.

    Takes plain VO strings (not BeatStub) so both generation paths can call it —
    the structured path passes `[s.vo_script for s in stubs]`, the standard path
    passes `[b.vo_script for b in guide.cuts[0].beats]` (see generate_guide()'s
    standard-path caption/hashtags regeneration, Phase 7o — see CLAUDE.md's Key
    conventions and docs/roadmap.md's Open Issues table)."""
    combined_vo = " ".join(v for v in vo_scripts if v)[:600]
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
    except SoftTimeLimitExceeded:
        # llm.complete() is the blocking call inside this try; a bare except Exception here would
        # silently launder a runtime-limit breach into "no caption, use the fallback template" and
        # let the task keep running to a normal-looking completion instead of failing visibly.
        raise
    except Exception:
        pass
    return "", []


def _generate_from_structured_script(
    reel, cuts, llm, db, target_lengths: dict, stubs: list[BeatStub], context: str
) -> MasterGuide:
    enrichment_llm = get_enrichment_provider()
    enrich_provider = "nvidia" if settings.nvidia_api_key else "ollama"

    with record_stage(db, reel.id, "enrich", provider=enrich_provider) as ev:
        usage_before = dict(getattr(enrichment_llm, "total_usage", {}))
        shallow_before = sum(1 for s in stubs if _is_shallow_beat(s, reel.niche))
        _enrich_with_insight(stubs, context, enrichment_llm, reel.niche)
        ev.detail["shallow_beats"] = shallow_before
        ev.detail["enriched_beats"] = shallow_before - sum(1 for s in stubs if _is_shallow_beat(s, reel.niche))
        usage_after = getattr(enrichment_llm, "total_usage", {})
        ev.tokens_in = usage_after.get("prompt_tokens", 0) - usage_before.get("prompt_tokens", 0)
        ev.tokens_out = usage_after.get("completion_tokens", 0) - usage_before.get("completion_tokens", 0)
        ev.cost_usd = llm_cost_usd(enrich_provider, ev.tokens_in, ev.tokens_out)

    conflict_visual: dict[int, str] = {}
    if not _has_conflict_beat(stubs):
        with record_stage(db, reel.id, "enrich_conflict", provider=enrich_provider) as ev:
            result = _make_conflict_stub(context, enrichment_llm, index=len(stubs) - 1)
            usage = getattr(enrichment_llm, "last_usage", {})
            ev.tokens_in = usage.get("prompt_tokens")
            ev.tokens_out = usage.get("completion_tokens")
            ev.cost_usd = llm_cost_usd(enrich_provider, ev.tokens_in, ev.tokens_out)
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
    generate_provider = "nvidia" if is_nvidia_generation() else "ollama"
    with record_stage(db, reel.id, "visuals", provider=generate_provider) as ev:
        usage_before = dict(getattr(llm, "total_usage", {}))
        attempts_made = 0
        for attempt in range(3):
            attempts_made += 1
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
        ev.detail["attempts"] = attempts_made
        usage_after = getattr(llm, "total_usage", {})
        ev.tokens_in = usage_after.get("prompt_tokens", 0) - usage_before.get("prompt_tokens", 0)
        ev.tokens_out = usage_after.get("completion_tokens", 0) - usage_before.get("completion_tokens", 0)
        ev.cost_usd = llm_cost_usd(generate_provider, ev.tokens_in, ev.tokens_out)
    if not visuals:
        _log.warning("reel_id=%s: all 3 visuals attempts failed — using fallback visuals", reel.id)

    for idx, cv in conflict_visual.items():
        if not visuals.get(idx):
            visuals[idx] = cv

    niche = reel.niche or "general"
    niche_tag = niche.replace(" ", "").lower()
    with record_stage(db, reel.id, "caption_hashtags", provider=enrich_provider) as ev:
        caption, hashtags = _generate_caption_hashtags(
            [s.vo_script for s in stubs], niche, enrichment_llm
        )
        usage = getattr(enrichment_llm, "last_usage", {})
        ev.tokens_in = usage.get("prompt_tokens")
        ev.tokens_out = usage.get("completion_tokens")
        ev.cost_usd = llm_cost_usd(enrich_provider, ev.tokens_in, ev.tokens_out)
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


def _enrich_standard_path_guide(guide: MasterGuide, context: str, enrichment_llm) -> None:
    """Apply tactical insight enrichment to shallow body beats in a standard-path guide.

    Converts each platform guide's beats to BeatStubs, runs _enrich_with_insight,
    then writes enriched VO + recalculated duration + re-derived on_screen_text back.
    Each platform is enriched independently because beat sets may differ by platform.

    Takes enrichment_llm from the caller (rather than creating its own) so the caller
    can read cumulative token usage off the same instance for cost tracking.
    """
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
        _enrich_with_insight(stubs, context, enrichment_llm, guide.niche)
        stub_map = {s.index: s for s in stubs}
        for beat in pg.beats:
            stub = stub_map.get(beat.index)
            if stub and stub.vo_script != beat.vo_script:
                beat.vo_script = stub.vo_script
                beat.duration_s = stub.duration_s
                beat.on_screen_text = script_parser.derive_on_screen(stub.vo_script, max_items=5)


def _prepare_generate(db, job):
    """Load the owning reel, refuse a reel that is no longer generating, enforce the budget."""
    reel = db.get(models.Reel, job.reel_id)
    if reel is None:
        raise ValueError(f"Reel {job.reel_id} no longer exists")
    # Defence in depth: never spend paid LLM calls on a reel that is no longer generating
    # (its final transition to guide_ready would fail anyway). enrich_context normally fails
    # an un-enqueued generate job first, so this is rarely reached.
    if reel.status.value != "generating":
        raise ValueError(f"Reel {reel.id} is '{reel.status.value}', not 'generating' — not running")
    _enforce_paid_call_budget(db, reel.id)
    return reel


def _strip_stale_fallback_meta(db, job) -> None:
    """A killed or ordinarily-retried attempt of this SAME Job row may have already committed
    "structured_fallback"/"structured_score"/"path" from a prior run (e.g. one that took the
    structured path, failed its quality gate, and fell through to standard, then got SIGKILLed
    before finishing). job.meta is always merged additively (see generate_guide's own writes),
    never reset, so those stale keys would otherwise survive into this run's final job.meta even
    when THIS run's structured path succeeds cleanly with no fallback at all — corrupting
    engine/generation/estimate.py::estimate_generation()'s structured_fallback-exclusion bucketing
    for every future reel on this path. Reaper-resume turns this from a rare, barely-reachable
    edge case into an operationally common one, which is why the fix lands here rather than being
    filed separately — see CLAUDE.md's Key conventions entry on the fencing-token/resume
    mechanism. Only these three keys are stripped: never context_score, performance_note_ids, or
    any other legitimately-persisted key.
    """
    stale_meta_keys = {"structured_fallback", "structured_score", "path"}
    if job.meta and stale_meta_keys & job.meta.keys():
        job.meta = {k: v for k, v in job.meta.items() if k not in stale_meta_keys}
        db.commit()


def _load_active_performance_notes(db) -> list["models.PerformanceNote"]:
    """Queried once, unconditionally, before the structured-vs-standard branch — the shared
    job.meta write at the end of generate_guide (reached by BOTH paths) records
    performance_note_ids, and the standard path's prior_feedback seed also reads these notes'
    text. Querying this only inside the standard path would NameError on every structured-path
    success — see docs/specs/2026-09-phase5-quality-engagement-feedback.md §3.5.
    """
    return (
        db.query(models.PerformanceNote)
        .filter(models.PerformanceNote.active.is_(True)).all()
    )


# Worst case from the code: up to 3 standard-path attempts, each a generate call plus batched
# enrichment and a judge call, at the 360 s per-call LLM timeout, is about 3.2 h.
_MAX_RUNTIME_S = 4 * 60 * 60


@dataclass
class _GenerationContext:
    """Read-only bundle of per-run state every generate_guide() section needs, computed once
    near the top of the task and never reassigned afterward — mutation happens only through
    ctx.job.meta / ctx.db (ordinary ORM/session mutation, unchanged in kind from before this
    decomposition). Threading these 13 values individually through _try_structured_path(),
    _run_standard_path_attempts(), _maybe_regenerate_caption_hashtags(), and
    _maybe_generate_hook_variants() was rejected (see
    docs/specs/2026-09-generate-guide-decomposition-module-design.md) — it reproduces the exact
    unbundled-parameter-list risk the decomposition exists to avoid.
    """
    db: Any
    job: Any
    reel: Any
    cuts: list
    platforms: list[str]
    target_lengths: dict[str, float]
    max_target: float
    voiceover_mode: str
    effective_context: str
    active_notes: list[str]
    axis_multipliers: dict[str, float] | None
    quality_threshold: int
    llm: Any


class GuideResult(NamedTuple):
    guide: MasterGuide | None
    score: int
    issues: list[str]
    exc: Exception | None


T = TypeVar("T")


def _best_effort_llm_call(
    db, reel_id: int, stage: str, call: Callable[[Any], T],
    detail: Callable[[T], dict] | None = None,
) -> T | None:
    """Run call(llm) under this reel's paid-call budget, best-effort — returns None (never
    raises for a budget or ordinary-failure reason) when the budget is already exhausted.
    `call` itself owns its own failure-degradation contract (e.g. _generate_caption_hashtags
    degrades to ("", []); generate_hook_variants degrades to []) — this wrapper only owns
    budget gating, provider selection, and record_stage instrumentation, identical across both
    current call sites (caption/hashtags regeneration and hook-variant generation) before this
    decomposition. `detail`, if given, is applied to ev.detail from inside the same
    record_stage transaction, preserving each call site's own diagnostic keys
    ("replaced"/"variant_count") exactly as before.
    """
    if paid_call_count(db, reel_id) >= settings.max_paid_llm_calls_per_reel:
        return None
    provider = "nvidia" if settings.nvidia_api_key else "ollama"
    llm = get_enrichment_provider()
    with record_stage(db, reel_id, stage, provider=provider) as ev:
        result = call(llm)
        usage = getattr(llm, "last_usage", {})
        ev.tokens_in = usage.get("prompt_tokens")
        ev.tokens_out = usage.get("completion_tokens")
        ev.cost_usd = llm_cost_usd(provider, ev.tokens_in, ev.tokens_out)
        if detail is not None:
            ev.detail.update(detail(result))
    return result


def _try_structured_path(ctx: _GenerationContext, stubs: list[BeatStub]) -> GuideResult:
    """Run the structured-script fast path: generate + score. On success records
    job.meta["path"]="structured" (only after success, matching the original inline
    ordering); if the guide misses the quality gate, also records
    job.meta["structured_score"]/["structured_fallback"]=True and returns guide=None so the
    caller falls through to the standard path. Owns exactly these job.meta keys — never
    "quality_score"/"performance_note_ids" (generate_guide's own, written once at the very end
    regardless of which path produced the guide).

    Re-raises SoftTimeLimitExceeded uncaught: the runtime limit ends the whole task, and
    falling back to the standard path would silently ignore it.
    """
    heartbeat(ctx.db, ctx.job, 20)
    try:
        guide = _generate_from_structured_script(
            ctx.reel, ctx.cuts, ctx.llm, ctx.db, ctx.target_lengths, stubs,
            context=ctx.effective_context,
        )
        clean_guide(guide, ctx.voiceover_mode)
        rule_s, rule_i = score_guide(ctx.effective_context, guide, ctx.max_target, ctx.axis_multipliers)
        score, issues = _combined_score(
            rule_s, rule_i, guide, ctx.effective_context,
            db=ctx.db, reel_id=ctx.reel.id, attempt=1,
        )
        # Record path only after structured path succeeds
        ctx.job.meta = {**(ctx.job.meta or {}), "path": "structured", "stub_count": len(stubs) if stubs else 0}
        ctx.db.commit()
        # Fall through to standard path if quality is too low
        if score < ctx.quality_threshold:
            ctx.job.meta = {**(ctx.job.meta or {}), "structured_score": score, "structured_fallback": True}
            return GuideResult(None, score, issues, None)
        return GuideResult(guide, score, issues, None)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        return GuideResult(None, 0, [], exc)


def _run_standard_path_attempts(ctx: _GenerationContext, stubs: list[BeatStub] | None) -> GuideResult:
    """Run the standard LLM path's up-to-3-attempt retry loop with closed-loop feedback and
    best-of-N acceptance. Records job.meta["path"]="standard" unconditionally at entry — the
    last path to actually run wins, matching the original inline behavior exactly (a structured-
    path fallback overwrites "path" from "structured" to "standard").

    feedback MUST stay additive (active_notes + issues), never a plain replace — active_notes is
    seeded into `feedback` before attempt 1, and a wholesale overwrite on a later attempt would
    silently drop those performance notes. See
    docs/specs/2026-09-phase5-quality-engagement-feedback.md §3.5 and
    tests/test_generate_task.py::test_seeded_performance_notes_survive_past_attempt_1_on_retry.
    """
    ctx.job.meta = {**(ctx.job.meta or {}), "path": "standard", "stub_count": len(stubs) if stubs else 0}
    ctx.db.commit()
    generate_provider = "nvidia" if is_nvidia_generation() else "ollama"
    # Seeded from attempt 1, not just retries — every active PerformanceNote is automatic
    # prompt-injection plumbing once an operator has curated it (see
    # docs/specs/2026-09-phase5-quality-engagement-feedback.md §3.2).
    feedback: list[str] = list(ctx.active_notes)
    best_guide: MasterGuide | None = None
    best_score = 0
    last_exc: Exception | None = None
    last_score = 0
    last_issues: list[str] = []
    guide: MasterGuide | None = None

    for attempt in range(3):
        _enforce_paid_call_budget(ctx.db, ctx.reel.id)
        heartbeat(ctx.db, ctx.job, 20 + attempt * 20)

        messages = build_messages(
            context=ctx.effective_context,
            niche=ctx.reel.niche or "",
            platforms=ctx.platforms,
            voiceover_mode=ctx.voiceover_mode,
            target_lengths=ctx.target_lengths,
            prior_feedback=feedback or None,
        )

        with record_stage(ctx.db, ctx.reel.id, "generate",
                          provider=generate_provider, attempt=attempt + 1) as ev:
            raw = ctx.llm.complete(messages, json_mode=True)
            ev.detail["raw_len"] = len(raw)
            usage = getattr(ctx.llm, "last_usage", {})
            ev.tokens_in = usage.get("prompt_tokens")
            ev.tokens_out = usage.get("completion_tokens")
            ev.cost_usd = llm_cost_usd(generate_provider, ev.tokens_in, ev.tokens_out)

        try:
            candidate = MasterGuide.model_validate_json(raw)
        except ValidationError as exc:
            last_exc = exc
            continue

        clean_guide(candidate, ctx.voiceover_mode)
        enrich_provider = "nvidia" if settings.nvidia_api_key else "ollama"
        enrichment_llm = get_enrichment_provider()
        with record_stage(ctx.db, ctx.reel.id, "enrich",
                          provider=enrich_provider,
                          attempt=attempt + 1) as ev:
            usage_before = dict(getattr(enrichment_llm, "total_usage", {}))
            shallow_before = sum(
                1 for pg in candidate.cuts for b in pg.beats
                if b.type == "body" and b.vo_script
                and len(b.vo_script.split()) < 25
            )
            _enrich_standard_path_guide(candidate, ctx.effective_context, enrichment_llm)
            ev.detail["shallow_beats"] = shallow_before
            usage_after = getattr(enrichment_llm, "total_usage", {})
            ev.tokens_in = usage_after.get("prompt_tokens", 0) - usage_before.get("prompt_tokens", 0)
            ev.tokens_out = usage_after.get("completion_tokens", 0) - usage_before.get("completion_tokens", 0)
            ev.cost_usd = llm_cost_usd(enrich_provider, ev.tokens_in, ev.tokens_out)

        rule_s, rule_i = score_guide(ctx.effective_context, candidate, ctx.max_target, ctx.axis_multipliers)
        last_score, last_issues = _combined_score(
            rule_s, rule_i, candidate, ctx.effective_context,
            db=ctx.db, reel_id=ctx.reel.id, attempt=attempt + 1,
        )

        if last_score >= ctx.quality_threshold:
            guide = candidate
            break

        # Track best-of-N so we can accept it if all retries fail
        if last_score > best_score:
            best_score = last_score
            best_guide = candidate

        feedback = ctx.active_notes + [i for i in last_issues if not i.startswith("Score breakdown")]
        last_exc = None

    # Accept best-of-N rather than failing when nothing clears the threshold
    if guide is None and best_guide is not None:
        guide = best_guide
        last_score = best_score

    # Owned by the STANDARD path only, matching the original inline nesting exactly (it
    # sat inside `if guide is None:`, never reached on a direct structured-path success) --
    # a security/correctness review of the first version of this decomposition caught a
    # real regression where this call had moved to the orchestrator and run unconditionally,
    # double-regenerating (and potentially overwriting with a second, nondeterministic LLM
    # result) a structured-path guide that already has its own content-aware caption/
    # hashtags from _generate_from_structured_script()'s own _generate_caption_hashtags()
    # call. See docs/specs/2026-09-generate-guide-decomposition-module-design.md §5.
    if guide is not None:
        _maybe_regenerate_caption_hashtags(ctx, guide)

    return GuideResult(guide, last_score, last_issues, last_exc)


def _maybe_regenerate_caption_hashtags(ctx: _GenerationContext, guide: MasterGuide) -> None:
    """Best-effort: regenerate caption/hashtags from the ACCEPTED guide's real VO content,
    mirroring the structured path's own _generate_caption_hashtags() call (Phase 7o) — the
    single-shot MasterGuide JSON call writes SOME caption/hashtags from niche/context alone,
    with no explicit instruction to ground them in the beats it just wrote. Mutates
    guide.cuts[*].caption/hashtags in place on success; leaves them untouched on any failure
    (budget exhausted, empty VO, or _generate_caption_hashtags() itself degrading to ("", [])) —
    never worse than before this feature existed.

    Skips the call entirely (not just discards its result) when every vo_script is empty —
    music_only/silent voiceover_mode leaves them that way by design (build_messages()'s
    vo_note), and the call could never succeed anyway.
    """
    if not guide.cuts:
        return
    vo_scripts = [b.vo_script for b in guide.cuts[0].beats]
    if not any(v.strip() for v in vo_scripts):
        return
    niche = ctx.reel.niche or "general"
    result = _best_effort_llm_call(
        ctx.db, ctx.reel.id, "caption_hashtags",
        lambda llm: _generate_caption_hashtags(vo_scripts, niche, llm),
        detail=lambda r: {"replaced": bool(r[0] and r[1])},
    )
    if result is None:
        return
    new_caption, new_hashtags = result
    if new_caption and new_hashtags:
        for platform_guide in guide.cuts:
            platform_guide.caption = new_caption
            platform_guide.hashtags = new_hashtags


def _maybe_generate_hook_variants(ctx: _GenerationContext, guide: MasterGuide) -> list[str]:
    """Best-effort: one cheap extra call for alternate hook lines. All platform guides
    normally share identical beats, so this runs once per reel, not once per cut, and the same
    variant list is applied to every cut by _persist_guide(). A failure or an exhausted
    paid-call budget just means no variants, never a failed generate job.
    """
    hook_beat = guide.cuts[0].beats[0] if guide.cuts and guide.cuts[0].beats else None
    if hook_beat is None or hook_beat.type != "hook" or not hook_beat.vo_script.strip():
        return []
    context = ctx.effective_context
    niche = ctx.reel.niche or ""
    result = _best_effort_llm_call(
        ctx.db, ctx.reel.id, "hook_variants",
        lambda llm: generate_hook_variants(hook_beat.vo_script, context, niche, llm),
        detail=lambda r: {"variant_count": len(r)},
    )
    return result or []


def _persist_guide(cuts, guide: MasterGuide, hook_variants: list[str]) -> None:
    """Write the accepted guide onto every matching Cut row. A platform_guide with no matching
    Cut (shouldn't happen — defensive) is silently skipped, unchanged from the original inline
    loop."""
    for platform_guide in guide.cuts:
        cut = next(
            (c for c in cuts if c.platform.value == platform_guide.platform), None
        )
        if cut is None:
            continue
        cut.guide = platform_guide.model_dump()
        cut.caption = platform_guide.caption
        cut.hashtags = platform_guide.hashtags
        cut.hook_variants = hook_variants or None


@celery_app.task(bind=True, max_retries=2, **time_limits(_MAX_RUNTIME_S))
@job_task("generate", prepare=_prepare_generate, start_progress=10, max_runtime_s=_MAX_RUNTIME_S)
def generate_guide(self, db, job, reel):
    effective_context = reel.enriched_context or reel.context
    _strip_stale_fallback_meta(db, job)
    active_notes_rows = _load_active_performance_notes(db)
    active_notes = [n.text for n in active_notes_rows]

    cuts = db.query(models.Cut).filter(models.Cut.reel_id == reel.id).all()
    platforms = [c.platform.value for c in cuts]
    target_lengths = {c.platform.value: c.target_length_s or 45.0 for c in cuts}
    max_target = max(target_lengths.values())
    voiceover_mode = reel.voiceover_mode or "voiceover"

    llm = get_llm_provider()
    quality_threshold = QUALITY_THRESHOLD if is_nvidia_generation() else QUALITY_THRESHOLD_LOCAL

    ctx = _GenerationContext(
        db=db, job=job, reel=reel, cuts=cuts, platforms=platforms,
        target_lengths=target_lengths, max_target=max_target, voiceover_mode=voiceover_mode,
        effective_context=effective_context, active_notes=active_notes,
        axis_multipliers=settings.evaluator_axis_weight_multipliers or None,
        quality_threshold=quality_threshold, llm=llm,
    )

    # ── Structured-script fast path ──────────────────────────────────────
    forced_path = (job.meta or {}).get("generation_path", "auto")
    stubs = script_parser.parse(effective_context)
    # resolve_generation_path() (shared with the pre-generation cost
    # estimate, which has no stubs to check) decides via is_structured()
    # alone. is_structured() is a cheaper header-count check that parse()
    # itself starts with — but parse() can still return None even when
    # is_structured() is True, if every detected section's body is empty
    # after stripping labels/player prefixes. Require stubs is not None
    # here so that disagreement falls straight through to the standard
    # path instead of calling _try_structured_path with stubs=None and
    # wasting an attempt on a guaranteed TypeError.
    use_structured = (
        resolve_generation_path(effective_context, forced_path) == "structured"
        and stubs is not None
    )

    result = _try_structured_path(ctx, stubs) if use_structured else GuideResult(None, 0, [], None)

    # ── Standard LLM path (unstructured context or fallback) ─────────────
    if result.guide is None:
        result = _run_standard_path_attempts(ctx, stubs)

    if result.guide is None:
        if result.exc:
            raise ValueError(
                f"LLM returned invalid guide after 3 attempts: {result.exc}"
            ) from result.exc
        raise ValueError(
            f"Guide quality score {result.score}/100 after 3 attempts — "
            f"below {quality_threshold}. Issues: {'; '.join(result.issues)}"
        )

    guide = result.guide
    heartbeat(db, job, 80)

    # Caption/hashtags regeneration is called from inside _run_standard_path_attempts()
    # itself, not here -- it is standard-path-only (see that function's own comment and
    # docs/specs/2026-09-generate-guide-decomposition-module-design.md §5). Hook-variant
    # generation, unlike caption regen, was already guide-agnostic in the original inline
    # code (it ran unconditionally after either path produced a guide) and stays that way.
    hook_variants = _maybe_generate_hook_variants(ctx, guide)
    _persist_guide(cuts, guide, hook_variants)

    transition(reel, "guide_ready", REEL_TRANSITIONS)
    # Shared by both paths — active_notes_rows is always defined by now regardless of
    # which path produced `guide` (queried unconditionally at the top of this function).
    job.meta = {**(job.meta or {}), "quality_score": result.score,
                "performance_note_ids": [n.id for n in active_notes_rows]}
