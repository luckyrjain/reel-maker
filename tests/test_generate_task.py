"""Tests for the generate_guide task's failure handling — missing rows and retries."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry

from api import models
from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.script_parser import BeatStub
from worker.tasks.generate import _stubs_to_platform_guide


def _job(job_id=1, reel_id=10):
    job = MagicMock()
    job.id = job_id
    job.reel_id = reel_id
    job.cut_id = None
    job.status = models.JobStatus.pending
    job.attempts = 0
    job.progress = 0
    job.meta = {}
    job.error = None
    return job


def _reel():
    reel = MagicMock()
    reel.id = 10
    reel.context = "Argentina squad review."
    reel.enriched_context = None
    reel.niche = "football"
    reel.voiceover_mode = "voiceover"
    reel.status.value = "generating"
    return reel


def _cut():
    cut = MagicMock()
    cut.platform.value = "youtube_shorts"
    cut.target_length_s = 45.0
    return cut


def test_missing_reel_fails_job_with_actionable_message():
    """A deleted reel must produce an operator-readable error, not an AttributeError."""
    from worker.tasks.generate import generate_guide

    job = _job(reel_id=99)
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else None

    with patch("worker.tasks.common.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="Reel 99 no longer exists"):
            generate_guide(1)

    assert job.status == models.JobStatus.failed
    assert "Reel 99 no longer exists" in job.error
    assert "AttributeError" not in job.error


def test_reel_rolled_back_to_failed_is_not_generated():
    """enrich_context rolls the reel back when it could not confirm the enqueue; a message that
    still reached the broker must not spend paid LLM calls on a failed reel."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    reel.status.value = "failed"
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider") as mock_llm,
    ):
        with pytest.raises(ValueError, match="not 'generating'"):
            generate_guide(1)

    mock_llm.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_paid_call_budget_exceeded_fails_without_retry():
    """A reel that already hit its paid-call cap must fail cleanly, not retry."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=20),
        patch.object(generate_guide, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(ValueError, match="Paid LLM call budget exceeded"):
            generate_guide(1)

    mock_retry.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_structured_path_skipped_when_parse_disagrees_with_is_structured():
    """resolve_generation_path()'s "auto" branch decides via is_structured()
    alone (it has no stubs to check — it's shared with the pre-generation cost
    estimate endpoint, which never parses anything). parse() runs the same
    is_structured() check first but can still return None afterward. If those
    two ever disagree, generate_guide must not call
    _generate_from_structured_script with stubs=None — it must fall straight
    to the standard path instead of wasting an attempt on a guaranteed
    TypeError that then gets silently swallowed."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.return_value.filter.return_value.all.return_value = [_cut()]

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.script_parser.parse", return_value=None),
        patch("worker.tasks.generate.resolve_generation_path", return_value="structured"),
        patch("worker.tasks.generate._generate_from_structured_script") as mock_structured,
        # get_llm_provider() is called unconditionally before either path is
        # selected — let it succeed, and fail the standard path itself
        # instead, right after it records job.meta["path"] = "standard".
        patch("worker.tasks.generate.build_messages",
              side_effect=ValueError("standard path reached")),
        patch.object(generate_guide, "retry", side_effect=Retry()),
    ):
        with pytest.raises(ValueError, match="standard path reached"):
            generate_guide(1)

    mock_structured.assert_not_called()
    assert job.meta.get("path") == "standard"


# ── _stubs_to_platform_guide — music_cue defaulting ──────────────────────────

def _stub(index, beat_type, vo="Some voiceover line here."):
    return BeatStub(
        index=index, beat_type=beat_type, section="", player="",
        vo_script=vo, duration_s=5.0, on_screen_text=["Text"],
    )


def test_structured_path_hook_beat_gets_a_default_music_cue():
    """Structured-script beats never carry an LLM-written music_cue — without a
    default, render_cut would never find a cue to look up a music track for and
    the structured path would silently never get background music.
    """
    stubs = [_stub(0, "hook"), _stub(1, "body"), _stub(2, "cta")]
    guide = _stubs_to_platform_guide(
        stubs, visuals={}, platform="youtube_shorts", target_length_s=30.0,
        caption="Caption", hashtags=["tag"] * 6,
    )
    assert guide.beats[0].type == "hook"
    assert guide.beats[0].music_cue == "upbeat energetic"


def test_structured_path_non_hook_beats_have_no_music_cue():
    """Only one cue is needed — render_cut picks the first non-empty cue across
    all beats for the whole cut's music track, so body/cta beats stay None.
    """
    stubs = [_stub(0, "hook"), _stub(1, "body"), _stub(2, "cta")]
    guide = _stubs_to_platform_guide(
        stubs, visuals={}, platform="youtube_shorts", target_length_s=30.0,
        caption="Caption", hashtags=["tag"] * 6,
    )
    assert guide.beats[1].music_cue is None
    assert guide.beats[2].music_cue is None


def test_the_soft_time_limit_in_the_structured_path_does_not_fall_back_to_the_standard_path():
    """The structured path catches Exception and falls back to the standard path; that would
    swallow the runtime limit and keep running to the hard kill.

    Narrowed to a direct test of _try_structured_path() after the generate_guide()
    decomposition (module design CAR-1, docs/specs/2026-09-generate-guide-decomposition-
    module-design.md) -- no longer needs to drive the whole task or mock any standard-path
    dependency (build_messages, path-resolution, SessionLocal, job_task's own lifecycle):
    "does the standard path get reached" is no longer even representable here, since this
    function never calls it -- the property is proven by never reaching the return."""
    from celery.exceptions import SoftTimeLimitExceeded
    from worker.tasks.generate import _GenerationContext, _try_structured_path

    reel = _reel()
    db = MagicMock()
    ctx = _GenerationContext(
        db=db, job=_job(), reel=reel, cuts=[_cut()], platforms=["youtube_shorts"],
        target_lengths={"youtube_shorts": 45.0}, max_target=45.0, voiceover_mode="voiceover",
        effective_context=reel.context, active_notes=[], axis_multipliers=None,
        quality_threshold=65, llm=MagicMock(),
    )

    with patch(
        "worker.tasks.generate._generate_from_structured_script",
        side_effect=SoftTimeLimitExceeded(),
    ):
        with pytest.raises(SoftTimeLimitExceeded):
            _try_structured_path(ctx, stubs=[MagicMock()])


def test_generate_caption_hashtags_does_not_swallow_the_soft_time_limit():
    """A bare except Exception around the blocking llm.complete() call would silently launder a
    runtime-limit breach into 'no caption, use the fallback template' and let the task run on to a
    normal-looking completion instead of failing visibly."""
    from celery.exceptions import SoftTimeLimitExceeded
    from worker.tasks.generate import _generate_caption_hashtags

    llm = MagicMock()
    llm.complete.side_effect = SoftTimeLimitExceeded()
    with pytest.raises(SoftTimeLimitExceeded):
        _generate_caption_hashtags([], "football", llm)


def test_generate_caption_hashtags_still_falls_back_on_an_ordinary_error():
    from worker.tasks.generate import _generate_caption_hashtags

    llm = MagicMock()
    llm.complete.side_effect = ValueError("bad json")
    assert _generate_caption_hashtags([], "football", llm) == ("", [])


# ── Phase 7o — standard-path caption/hashtags content-awareness ─────────────
# docs/roadmap.md's Open Issues item: the single-shot MasterGuide JSON call
# writes caption/hashtags from niche/context alone, with no explicit
# instruction to derive them from the beats it just wrote. generate_guide()
# now makes one extra best-effort call to the ALREADY-generalized
# _generate_caption_hashtags() (see its own docstring) with the accepted
# guide's real vo_script content, overwriting the model's own caption/
# hashtags on success and leaving them untouched on any failure.

def _generation_context(reel, db, **overrides):
    """Minimal _GenerationContext for the narrow best-effort-call/path-runner unit
    tests below (module design CAR-1) -- fields not read by the function under test
    are filled with cheap placeholders, since the dataclass requires all 13."""
    from worker.tasks.generate import _GenerationContext

    fields = dict(
        db=db, job=_job(), reel=reel, cuts=[], platforms=["youtube_shorts"],
        target_lengths={"youtube_shorts": 45.0}, max_target=45.0,
        voiceover_mode=reel.voiceover_mode, effective_context=reel.context,
        active_notes=[], axis_multipliers=None, quality_threshold=65, llm=MagicMock(),
    )
    fields.update(overrides)
    return _GenerationContext(**fields)


def test_persist_guide_writes_every_matching_cut_and_skips_unmatched_platforms():
    """A Test-Quality Auditor review of the generate_guide() decomposition (module design
    CAR-1) caught a real coverage gap: no test called _persist_guide() directly or asserted
    on the Cut ORM rows it writes -- the 4 caption-regeneration tests below assert only on
    the pydantic `guide` object, never on `cut.guide`/`cut.caption`/`cut.hashtags`/
    `cut.hook_variants`. A bug here (wrong platform-matching in the `next(...)` lookup, a
    swapped field, a dropped hook_variants assignment) would have passed every test in this
    file undetected.

    Uses TWO platforms with `cuts` passed in the OPPOSITE order from `guide.cuts`, so a
    mutation like "always use the first cut regardless of platform" (mutation-tested below)
    actually has something to get wrong -- a single-platform-guide version of this test
    passed even against that exact mutation, since `cuts[0]` coincidentally WAS the right
    match when there was only one platform_guide to place."""
    from worker.tasks.generate import _persist_guide

    yt_cut = _cut()
    ig_cut = _cut()
    ig_cut.platform.value = "instagram_reels"
    unmatched_cut = _cut()
    unmatched_cut.platform.value = "tiktok"  # no matching platform_guide below -- must be skipped
    original_unmatched_guide = unmatched_cut.guide  # the auto-mock attribute, captured before persisting

    two_platform_raw = json.dumps({
        "title": "Test guide",
        "niche": "football",
        "cuts": [
            {
                "platform": "youtube_shorts",
                "target_length_s": 45.0,
                "beats": [
                    {"index": 0, "type": "hook", "duration_s": 5, "visual_direction": "v",
                     "on_screen_text": ["Hook"], "vo_script": "YouTube hook."},
                    {"index": 1, "type": "body", "duration_s": 10, "visual_direction": "v",
                     "on_screen_text": ["Body"], "vo_script": "YouTube body."},
                    {"index": 2, "type": "cta", "duration_s": 5, "visual_direction": "v",
                     "on_screen_text": ["CTA"], "vo_script": "YouTube CTA."},
                ],
                "caption": "YouTube caption.",
                "hashtags": ["a", "b", "c", "d", "e"],
            },
            {
                "platform": "instagram_reels",
                "target_length_s": 30.0,
                "beats": [
                    {"index": 0, "type": "hook", "duration_s": 3, "visual_direction": "v",
                     "on_screen_text": ["Hook"], "vo_script": "Instagram hook."},
                    {"index": 1, "type": "body", "duration_s": 4, "visual_direction": "v",
                     "on_screen_text": ["Body"], "vo_script": "Instagram body."},
                    {"index": 2, "type": "cta", "duration_s": 3, "visual_direction": "v",
                     "on_screen_text": ["CTA"], "vo_script": "Instagram CTA."},
                ],
                "caption": "Instagram caption.",
                "hashtags": ["f", "g", "h", "i", "j"],
            },
        ],
    })
    guide = MasterGuide.model_validate_json(two_platform_raw)
    hook_variants = ["Alt hook one.", "Alt hook two."]

    # cuts in the OPPOSITE order from guide.cuts (ig, yt, unmatched vs. guide.cuts' yt, ig).
    _persist_guide([ig_cut, yt_cut, unmatched_cut], guide, hook_variants)

    assert yt_cut.caption == "YouTube caption."
    assert yt_cut.hashtags == ["a", "b", "c", "d", "e"]
    assert yt_cut.hook_variants == hook_variants
    assert ig_cut.caption == "Instagram caption."
    assert ig_cut.hashtags == ["f", "g", "h", "i", "j"]
    assert ig_cut.hook_variants == hook_variants
    # unmatched_cut's platform has no corresponding platform_guide -- identity-unchanged
    # proves _persist_guide() never touched it (a MagicMock attribute read alone would
    # auto-vivify a NEW mock, not preserve this one, if the code had actually assigned it).
    assert unmatched_cut.guide is original_unmatched_guide


def test_standard_path_regenerates_caption_hashtags_from_real_vo_content():
    """The success path: _generate_caption_hashtags() returns real content, and
    every platform guide's caption/hashtags is overwritten with it -- not the
    generic caption/hashtags _valid_guide_raw()'s single-shot JSON produced.

    Narrowed to a direct test of _maybe_regenerate_caption_hashtags() after the
    generate_guide() decomposition (module design CAR-1) -- no longer needs the LLM
    provider, build_messages, score_guide, _combined_score,
    _enrich_standard_path_guide, generate_hook_variants, or SessionLocal/job_task's
    own lifecycle; the guide is constructed directly instead of round-tripped through
    a mocked LLM completion."""
    from worker.tasks.generate import _maybe_regenerate_caption_hashtags

    reel = _reel()
    db = MagicMock()
    guide = MasterGuide.model_validate_json(_valid_guide_raw())
    ctx = _generation_context(reel, db)

    with (
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch(
            "worker.tasks.generate._generate_caption_hashtags",
            return_value=("Content-aware caption from real VO.", ["a", "b", "c", "d", "e"]),
        ) as mock_caption,
    ):
        _maybe_regenerate_caption_hashtags(ctx, guide)

    mock_caption.assert_called_once()
    # The real accepted guide's own beat VO content, not empty/placeholder text.
    vo_scripts_arg = mock_caption.call_args.args[0]
    assert any("biggest upset" in v for v in vo_scripts_arg)

    assert guide.cuts[0].caption == "Content-aware caption from real VO."
    assert guide.cuts[0].hashtags == ["a", "b", "c", "d", "e"]


def test_standard_path_regenerates_caption_hashtags_for_every_platform_not_just_the_first():
    """A Test-Quality Auditor review caught a real gap: every prior test used a
    single-cut/single-platform fixture, so a bug that only overwrote
    guide.cuts[0] (e.g. `guide.cuts[0].caption = new_caption` instead of
    looping every platform_guide) would have passed undetected. Uses TWO
    platforms so the overwrite loop actually has more than one item to miss.

    Narrowed to a direct test of _maybe_regenerate_caption_hashtags() -- no longer
    needs Cut fixtures/_query_dispatch at all, since persisting onto Cut rows is now
    a separate function (_persist_guide) this test doesn't exercise."""
    from worker.tasks.generate import _maybe_regenerate_caption_hashtags

    reel = _reel()
    db = MagicMock()
    two_platform_raw = json.dumps({
        "title": "Test guide",
        "niche": "football",
        "cuts": [
            {
                "platform": "youtube_shorts",
                "target_length_s": 45.0,
                "beats": [
                    {"index": 0, "type": "hook", "duration_s": 5, "visual_direction": "stadium wide shot",
                     "on_screen_text": ["Hook"], "vo_script": "Could this be the biggest upset yet?"},
                    {"index": 1, "type": "body", "duration_s": 10, "visual_direction": "training ground clip",
                     "on_screen_text": ["Body"], "vo_script": "The squad has been preparing all week for this."},
                    {"index": 2, "type": "cta", "duration_s": 5, "visual_direction": "crowd celebration",
                     "on_screen_text": ["CTA"], "vo_script": "Drop your prediction below."},
                ],
                "caption": "Big match preview.",
                "hashtags": ["football", "soccer", "sports", "matchday", "preview"],
            },
            {
                "platform": "instagram_reels",
                "target_length_s": 30.0,
                "beats": [
                    {"index": 0, "type": "hook", "duration_s": 3, "visual_direction": "stadium wide shot",
                     "on_screen_text": ["Hook"], "vo_script": "Could this be the biggest upset yet?"},
                    {"index": 1, "type": "body", "duration_s": 4, "visual_direction": "training ground clip",
                     "on_screen_text": ["Body"], "vo_script": "The squad has been preparing all week for this."},
                    {"index": 2, "type": "cta", "duration_s": 3, "visual_direction": "crowd celebration",
                     "on_screen_text": ["CTA"], "vo_script": "Drop your prediction below."},
                ],
                "caption": "Big match preview (IG).",
                "hashtags": ["football", "soccer", "sports", "matchday", "preview"],
            },
        ],
    })
    guide = MasterGuide.model_validate_json(two_platform_raw)
    ctx = _generation_context(reel, db)

    with (
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch(
            "worker.tasks.generate._generate_caption_hashtags",
            return_value=("Content-aware caption from real VO.", ["a", "b", "c", "d", "e"]),
        ),
    ):
        _maybe_regenerate_caption_hashtags(ctx, guide)

    for pg in guide.cuts:
        assert pg.caption == "Content-aware caption from real VO.", pg.platform
        assert pg.hashtags == ["a", "b", "c", "d", "e"], pg.platform


def test_standard_path_skips_caption_regeneration_when_every_vo_script_is_empty():
    """A Correctness/Edge-Case review caught a real edge case: music_only/silent
    voiceover_mode instructs the LLM to leave every vo_script empty
    (build_messages()'s vo_note) -- the caption-regeneration call would
    otherwise fire with nothing to ground a caption in, wasting a paid call
    that could never succeed. Must be skipped outright, not attempted and
    left to fail.

    Narrowed to a direct test of _maybe_regenerate_caption_hashtags()."""
    from worker.tasks.generate import _maybe_regenerate_caption_hashtags

    reel = _reel()
    reel.voiceover_mode = "silent"
    db = MagicMock()
    silent_raw = json.dumps({
        "title": "Test guide",
        "niche": "football",
        "cuts": [{
            "platform": "youtube_shorts",
            "target_length_s": 45.0,
            "beats": [
                {"index": 0, "type": "hook", "duration_s": 5, "visual_direction": "stadium wide shot",
                 "on_screen_text": ["Hook"], "vo_script": ""},
                {"index": 1, "type": "body", "duration_s": 10, "visual_direction": "training ground clip",
                 "on_screen_text": ["Body"], "vo_script": ""},
                {"index": 2, "type": "cta", "duration_s": 5, "visual_direction": "crowd celebration",
                 "on_screen_text": ["CTA"], "vo_script": ""},
            ],
            "caption": "Big match preview.",
            "hashtags": ["football", "soccer", "sports", "matchday", "preview"],
        }],
    })
    guide = MasterGuide.model_validate_json(silent_raw)
    ctx = _generation_context(reel, db)

    with (
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate._generate_caption_hashtags") as mock_caption,
    ):
        _maybe_regenerate_caption_hashtags(ctx, guide)

    mock_caption.assert_not_called()
    assert guide.cuts[0].caption == "Big match preview."


def test_standard_path_keeps_original_caption_when_regeneration_fails():
    """The degrade path: _generate_caption_hashtags() returning its own
    documented failure contract ("", []) must leave the single-shot guide's
    ORIGINAL model-generated caption/hashtags untouched -- never blanked out,
    never worse than before this fix existed.

    Narrowed to a direct test of _maybe_regenerate_caption_hashtags()."""
    from worker.tasks.generate import _maybe_regenerate_caption_hashtags

    reel = _reel()
    db = MagicMock()
    guide = MasterGuide.model_validate_json(_valid_guide_raw())
    ctx = _generation_context(reel, db)

    with (
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate._generate_caption_hashtags", return_value=("", [])),
    ):
        _maybe_regenerate_caption_hashtags(ctx, guide)

    # Exactly the caption/hashtags _valid_guide_raw()'s single-shot JSON carried.
    assert guide.cuts[0].caption == "Big match preview."
    assert guide.cuts[0].hashtags == ["football", "soccer", "sports", "matchday", "preview"]


# ── §3.5 performance-note wiring regressions ─────────────────────────────────
# Both tests below were written first against the naive/buggy implementation
# (a plain `feedback = [...]` replace for #1; notes queried inside the
# standard-path-only `if guide is None:` block for #2), confirmed to fail
# against it, then confirmed to pass against the fix — the same
# mutation-testing discipline this codebase's history already uses (see
# CLAUDE.md's reap_stuck_jobs done-orphan sweep). See
# docs/specs/2026-09-phase5-quality-engagement-feedback.md §3.5 and §7.

def _query_dispatch(notes=None, cuts=None):
    """db.query(Model).filter(...).all() dispatch by Model, for a MagicMock db."""
    notes = notes if notes is not None else []
    cuts = cuts if cuts is not None else [_cut()]

    def _side_effect(model):
        q = MagicMock()
        if model is models.PerformanceNote:
            q.filter.return_value.all.return_value = notes
        elif model is models.Cut:
            q.filter.return_value.all.return_value = cuts
        else:
            q.filter.return_value.all.return_value = []
        return q

    return _side_effect


def _valid_guide_raw() -> str:
    """A minimal but schema-valid MasterGuide JSON string, matching the single
    youtube_shorts cut _cut() produces, for llm.complete() to return."""
    return json.dumps({
        "title": "Test guide",
        "niche": "football",
        "cuts": [{
            "platform": "youtube_shorts",
            "target_length_s": 45.0,
            "beats": [
                {"index": 0, "type": "hook", "duration_s": 5, "visual_direction": "stadium wide shot",
                 "on_screen_text": ["Hook"], "vo_script": "Could this be the biggest upset yet?"},
                {"index": 1, "type": "body", "duration_s": 10, "visual_direction": "training ground clip",
                 "on_screen_text": ["Body"], "vo_script": "The squad has been preparing all week for this."},
                {"index": 2, "type": "cta", "duration_s": 5, "visual_direction": "crowd celebration",
                 "on_screen_text": ["CTA"], "vo_script": "Drop your prediction below."},
            ],
            "caption": "Big match preview.",
            "hashtags": ["football", "soccer", "sports", "matchday", "preview"],
        }],
    })


def test_seeded_performance_notes_survive_past_attempt_1_on_retry():
    """The retry-replace bug: `feedback = [...]` (plain replace) on a failed attempt
    would silently drop performance notes seeded before attempt 1. Force attempt 1
    to score below threshold and attempt 2 to succeed; the seeded notes' text must
    still be present in the build_messages() call for attempt 2, not just attempt 1.

    Narrowed to a direct test of _run_standard_path_attempts() after the
    generate_guide() decomposition (module design CAR-1) -- no longer needs
    path-selection (script_parser.parse/resolve_generation_path), the best-effort
    caption/hook-variant calls, or SessionLocal/job_task's own lifecycle; active
    notes are passed in directly instead of round-tripped through a mocked
    PerformanceNote query.
    """
    from worker.tasks.generate import _run_standard_path_attempts

    reel = _reel()
    db = MagicMock()
    llm = MagicMock()
    llm.complete.return_value = _valid_guide_raw()
    notes_text = [
        "Direct-question hooks outperform statement hooks.",
        "Keep the CTA under 8 seconds.",
    ]
    ctx = _generation_context(reel, db, active_notes=notes_text, llm=llm)

    def _combined_score_side_effect(rule_s, rule_i, guide, context, db=None, reel_id=None, attempt=None):
        if attempt == 1:
            return 30, ["Weak hook — add a question or direct address"]
        return 90, []

    with (
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.score_guide", return_value=(0, [])),
        patch("worker.tasks.generate._combined_score", side_effect=_combined_score_side_effect),
        patch("worker.tasks.generate._enrich_standard_path_guide"),
        patch(
            "worker.tasks.generate.build_messages",
            return_value=[{"role": "system", "content": "x"}],
        ) as mock_build_messages,
    ):
        result = _run_standard_path_attempts(ctx, stubs=None)

    assert result.guide is not None
    assert mock_build_messages.call_count >= 2

    second_call_kwargs = mock_build_messages.call_args_list[1].kwargs
    prior_feedback = second_call_kwargs.get("prior_feedback") or []
    for text in notes_text:
        assert text in prior_feedback, (
            f"seeded note {text!r} missing from attempt-2 prior_feedback — "
            "the retry-replace bug dropped it"
        )


def test_active_performance_notes_text_is_correctly_extracted_and_seeded():
    """A Test-Quality Auditor review of the generate_guide() decomposition (module design
    CAR-1) caught a real coverage gap: test_seeded_performance_notes_survive_past_attempt_1_
    on_retry above now injects active_notes as plain strings directly into
    _GenerationContext, bypassing generate_guide()'s own `active_notes = [n.text for n in
    active_notes_rows]` extraction entirely -- a regression swapping `.text` for `.id` (or
    any other field) there would ship silently, with no crash and no failing assertion
    anywhere. This test drives the real end-to-end generate_guide() path with a real
    PerformanceNote-shaped row to prove that extraction actually works."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    notes = [SimpleNamespace(id=42, text="A real performance note's text.")]
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.side_effect = _query_dispatch(notes=notes, cuts=[_cut()])

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.script_parser.parse", return_value=None),  # force standard path
        patch("worker.tasks.generate.get_llm_provider") as mock_get_llm,
        patch(
            "worker.tasks.generate.build_messages",
            return_value=[{"role": "system", "content": "x"}],
        ) as mock_build_messages,
        patch("worker.tasks.generate.score_guide", return_value=(0, [])),
        patch("worker.tasks.generate._combined_score", return_value=(90, [])),
        patch("worker.tasks.generate._enrich_standard_path_guide"),
        patch("worker.tasks.generate.generate_hook_variants", return_value=[]),
        patch("worker.tasks.generate._generate_caption_hashtags", return_value=("", [])),
    ):
        mock_get_llm.return_value.complete.return_value = _valid_guide_raw()
        generate_guide(1)

    assert job.status == models.JobStatus.done
    first_call_kwargs = mock_build_messages.call_args_list[0].kwargs
    assert first_call_kwargs.get("prior_feedback") == ["A real performance note's text."]


def test_structured_path_success_does_not_nameerror_on_performance_notes():
    """The NameError bug: active_notes_rows must be queried unconditionally at the
    top of generate_guide, not inside the `if guide is None:` (standard-path-only)
    branch — the shared job.meta write at the end of the function is reached by
    BOTH paths. Run the structured path to a threshold-clearing success with
    active PerformanceNotes present and assert the job completes `done` (not an
    unhandled NameError) with job.meta["performance_note_ids"] set correctly.
    """
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    notes = [SimpleNamespace(id=7, text="Some performance note.")]
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.side_effect = _query_dispatch(notes=notes, cuts=[_cut()])

    structured_guide = MasterGuide(
        title="Structured guide",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45.0,
            caption="Caption",
            hashtags=["football"] * 6,
            beats=[
                Beat(index=0, type="hook", duration_s=5, visual_direction="v",
                     on_screen_text=["h"], vo_script="Could this be the biggest upset yet?"),
                Beat(index=1, type="body", duration_s=10, visual_direction="v",
                     on_screen_text=["b"], vo_script="Body content here."),
                Beat(index=2, type="cta", duration_s=5, visual_direction="v",
                     on_screen_text=["c"], vo_script="Drop your prediction below."),
            ],
        )],
    )

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider"),
        patch("worker.tasks.generate.script_parser.parse", return_value=[MagicMock()]),
        patch("worker.tasks.generate.resolve_generation_path", return_value="structured"),
        patch("worker.tasks.generate._generate_from_structured_script", return_value=structured_guide),
        patch("worker.tasks.generate.score_guide", return_value=(90, [])),
        patch("worker.tasks.generate._combined_score", return_value=(90, [])),
        patch("worker.tasks.generate.generate_hook_variants", return_value=[]),
        patch("worker.tasks.generate.build_messages") as mock_build_messages,
    ):
        generate_guide(1)

    mock_build_messages.assert_not_called()  # never falls through to the standard path
    assert job.status == models.JobStatus.done
    assert job.meta.get("performance_note_ids") == [7]
    assert job.meta.get("quality_score") == 90


def test_structured_path_success_does_not_regenerate_caption_hashtags_a_second_time():
    """A Correctness/Edge-Case review of the generate_guide() decomposition (module design
    CAR-1) caught a real regression, confirmed by running both versions: the original inline
    code nested the caption/hashtags-regeneration call INSIDE `if guide is None:` (the
    standard-path-only block) -- a direct structured-path success never reached it, since the
    structured path already produces content-aware caption/hashtags via its own
    _generate_caption_hashtags() call inside _generate_from_structured_script(). The first
    decomposed version called _maybe_regenerate_caption_hashtags() unconditionally from the
    orchestrator, silently double-regenerating (and risking overwriting with a second,
    nondeterministic LLM result) every structured-path success. Fixed by moving the call back
    inside _run_standard_path_attempts(), reached only on the standard path."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.side_effect = _query_dispatch(notes=[], cuts=[_cut()])

    structured_guide = MasterGuide(
        title="Structured guide",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45.0,
            caption="Structured path's own caption.",
            hashtags=["football"] * 6,
            beats=[
                Beat(index=0, type="hook", duration_s=5, visual_direction="v",
                     on_screen_text=["h"], vo_script="Could this be the biggest upset yet?"),
                Beat(index=1, type="body", duration_s=10, visual_direction="v",
                     on_screen_text=["b"], vo_script="Body content here."),
                Beat(index=2, type="cta", duration_s=5, visual_direction="v",
                     on_screen_text=["c"], vo_script="Drop your prediction below."),
            ],
        )],
    )

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider"),
        patch("worker.tasks.generate.script_parser.parse", return_value=[MagicMock()]),
        patch("worker.tasks.generate.resolve_generation_path", return_value="structured"),
        patch("worker.tasks.generate._generate_from_structured_script", return_value=structured_guide),
        patch("worker.tasks.generate.score_guide", return_value=(90, [])),
        patch("worker.tasks.generate._combined_score", return_value=(90, [])),
        patch("worker.tasks.generate.generate_hook_variants", return_value=[]),
        patch("worker.tasks.generate.build_messages") as mock_build_messages,
        patch("worker.tasks.generate._generate_caption_hashtags") as mock_caption,
    ):
        generate_guide(1)

    mock_build_messages.assert_not_called()  # never falls through to the standard path
    mock_caption.assert_not_called()
    cut = db.query.side_effect(models.Cut).filter.return_value.all.return_value[0]
    assert cut.caption == "Structured path's own caption."


# ── reaper-resume design §7 — job.meta staleness-across-retry regression ────
# A killed (or ordinarily retried) attempt of this SAME Job row may have already
# committed structured_fallback=True/structured_score from a prior run that took
# the structured path, failed its quality gate, and fell through to standard.
# job.meta is always merged additively, never reset, so those stale keys must
# not survive into a SUBSEQUENT clean structured-path success's final job.meta
# (a real bug: it would misclassify that reel's cost history in
# estimate_generation()'s structured_fallback-exclusion bucket). Written first
# against the naive/buggy version (no strip at function entry), confirmed to
# fail, then confirmed to pass against the fix — this file's established
# mutation-testing convention (see the §3.5 regressions above).

def test_stale_structured_fallback_does_not_leak_into_a_clean_success():
    from worker.tasks.generate import generate_guide

    job = _job()
    # Seed job.meta as if a prior, killed/retried attempt of this exact Job row
    # had already committed a structured-path fallback, alongside keys that must
    # NEVER be stripped (context_score, performance_note_ids — legitimately
    # persisted, unrelated to this leak).
    job.meta = {
        "structured_fallback": True,
        "structured_score": 40,
        "path": "standard",
        "context_score": 55,
        "performance_note_ids": [999],
    }
    reel = _reel()
    db = MagicMock()
    notes = [SimpleNamespace(id=7, text="Some performance note.")]
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.side_effect = _query_dispatch(notes=notes, cuts=[_cut()])

    structured_guide = MasterGuide(
        title="Structured guide",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45.0,
            caption="Caption",
            hashtags=["football"] * 6,
            beats=[
                Beat(index=0, type="hook", duration_s=5, visual_direction="v",
                     on_screen_text=["h"], vo_script="Could this be the biggest upset yet?"),
                Beat(index=1, type="body", duration_s=10, visual_direction="v",
                     on_screen_text=["b"], vo_script="Body content here."),
                Beat(index=2, type="cta", duration_s=5, visual_direction="v",
                     on_screen_text=["c"], vo_script="Drop your prediction below."),
            ],
        )],
    )

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider"),
        patch("worker.tasks.generate.script_parser.parse", return_value=[MagicMock()]),
        patch("worker.tasks.generate.resolve_generation_path", return_value="structured"),
        patch("worker.tasks.generate._generate_from_structured_script", return_value=structured_guide),
        patch("worker.tasks.generate.score_guide", return_value=(90, [])),
        patch("worker.tasks.generate._combined_score", return_value=(90, [])),
        patch("worker.tasks.generate.generate_hook_variants", return_value=[]),
        patch("worker.tasks.generate.build_messages") as mock_build_messages,
    ):
        generate_guide(1)

    mock_build_messages.assert_not_called()  # never falls through to the standard path
    assert job.status == models.JobStatus.done
    assert "structured_fallback" not in job.meta, (
        "stale structured_fallback from a prior killed/retried attempt leaked into a "
        "clean structured-path success"
    )
    assert "structured_score" not in job.meta
    assert job.meta.get("path") == "structured"
    # Legitimately-persisted keys, unrelated to the leak, must survive the strip.
    assert job.meta.get("context_score") == 55
    assert job.meta.get("performance_note_ids") == [7]


def test_genuine_structured_fallback_still_recorded_after_the_strip():
    """The strip-then-rebuild at function entry must not suppress a REAL fallback
    signal from THIS run — only a stale one carried over from a prior attempt.
    Force the structured path to score below threshold so it genuinely falls
    through to (and succeeds on) the standard path, and assert job.meta still
    ends up with structured_fallback=True / structured_score set from this run,
    exactly as estimate_generation()'s exclusion logic depends on."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.side_effect = _query_dispatch(notes=[], cuts=[_cut()])

    structured_guide = MasterGuide(
        title="Structured guide",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45.0,
            caption="Caption",
            hashtags=["football"] * 6,
            beats=[
                Beat(index=0, type="hook", duration_s=5, visual_direction="v",
                     on_screen_text=["h"], vo_script="Could this be the biggest upset yet?"),
                Beat(index=1, type="body", duration_s=10, visual_direction="v",
                     on_screen_text=["b"], vo_script="Body content here."),
                Beat(index=2, type="cta", duration_s=5, visual_direction="v",
                     on_screen_text=["c"], vo_script="Drop your prediction below."),
            ],
        )],
    )

    def _combined_score_side_effect(rule_s, rule_i, guide, context, db=None, reel_id=None, attempt=None):
        if attempt == 1:
            return 30, ["Weak hook — add a question or direct address"]
        return 90, []

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider") as mock_get_llm,
        patch("worker.tasks.generate.script_parser.parse", return_value=[MagicMock()]),
        patch("worker.tasks.generate.resolve_generation_path", return_value="structured"),
        patch("worker.tasks.generate._generate_from_structured_script", return_value=structured_guide),
        patch("worker.tasks.generate.score_guide", return_value=(0, [])),
        patch("worker.tasks.generate._combined_score", side_effect=_combined_score_side_effect),
        patch("worker.tasks.generate.build_messages", return_value=[{"role": "system", "content": "x"}]),
        patch("worker.tasks.generate._enrich_standard_path_guide"),
        patch("worker.tasks.generate.generate_hook_variants", return_value=[]),
        patch("worker.tasks.generate._generate_caption_hashtags", return_value=("", [])),
    ):
        mock_get_llm.return_value.complete.return_value = _valid_guide_raw()
        generate_guide(1)

    assert job.status == models.JobStatus.done
    assert job.meta.get("structured_fallback") is True
    assert job.meta.get("structured_score") == 30
    assert job.meta.get("path") == "standard"


# ── _enrich_standard_path_guide() niche threading (improve-codebase-architecture
# review, candidate 1) ────────────────────────────────────────────────────────
#
# Previously beat_enrichment.py was football-hardcoded with no niche parameter at
# all, so a non-football standard-path reel naming a specific person was silently
# analyzed with football-tactics framing. _enrich_standard_path_guide() is the one
# call site threading the guide's real niche through — every existing test in this
# file patches it out entirely with no assertion on call args, so a regression here
# (e.g. hardcoding "football" or passing the wrong variable) would pass silently.
# See docs/specs/2026-09-beat-enrichment-niche-branching-module-design.md.

def test_enrich_standard_path_guide_threads_the_guides_real_niche_through():
    from worker.tasks.generate import _enrich_standard_path_guide

    guide = MasterGuide(
        title="t", niche="personal finance",
        cuts=[PlatformGuide(
            platform="youtube_shorts", target_length_s=30, caption="c", hashtags=list("abcde"),
            beats=[
                Beat(index=0, type="hook", duration_s=3.0, visual_direction="v",
                     on_screen_text=["x"], vo_script="Could you retire early?"),
                Beat(index=1, type="body", duration_s=5.0, visual_direction="Warren Buffett",
                     on_screen_text=["x"], vo_script="Buffett invests."),
                Beat(index=2, type="cta", duration_s=3.0, visual_direction="v",
                     on_screen_text=["x"], vo_script="Follow for more."),
            ],
        )],
    )

    with patch("worker.tasks.generate._enrich_with_insight") as mock_enrich:
        _enrich_standard_path_guide(guide, "some context", MagicMock())

    # positional args: (stubs, context, enrichment_llm, niche)
    assert mock_enrich.call_args.args[3] == "personal finance"


# ── _generate_from_structured_script() niche wiring (PR #36 review) ──────────
#
# The structured path is the main enrichment path and was never exercised by any
# test (every test patches _generate_from_structured_script out). Reel.niche is
# nullable, and an unset niche must keep the pre-niche-parameter football behavior
# rather than crash or fall to the generic prompt. Runs the REAL _enrich_with_insight
# through a capturing LLM, bailing out right after the enrich stage.

class _StopAfterEnrich(Exception):
    pass


@pytest.mark.parametrize("niche,expected_in_prompt", [
    ("personal finance", "content analyst for a personal finance video"),
    (None, "football tactical analyst"),
    ("", "football tactical analyst"),
    ("Premier League football", "football tactical analyst"),
])
def test_structured_path_threads_reel_niche_into_enrichment(niche, expected_in_prompt):
    from worker.tasks.generate import _generate_from_structured_script

    captured = []

    class CaptureLLM:
        total_usage = {}

        def complete(self, messages, **kwargs):
            captured.append(messages)
            return json.dumps([{"index": 0, "tactical_sentence":
                                "This discipline lets him avoid panic selling during downturns"}])

    stub = BeatStub(
        index=0, beat_type="body", section="", player="Warren Buffett",
        vo_script="Buffett holds stocks.", duration_s=5.0, on_screen_text=["x"],
    )
    ev = SimpleNamespace(detail={}, tokens_in=0, tokens_out=0, cost_usd=0)
    stage_cm = MagicMock()
    stage_cm.return_value.__enter__.return_value = ev
    stage_cm.return_value.__exit__.return_value = False

    with (
        patch("worker.tasks.generate.get_enrichment_provider", return_value=CaptureLLM()),
        patch("worker.tasks.generate.record_stage", stage_cm),
        patch("worker.tasks.generate._has_conflict_beat", return_value=True),
        patch("worker.tasks.generate.build_visuals_messages", side_effect=_StopAfterEnrich),
    ):
        with pytest.raises(_StopAfterEnrich):
            _generate_from_structured_script(
                SimpleNamespace(id=1, niche=niche), [], MagicMock(), MagicMock(), {}, [stub], "ctx",
            )

    assert ev.detail["shallow_beats"] == 1
    system_msg = next(m["content"] for m in captured[0] if m["role"] == "system")
    assert expected_in_prompt in system_msg
